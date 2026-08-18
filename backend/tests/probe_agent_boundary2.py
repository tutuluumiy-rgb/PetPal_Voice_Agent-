# -*- coding: utf-8 -*-
"""补充边界验证（进程内，不发 WS/TTS）：
    A. 工作模式全工具列表 + 工具执行
    B. 压缩触发（极小 threshold + 注入 summarizer → compacted 事件 + 检查点）
"""
import asyncio
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from session_store import SessionStore
from agent_runtime import run_agent_loop
from agent_config import ModeAgentConfig
from tools.loader import build_tools_list, get_tool_names
from prompt_loader import load_prompt

WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP = os.path.join(WORKSPACE, "sessions_probe_tmp")

_SYSTEM = "\n\n".join(p for p in [
    load_prompt("personality.md"), load_prompt("voice_style.md"),
    "你通过 function calling 工具完成任务，工具列表由 tools 参数提供。",
] if p)


def _reset():
    shutil.rmtree(_TMP, ignore_errors=True)
    os.makedirs(_TMP, exist_ok=True)
    SessionStore.SESSIONS_DIR = _TMP


async def test_work_mode(llm):
    print("-- A. 工作模式全工具 --")
    _reset()
    # 验证工具列表：工作模式应包含 bash/write/edit/read/ask 等全量工具
    tools_work = build_tools_list("work")
    names_work = {t["function"]["name"] for t in tools_work}
    print(f"  工作模式工具数={len(tools_work)}: {sorted(names_work)}")
    assert "bash" in names_work and "write" in names_work and "read" in names_work
    # 闲聊应只 3 个
    names_chat = {t["function"]["name"] for t in build_tools_list("chat")}
    print(f"  闲聊模式工具数={len(names_chat)}: {sorted(names_chat)}")
    assert names_chat == {"web_search", "read", "calculator"}

    store = SessionStore("workx")
    store.add("user", "帮我算一下 7 乘 8 等于多少", run_id="w", sub_turn=1)
    replied = False
    async for ev in run_agent_loop(llm.client, llm.model, "work", _SYSTEM, store, run_id="w"):
        if ev[0] == "tool":
            print(f"  [tool] {ev[1]}")
        elif ev[0] == "reply":
            replied = True
            print(f"  [reply] {ev[1]}")
        elif ev[0] == "done":
            print(f"  [done] {ev[1]}")
    assert replied, "工作模式应有回复"
    print("  [OK] 工作模式全工具 + 工具执行通过\n")


async def test_compaction(llm):
    print("-- B. 压缩触发 --")
    _reset()
    store = SessionStore("cmp")
    for i in range(4):
        store.add("user", f"第{i}个问题" + "机" * 300, run_id=f"c{i}", sub_turn=1)
        store.add("assistant", "答" + "机" * 300, run_id=f"c{i}", sub_turn=2)

    async def _summarize(prompt_text):
        # 返回带必需标题的检查点摘要（不依赖真实 LLM 摘要）
        return ("## Goal\n压缩测试\n## Progress\n进行中\n## Key Decisions\n无\n"
                "## Next Steps\n完成\n## Critical Context\n关键上下文\n")

    cfg = ModeAgentConfig(mode="work", keep_complete_turns=1, max_sub_turns=30,
                          drop_old_tool_results=True, context_max_tokens=1_000_000,
                          compaction_threshold=1)  # 必然超阈值 → 触发压缩

    compacted = [False]
    async for ev in run_agent_loop(llm.client, llm.model, "work", _SYSTEM, store,
                                   config=cfg, run_id="cmpnew", summarizer=_summarize):
        if ev[0] == "compacted":
            compacted[0] = True
            print(f"  [compacted] #{ev[1]}")
        elif ev[0] == "reply":
            print(f"  [reply] {ev[1][:20]}...")
        elif ev[0] == "done":
            print(f"  [done] {ev[1]}")
    assert compacted[0], "压缩应触发 compacted 事件"
    print("  [OK] 压缩触发 + 检查点注入通过\n")


async def main():
    from providers import get_llm
    llm = get_llm()
    print(f"LLM: {llm.model}\n")
    await test_work_mode(llm)
    await test_compaction(llm)
    print("补充验证完成 ✅")


if __name__ == "__main__":
    asyncio.run(main())
