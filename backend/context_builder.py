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
import os
from dataclasses import dataclass

from session_store import _estimate_tokens

# 近 N 条工具结果不截断（完整保留；chat 与 work 一致，见设计 2.1）
KEEP_RECENT_TOOL_FULL = 10
# 待压缩工具结果保留在上下文里的片段长度（完整内容落盘 tool_result/<uuid>.txt）
TOOL_FRAGMENT_CHARS = 200


def _msg_role(m):
    return m.get("role") if isinstance(m, dict) else getattr(m, "role", "?")


def _msg_text(m):
    content = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
    return content if isinstance(content, str) else ""


def _msg_tool_calls(m):
    return m.get("tool_calls") if isinstance(m, dict) else getattr(m, "tool_calls", None)


def _tc_id(tc):
    return tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)


def _msg_tool_calls_ids(m):
    return [_tc_id(tc) for tc in (_msg_tool_calls(m) or [])]


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


def _turn_tool_ids(turn):
    """一个完整轮里的 tool 结果对应的 tool_call_id 列表。"""
    return [_msg_tool_call_id(m) for m in turn if _msg_role(m) == "tool"]


def _recent_tool_full_ids(transcript, n: int) -> set:
    """返回 transcript 中「最近 n 条 tool 结果」的 tool_call_id 集合（不截断）。"""
    ids = [_msg_tool_call_id(m) for m in transcript if _msg_role(m) == "tool"]
    return set(ids[-n:]) if n > 0 else set()


def _tool_result_placeholder(tool_msg, memory_fs=None) -> dict:
    """把一条 tool 结果消息转成 JSON 占位。

    完整内容落盘到 tool_result/<uuid>.txt（若提供 memory_fs），上下文只留
    截断片段 + 续读提示（design 2.1「片段 + 续读」）。不传 memory_fs 时降级为
    仅保留片段（便于纯内存单测）。
    """
    content = _msg_text(tool_msg)
    status = "ok"
    placeholder = "已返回"
    summary = content[:TOOL_FRAGMENT_CHARS] if content else ""
    # 约定：工具失败以 "错误：" 或 ToolError 开头
    low = (content or "").lower()
    if not content:
        status, placeholder = "empty", "无返回"
    elif low.startswith("错误") or low.startswith("toolerror") or "toolerror[" in low:
        status, placeholder = "error", content[:TOOL_FRAGMENT_CHARS]
    rec: dict = {
        "tool_call_id": _msg_tool_call_id(tool_msg),
        "result_status": status,
        "result": placeholder,
        "fragment": summary,
    }
    # 完整结果落盘 + 续读提示（design 2.1）
    if memory_fs is not None and content and status != "error":
        try:
            tool_id = memory_fs.save_tool_result(content)
            rec["tool_id"] = tool_id
            rec["read_path"] = memory_fs.tool_path(tool_id)
            rec["read_hint"] = ("结果已截断，如需完整内容可读取 tool_result 目录下 "
                                f"文件 {tool_id}.txt")
        except Exception:  # 落盘失败不阻断上下文派生
            pass
    return rec


