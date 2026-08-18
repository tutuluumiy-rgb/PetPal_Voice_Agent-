# -*- coding: utf-8 -*-
"""端到端：run_agent_loop（原生 function calling + 多 sub_turn + 最终流式 + 会话持久化）

用真实 qwen LLM，不发 WS/TTS。验证：
  1. 原生 tool_calls 触发（输入含计算 → 应调 calculator）
  2. 多 sub_turn（工具轮 + 最终回复轮）
  3. 最终回复流式逐句 yield ("reply", ...)
  4. 会话层把 assistant(tool_calls) 与 tool 结果按 id 记录
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from session_store import SessionStore
from agent_runtime import run_agent_loop
from prompt_loader import load_prompt


async def main():
    from providers import get_llm

    llm = get_llm()
    client = llm.client
    model = llm.model
    print(f"LLM: {model}")

    # 原生 function calling 专用 system prompt（不带文本 TOOL_CALL 声明）
    system_prompt = "\n\n".join(p for p in [
        load_prompt("personality.md"),
        load_prompt("voice_style.md"),
        "你通过系统提供的 function calling 工具完成任务，工具列表由 tools 参数提供。",
    ] if p)

    store = SessionStore("probe_run1")
    store.add("user", "帮我算一下 123 * 456 等于多少", run_id="probe_run1", sub_turn=1)

    print("\n=== 开始 run ===")
    async for ev in run_agent_loop(
        client, model, mode="work", system_prompt=system_prompt, session=store,
    ):
        kind = ev[0]
        if kind == "sub_turn":
            print(f"[sub_turn] {ev[1]}")
        elif kind == "tool":
            print(f"[tool] {ev[1]} call_id={ev[2]} args={ev[3]}")
        elif kind == "reply":
            print(f"[reply] {ev[1]}")
        elif kind == "compacted":
            print(f"[compacted] #{ev[1]}")
        elif kind == "done":
            print(f"[done] outcome={ev[1]}")

    print("\n=== 会话层记录（可追溯）===")
    for m in store.all():
        tag = m.get("role")
        if tag == "assistant" and m.get("tool_calls"):
            print(f"  assistant[sub{m.get('sub_turn')}] tool_calls={[tc.get('function',{}).get('name') for tc in m['tool_calls']]} hooks={m.get('id')}")
        elif tag == "tool":
            print(f"  tool[sub{m.get('sub_turn')}] → {m.get('content','')[:40]!r} id={m.get('tool_call_id')}")
        elif tag == "user":
            print(f"  user[sub{m.get('sub_turn')}] {m.get('content','')[:40]!r}")
        else:
            print(f"  assistant[sub{m.get('sub_turn')}] {m.get('content','')[:40]!r}")

    # 校验：tool call 与其 result 都写入了会话，且 tool_call_id 对应
    tool_ids = [m.get("tool_call_id") for m in store.all() if m.get("role") == "tool"]
    call_ids = [
        tc.get("id")
        for m in store.all() if m.get("role") == "assistant" and m.get("tool_calls")
        for tc in m["tool_calls"]
    ]
    print(f"\ntool_call_ids(模型发出) = {call_ids}")
    print(f"tool_result_ids(会话回填) = {tool_ids}")
    assert set(call_ids) == set(tool_ids) and call_ids, "tool call 与 result 的 id 必须一一对应"
    print("✅ 工具调用与结果按 id 正确配对")


if __name__ == "__main__":
    asyncio.run(main())
