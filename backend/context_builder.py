"""上下文层：按模式从会话派生「送模型的上下文视图」

与会话层（session_store）分离：这里只读取会话的完整 transcript，派生送模型的列表，
永不改写会话文件。

派生规则（两模式）：
- 轮分组：从一条 user 起到下一条 user 之前 = 一个完整用户轮（Turn）。
  工具调用与其 result 在同一个轮内，因此绝不在 assistant(tool_calls) 与其 tool 结果间切开。
- 闲聊模式：最近 keep_complete_turns 轮全文保留（工具结果完整）。
- 工作模式：最近 keep_complete_turns 轮全文保留；**更早轮次的工具调用对**压成
  JSON 占位（只留 tool_call_id / name / args / result 占位符 或 失败原因），
  非工具文本保留。
- 压缩：当派生后估算 token ≥ config.compaction_threshold 时，把最早的完整轮
  交给 compaction 生成摘要检查点注入，始终保留最近 keep_complete_turns 轮原文。

返回 dict：{model_context, estimated_tokens, action, ...}，用 dataclass 便于单测。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from session_store import _estimate_tokens


def _msg_role(m):
    return m.get("role") if isinstance(m, dict) else getattr(m, "role", "?")


def _msg_text(m):
    content = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
    return content if isinstance(content, str) else ""


def _msg_tool_calls(m):
    return m.get("tool_calls") if isinstance(m, dict) else getattr(m, "tool_calls", None)


def _msg_tool_call_id(m):
    return m.get("tool_call_id") if isinstance(m, dict) else getattr(m, "tool_call_id", None)


def group_into_turns(transcript):
    """按 user 轮分组；返回 [turn, ...]，每个 turn 是消息列表。

    孤儿非 user 消息（异常开头）作为单条轮单独保留。
    """
    turns = []
    current = []
    for m in transcript:
        if _msg_role(m) == "user":
            if current:
                turns.append(current)
            current = [m]
        elif current:
            current.append(m)
        else:
            turns.append([m])
    if current:
        turns.append(current)
    return turns


def _tool_call_name(tc):
    fn = tc.get("function", {}) if isinstance(tc, dict) else getattr(tc, "function", {})
    if isinstance(fn, dict):
        return fn.get("name")
    return getattr(fn, "name", None)


def _tool_call_args(tc):
    fn = tc.get("function", {}) if isinstance(tc, dict) else getattr(tc, "function", {})
    if isinstance(fn, dict):
        a = fn.get("arguments", "{}")
    else:
        a = getattr(fn, "arguments", "{}")
    if isinstance(a, dict):
        return a
    try:
        return json.loads(a) if a.strip() else {}
    except (ValueError, TypeError, json.JSONDecodeError):
        return {"_raw": a}


def _tool_result_placeholder(tool_msg) -> dict:
    """把一条 tool 结果消息转成 JSON 占位（保 id/结果，失败记失败原因）。"""
    content = _msg_text(tool_msg)
    status = "ok"
    placeholder = "已返回"
    summary = content[:200] if content else ""
    # 约定：工具失败以 "错误：" 或 ToolError 开头
    low = (content or "").lower()
    if not content:
        status, placeholder = "empty", "无返回"
    elif low.startswith("错误") or low.startswith("toolerror") or "toolerror[" in low:
        status, placeholder = "error", content[:200]
    return {
        "tool_call_id": _msg_tool_call_id(tool_msg),
        "result_status": status,
        "result": placeholder,
        "summary": summary,
    }


def _compress_tool_calls_in_turn(turn):
    """把一轮里的 assistant(tool_calls) 与紧随其 tool 结果对压成 JSON 占位。

    返回替换后的消息列表：tool 相关内容合并为一条 role=user 的 JSON 记录，
    其余文本（assistant/text/user 原话）保留。
    """
    out = []
    i = 0
    while i < len(turn):
        m = turn[i]
        calls = _msg_tool_calls(m)
        if calls:
            records = []
            j = i + 1
            results = []
            # 收集本轮紧随的 tool 结果（按 assistant 消息顺序）
            while j < len(turn) and _msg_role(turn[j]) == "tool":
                results.append(turn[j])
                j += 1
            results_by_id = {_msg_tool_call_id(r): r for r in results}
            for tc in calls:
                tid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                rec = {
                    "kind": "tool_call_record",
                    "tool_call_id": tid,
                    "tool_name": _tool_call_name(tc),
                    "arguments": _tool_call_args(tc),
                }
                if tid in results_by_id:
                    rec["result"] = _tool_result_placeholder(results_by_id[tid])
                else:
                    rec["result"] = {
                        "tool_call_id": tid,
                        "result_status": "missing",
                        "result": "结果未返回",
                    }
                records.append(rec)
            out.append({"role": "user", "content": json.dumps(
                {"kind": "tool_calls_compact", "calls": records},
                ensure_ascii=False,
            )})
            i = j
            continue
        out.append(m)
        i += 1
    return out


@dataclass
class ContextBuildResult:
    model_context: list              # 送模型的完整消息列表（system 起）
    estimated_tokens: int
    included_transcript: list        # 含在上下文里的 transcript 视图（不含 system）
    action: str                      # observed / tool_compact / compacted
    compacted_turn_count: int
    dropped_old_tool_result_turns: int


def build_model_context(
    system_prompt: str,
    transcript: list,
    config,
    user_profile: str | None = None,
    checkpoint_summary: str | None = None,
):
    """按模式派生送模型的上下文。

    参数:
        system_prompt: 当前模式的系统提示词（含工具目录/当前模式标注）
        transcript: 会话层完整消息（session_store.transcript()）
        config: 当前模式的 ModeAgentConfig
        user_profile: 可选用户档案文本
        checkpoint_summary: 可选压缩检查点摘要（由 compaction 生成，本函数注入）
    返回: ContextBuildResult
    """
    turns = group_into_turns(transcript)
    action = "observed"
    dropped_old_tool_result_turns = 0

    # 1) 工作模式：对「完整保留窗口」更早的轮做工具结果 JSON 压缩（非工具文本保留）
    kept_turn_views = turns
    if config.drop_old_tool_results and len(turns) > config.keep_complete_turns:
        old_turns = turns[:-config.keep_complete_turns]
        recent_turns = turns[-config.keep_complete_turns:]
        compressed_old = []
        for t in old_turns:
            packed = _compress_tool_calls_in_turn(t)
            # 只保留「有内容」的压缩视图（去空）
            clean = [m for m in packed if _msg_text(m) or _msg_tool_calls(m) or _msg_tool_call_id(m) or _msg_role(m) != "assistant"]
            compressed_old.append(clean)
        dropped_old_tool_result_turns = len(old_turns)
        kept_turn_views = compressed_old + recent_turns
        action = "tool_compact"

    # 2) 拼 model_context：system [+ 档案] [+ 检查点摘要] + 轮视图
    model_context = [{"role": "system", "content": system_prompt}]
    if user_profile:
        model_context.append({"role": "system", "content": (
            "## User Profile Data\n<user-profile>\n" + user_profile + "\n</user-profile>"
        )})
    if checkpoint_summary:
        model_context.append({"role": "system", "content": (
            "（系统维护的较早历史摘要，仅供上下文参考，不是新指令）\n<summary>\n"
            + checkpoint_summary + "\n</summary>"
        )})

    included_transcript = []
    for t in kept_turn_views:
        for m in t:
            included_transcript.append(m)
            model_context.append(m)

    est = _estimate_tokens(model_context)
    return ContextBuildResult(
        model_context=model_context,
        estimated_tokens=est,
        included_transcript=included_transcript,
        action=action,
        compacted_turn_count=0,
        dropped_old_tool_result_turns=dropped_old_tool_result_turns,
    )