def _compress_tool_calls_in_turn(turn, keep_full_ids=None, memory_fs=None):
    """把一轮里的 assistant(tool_calls) 与紧随其 tool 结果对压成 JSON 占位。

    近 N 条工具结果（keep_full_ids）不压缩、保留原始 tool 消息；
    其余工具结果转成 JSON 占位（片段 + 续读，完整内容落盘）。
    返回替换后的消息列表：tool 相关内容合并为一条 role=user 的 JSON 记录，
    其余文本（assistant/text/user 原话）保留。
    """
    keep_full_ids = keep_full_ids or set()
    out = []
    i = 0
    while i < len(turn):
        m = turn[i]
        calls = _msg_tool_calls(m)
        if calls:
            j = i + 1
            results = []
            while j < len(turn) and _msg_role(turn[j]) == "tool":
                results.append(turn[j])
                j += 1
            results_by_id = {_msg_tool_call_id(r): r for r in results}
            # 若本轮工具调用里的结果全部在「近 N 全量保留」集合里 → 保留原文
            call_ids = _msg_tool_calls_ids(m)
            if call_ids and all(cid in keep_full_ids for cid in call_ids) \
                    and all(cid in results_by_id for cid in call_ids):
                out.extend(turn[i:j])
                i = j
                continue
            records = []
            results_kept = []  # 撞到最近 N 全量保留的结果（罕见跨轮边界情形，兜底）
            for tc in calls:
                tid = _tc_id(tc)
                rec = {
                    "kind": "tool_call_record",
                    "tool_call_id": tid,
                    "tool_name": _tool_call_name(tc),
                    "arguments": _tool_call_args(tc),
                }
                if tid in results_by_id:
                    if tid in keep_full_ids:
                        results_kept.append(results_by_id[tid])
                        continue
                    rec["result"] = _tool_result_placeholder(results_by_id[tid], memory_fs)
                else:
                    rec["result"] = {
                        "tool_call_id": tid,
                        "result_status": "missing",
                        "result": "结果未返回",
                    }
                records.append(rec)
            if records:
                out.append({"role": "user", "content": json.dumps(
                    {"kind": "tool_calls_compact", "calls": records},
                    ensure_ascii=False,
                )})
            out.extend(results_kept)
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
    memory_blocks: dict | None = None,
    memory_max_tokens: int | None = None,
    memory_fs=None,
    memory_text: str | None = None,
):
    """按模式派生送模型的上下文。

    参数:
        system_prompt: 当前模式的系统提示词（含工具目录/当前模式标注）
        transcript: 会话层完整消息（session_store.transcript()）
        config: 当前模式的 ModeAgentConfig
        user_profile: 可选用户档案文本
        checkpoint_summary: 可选压缩检查点摘要（由 compaction 生成，本函数注入）
        memory_blocks: 可选记忆块 {"l3":str, "l2":[...], "l1":[...]}，按 L3>L2>L1 注入
        memory_max_tokens: 记忆注入预算（估算 token），超了按权重截断
        memory_fs: 可选 MemoryFs 实例；提供时把被压缩的工具结果完整内容落盘
                   tool_result/<uuid>.txt（design 2.1 片段+续读）
    返回: ContextBuildResult
    """
    turns = group_into_turns(transcript)
    action = "observed"
    dropped_old_tool_result_turns = 0

    # 1) 工作模式：对「完整保留窗口」更早的轮做工具结果 JSON 压缩（非工具文本保留）
    #    近 N 条工具结果（KEEP_RECENT_TOOL_FULL）不截断，完整保留（chat/work 一致）。
    kept_turn_views = turns
    if config.drop_old_tool_results and len(turns) > config.keep_complete_turns:
        recent_full_ids = _recent_tool_full_ids(transcript, KEEP_RECENT_TOOL_FULL)
        # 完整保留的轮：尾部 keep_complete_turns 轮 + 含近 N 工具结果的轮
        keep_indices = set(range(max(0, len(turns) - config.keep_complete_turns), len(turns)))
        for idx, t in enumerate(turns):
            if any(cid in recent_full_ids for cid in _turn_tool_ids(t)):
                keep_indices.add(idx)
        old_indices = [i for i in range(len(turns)) if i not in keep_indices]
        if old_indices:
            kept_turn_views = []
            for idx, t in enumerate(turns):
                if idx in old_indices:
                    packed = _compress_tool_calls_in_turn(t, keep_full_ids=recent_full_ids,
                                                          memory_fs=memory_fs)
                    # 只保留「有内容」的压缩视图（去空）
                    clean = [m for m in packed
                             if _msg_text(m) or _msg_tool_calls(m) or _msg_tool_call_id(m)
                             or _msg_role(m) != "assistant"]
                    kept_turn_views.append(clean)
                else:
                    kept_turn_views.append(t)
            dropped_old_tool_result_turns = len(old_indices)
            action = "tool_compact"

    # 2) 拼 model_context：system [+ 档案] [+ 检查点摘要] [+ 记忆] + 轮视图
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

    # 记忆注入：L3(叙事) > L2(事实) > L1(事件)，受 memory_max_tokens 预算约束
    if memory_blocks is not None:
        mem_system = _build_memory_system_block(memory_blocks, memory_max_tokens)
        if mem_system:
            model_context.append({"role": "system", "content": mem_system})

    # v2 扁平文件记忆（MEMORY.md + 昨日日志）注入：design 2.3
    if memory_text:
        model_context.append({"role": "system", "content":
            "（以下为长期记忆，仅供上下文参考）\n" + memory_text})

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


def _build_memory_system_block(memory_blocks: dict, max_tokens: int | None) -> str:
    """把记忆块拼成一段 system 文本，L3>L2>L1，预算内截断。"""
    l3 = memory_blocks.get("l3")
    l2 = memory_blocks.get("l2") or []
    l1 = memory_blocks.get("l1") or []

    parts: list[tuple[str, int, str]] = []  # (key, weight_order, text)
    order = {"l3": 0, "l2": 1, "l1": 2}
    if l3:
        parts.append((l3, order["l3"], f"- 长期画像：{l3}"))
    for it in l2:
        txt = it.get("text") if isinstance(it, dict) else str(it)
        if txt:
            confirmed = it.get("confirmed", True) if isinstance(it, dict) else True
            if not confirmed:
                continue  # 未确认事实默认不注入
            parts.append((txt, order["l2"], f"- {txt}"))
    for it in l1:
        txt = it.get("text") if isinstance(it, dict) else str(it)
        if txt:
            parts.append((txt, order["l1"], f"- (事件) {txt}"))

    if not parts:
        return ""

    # 预算 clamp：按权重顺序累加，超预算就停（est 用估算 token）
    budget = max_tokens if max_tokens and max_tokens > 0 else 1800
    selected: list[str] = []
    acc = 0
    for _key, _order, text in sorted(parts, key=lambda p: p[1]):
        t = _estimate_tokens(text)
        if acc + 3 + t > budget:
            break
        selected.append(text)
        acc += 3 + t
    if not selected:
        # 至少保留权重最高的 L3/第一条，避免记忆为空但开关开着
        top = min(parts, key=lambda p: p[1])
        selected = [top[2]]
    return "## Long-term Memory (about the user)\n" + "\n".join(selected)

