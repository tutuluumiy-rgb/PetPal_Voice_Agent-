"""原生 function calling 的多 sub_turn agent 环（新架构核心）

区别于旧的"文本 TOOL_CALL 声明"（agent_loop.run_tool_loop）：
- 工具以 API `tools` 参数传给 LLM（按当前模式白名单构建）
- LLM 返回原生 tool_calls（带 id）→ 并发调度执行 → 按 tool_call_id 回填 tool 消息
- 每个 sub_turn 从会话层重新派生上下文（context_builder）；超阈值触发压缩（compaction）

run 语义：
    run       = 处理一条用户输入的一次完整 agent loop（每 run 生成 run_id）
    sub_turn  = run 里第 N 次模型调用（含工具轮 + 最终回复轮）
    所有消息带 run_id/sub_turn/tool_call_id 写入会话层，可追溯。

降级兜底：sub_turn 超过该模式 max_sub_turns 后，最后一次模型调用前注入
"轮次已达上限"描述并清空 tools，让模型生成收尾回复（不再调工具）。

yield 事件：
    ("reply", sentence, emotion)    最终回复逐句
    ("sub_turn", n)                 每个 sub_turn 开始
    ("tool", name, call_id, args)   工具开始执行
    ("compacted", count)            完成一次压缩
    ("done", outcome)               run 结束
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

from agent_config import get_mode_config, history_budget_tokens
from session_store import SessionStore
from session_store import _estimate_tokens
from context_builder import build_model_context
from compaction import (
    CompactionState,
    prepare_compaction,
    generate_checkpoint_summary,
    validate_checkpoint_summary,
)
from tools.loader import build_tools_list, get_execution_mode, execute_tool
from providers.llm import SENTENCE_ENDS, _EMOTION_RE, strip_emotion_tags

MAX_PARALLEL_TOOL_CALLS = 2

_LIMIT_HINT = (
    "（系统提示：你已经达到本次允许的最大模型调用轮次，必须停止调用任何工具，"
    "直接用一段话总结当前进展和结论，让对话在此收尾。）"
)


def _new_run_id() -> str:
    return uuid.uuid4().hex[:8]


def _normalize_tool_calls(tool_calls) -> list:
    """OpenAI tool_calls 对象/字典 → [(call_id, name, args_dict)]。"""
    out = []
    for tc in tool_calls:
        if isinstance(tc, dict):
            call_id = tc.get("id")
            fn = tc.get("function", {})
            name = fn.get("name") if isinstance(fn, dict) else getattr(fn, "name", None)
            raw = fn.get("arguments") if isinstance(fn, dict) else getattr(fn, "arguments", "{}")
        else:
            call_id = tc.id
            name = tc.function.name
            raw = tc.function.arguments
        if call_id and name:
            try:
                args = json.loads(raw or "{}")
            except (ValueError, TypeError, json.JSONDecodeError):
                args = {"_raw": raw}
            if not isinstance(args, dict):
                args = {"_value": args}
            out.append((call_id, name, args))
    return out


def _tool_calls_serializable(tool_calls) -> list:
    raw = []
    for tc in tool_calls:
        if isinstance(tc, dict):
            raw.append(tc)
            continue
        raw.append({
            "id": tc.id,
            "type": "function",
            "function": {"name": tc.function.name, "arguments": tc.function.arguments},
        })
    return raw


async def _execute_tool_calls(calls, mode) -> list:
    """calls: [(call_id, name, args)] → [(call_id, result_text)]，按原始顺序。

    并行规则（参考 tool_scheduler）：连续 PARALLEL_READONLY 攒批 asyncio.gather；
    SEQUENTIAL（副作用）为分隔点单独执行。结果按模型原始调用顺序回填。
    """
    n = len(calls)
    results = [None] * n
    i = 0
    while i < n:
        _, name, _ = calls[i]
        if get_execution_mode(name) == "SEQUENTIAL":
            cid, _, args = calls[i]
            results[i] = (cid, await execute_tool(name, args, mode))
            i += 1
            continue
        j = i
        batch = []
        while j < n:
            _, n2, _ = calls[j]
            if get_execution_mode(n2) != "PARALLEL_READONLY":
                break
            batch.append(calls[j])
            j += 1
        maxw = min(MAX_PARALLEL_TOOL_CALLS, len(batch))
        sem = asyncio.Semaphore(maxw)

        async def _one(c):
            async with sem:
                return await execute_tool(c[1], c[2], mode)

        tasks = [_one(c) for c in batch]
        texts = await asyncio.gather(*tasks)
        for k, (c, t) in enumerate(zip(batch, texts)):
            results[i + k] = (c[0], t)
        i = j
    return results


async def _stream_final_sentences(stream, t_start):
    """最终回复流式切句（复用 llm 切句标点/情绪标签），yield (sentence, emotion)。"""
    buffer = ""
    emotion = "平静"
    emotion_parsed = False
    async for chunk in stream:
        delta = getattr(chunk.choices[0].delta, "content", None)
        if not delta:
            continue
        buffer += delta
        if not emotion_parsed:
            m = _EMOTION_RE.search(buffer)
            if m:
                emotion = m.group(1)
                buffer = buffer.replace(m.group(0), "", 1)
                emotion_parsed = True
        while True:
            cut = -1
            for idx, ch in enumerate(buffer):
                if ch in SENTENCE_ENDS:
                    cut = idx
                    break
            if cut == -1:
                break
            sentence = buffer[:cut + 1].strip()
            buffer = buffer[cut + 1:]
            if sentence:
                yield strip_emotion_tags(sentence), emotion
            if buffer == "":
                break
    if buffer.strip():
        yield strip_emotion_tags(buffer), emotion


def _v2_memory_text(memory_fs, memory_max_tokens):
    """v2 扁平记忆注入文本（MEMORY.md + 昨日日志，预算内）。memory_fs 为 None 或不可用时返回 None。"""
    if memory_fs is None:
        return None
    try:
        return memory_fs.build_inject_text(max_tokens=memory_max_tokens or 1800)
    except Exception as e:
        print(f"[memory] 记忆注入文本构建失败(降级为空): {e}")
        return None


async def _persist_after_compaction(memory_fs, summary: str):
    """压缩提交后的低层记忆持久化（design 2.5，异步不阻塞主流程）。

    把检查点摘要沉淀进每日日志 + 对话存档 + MEMORY.md。文件 IO 小且快，
    作为独立 task 调度，不阻塞 agent 环继续回复。
    """
    try:
        import datetime as _dt
        today = _dt.date.today().isoformat()
        lines = [ln.strip() for ln in (summary or "").splitlines() if ln.strip()]

        # 解析五层字段：标题行后紧跟的若干非标题行为其取值（goal/constraints/...）
        heads = {"goal", "constraints", "progress", "keydecision", "nextsteps"}
        def _heading_values():
            out = []
            for i, ln in enumerate(lines):
                if ln in heads and i + 1 < len(lines) and lines[i + 1] not in heads:
                    out.append(lines[i + 1])
            return out

        # 1) dialog/YYYY-MM-DD.json：新增一条压缩检查点条目
        try:
            memory_fs.upsert_dialog({
                "id": f"compaction-{int(__import__('time').time() * 1000)}",
                "kind": "memory_compact",
                "date": today,
                "summary": (summary or "")[:800],
            })
        except Exception as e:
            print(f"[memory] dialog 落盘失败(不阻塞): {e}")
        # 2) memory/YYYY-MM-DD.md：追加当日关键点（取各字段取值）
        facts = [v for v in _heading_values() if v]
        if facts:
            try:
                memory_fs.append_daily_md("；".join(facts[:8])[:300])
            except Exception as e:
                print(f"[memory] 每日日志落盘失败(不阻塞): {e}")
        # 3) MEMORY.md：沉淀关键决策/下一步（长期事务/知识主干）
        keep_heads = {"keydecision", "nextsteps"}
        for i, ln in enumerate(lines):
            if ln in keep_heads and i + 1 < len(lines) and lines[i + 1] not in heads:
                try:
                    memory_fs.append_memory_md(f"{ln}{lines[i + 1]}"[:200])
                except Exception as e:
                    print(f"[memory] MEMORY.md 落盘失败(不阻塞): {e}")
    except Exception as e:
        print(f"[memory] 压缩后记忆持久化失败(不阻塞): {e}")


async def run_agent_loop(
    client,
    model,
    mode,
    system_prompt,
    session: SessionStore,
    *,
    run_id: str | None = None,
    user_profile=None,
    compaction_state=None,
    summarizer=None,
    on_tool=None,
    config=None,
    memory_store=None,
    memory_fs=None,
):
    """执行一次完整 run（多 sub_turn 原生 function calling）。

    参数:
        client: AsyncOpenAI
        model: 模型名
        mode: 当前模式（chat/work）
        system_prompt: 当前模式系统提示词
        session: 会话层（本 run 的 assistant tool_calls / tool 结果 / 由本函数写入；
                 用户输入需调用方在 run 前 add，并传入同一个 run_id 保持可追溯）
        run_id: 本次 run 的 id（调用方生成以保证与用户消息一致的 traceability；
                缺省自动生成）
        user_profile: 可选用户档案
        compaction_state: 跨 run 压缩检查点（可复用）
        summarizer: async (prompt_text)->str，压缩摘要模型回调；None 则不压缩
        on_tool: async (stage, name, call_id, text) 进度回调
        config: 可选 ModeAgentConfig 覆盖（默认按 mode 查 get_mode_config，测试/特殊场景用）
        memory_fs: 可选 MemoryFs 实例；压缩提交后异步持久化每日日志/对话/MEMORY.md
                   （design 2.5，异步不阻塞主流程）

    yield 事件见模块 docstring。
    """
    config = config or get_mode_config(mode)
    run_id = run_id or _new_run_id()
    compaction_state = compaction_state or CompactionState()
    tools = build_tools_list(mode)
    sub_turn = 1
    over_limit = False
    transcript = session.transcript()

    while True:
        yield ("sub_turn", sub_turn)

        # ── 1) 从会话层重建上下文（含此前 sub_turn 的工具结果/压缩检查点/记忆）──
        transcript = session.transcript()
        ctx = build_model_context(
            system_prompt, transcript, config,
            user_profile=user_profile,
            checkpoint_summary=compaction_state.summary,
            # v2 扁平文件记忆优先（MEMORY.md+昨日）；无 memory_fs 时回退 v1 分层
            memory_blocks=memory_store.recall_blocks() if (memory_store and memory_fs is None) else None,
            memory_text=_v2_memory_text(memory_fs, None),
        )

        # ── 2) 压缩/上下文拆分：超出 check_context 预算（design 2.2）→ 压旧完整轮 ──
        _budget = config.compaction_threshold
        _over_budget = False
        if summarizer is not None:
            sys_tokens = sum(_estimate_tokens(m.get("content", "")) for m in ctx.model_context
                             if m.get("role") == "system")
            summary_tokens = _estimate_tokens(compaction_state.summary) if compaction_state.summary else 0
            _budget = history_budget_tokens(sys_tokens, summary_tokens)
            hist_tokens = sum(_estimate_tokens(m.get("content", "")) for m in ctx.model_context
                              if m.get("role") != "system")
            _over_budget = (hist_tokens >= _budget
                            or ctx.estimated_tokens >= config.compaction_threshold)
        if summarizer is not None and _over_budget:
            dec = prepare_compaction(
                transcript, config, compaction_state,
                threshold=min(_budget, config.compaction_threshold),
            )
            if dec.should_compact and dec.messages_to_summarize:
                try:
                    summary = await generate_checkpoint_summary(
                        summarizer, dec.messages_to_summarize, compaction_state.summary
                    )
                    valid, _ = validate_checkpoint_summary(summary)
                    if valid:
                        # 更新检查点边界：<first_kept_turn_index> 由保留轮推导
                        compaction_state.commit(
                            summary=summary,
                            first_kept_turn_index=len(transcript) - len(dec.retained_turn_views),
                            first_kept_message_index=0,
                        )
                        yield ("compacted", compaction_state.compaction_count)
                        # 压缩后异步持久化记忆（design 2.5，不阻塞主流程）
                        if memory_fs is not None:
                            try:
                                asyncio.create_task(_persist_after_compaction(memory_fs, summary))
                            except Exception as e:
                                print(f"[memory] 调度压缩后持久化失败: {e}")
                        # 用新检查点重建上下文
                        ctx = build_model_context(
                            system_prompt, transcript, config,
                            user_profile=user_profile,
                            checkpoint_summary=compaction_state.summary,
                            memory_blocks=memory_store.recall_blocks() if (memory_store and memory_fs is None) else None,
                            memory_text=_v2_memory_text(memory_fs, None),
                        )
                except Exception as e:
                    print(f"[agent_runtime] 压缩失败（不丢历史，继续）: {e}")

        # ── 3) 降级/正常构建本轮请求 ──
        messages = list(ctx.model_context)
        if sub_turn > config.max_sub_turns:
            over_limit = True
            messages.append({"role": "user", "content": _LIMIT_HINT})
            effective_tools = []
        else:
            effective_tools = tools

        kwargs = dict(model=model, messages=messages, temperature=0.9, max_tokens=15000, stream=False)
        if effective_tools:
            kwargs["tools"] = effective_tools

        resp = await client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        content = msg.content or ""
        tool_calls = msg.tool_calls

        if tool_calls:
            session.add("assistant", content or "", run_id=run_id, sub_turn=sub_turn,
                        tool_calls=_tool_calls_serializable(tool_calls))
            normalized = _normalize_tool_calls(tool_calls)
            if not normalized:
                break
            for cid, name, args in normalized:
                yield ("tool", name, cid, args)
                if on_tool:
                    await on_tool("start", name, cid, args)
            if len(normalized) > MAX_PARALLEL_TOOL_CALLS * 4:
                # 防御：单轮工具调用过多
                pass
            executed = await _execute_tool_calls(normalized, mode)
            for cid, result in executed:
                session.add("tool", result, run_id=run_id, sub_turn=sub_turn, tool_call_id=cid)
            sub_turn += 1
            continue

        # ── 4) 无工具调用 → 最终回复（流式逐句）──
        stream = await client.chat.completions.create(
            model=model, messages=messages, temperature=0.9, max_tokens=15000,
            tools=effective_tools or None, stream=True,
        )
        async for sentence, emo in _stream_final_sentences(stream, time.time()):
            yield ("reply", sentence, emo)
        break

    yield ("done", "max_turns" if over_limit else "completed")
