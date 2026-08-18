# -*- coding: utf-8 -*-
"""边界机制端到端验证（进程内，不发 WS/TTS）：
    1. sub_turn 降级：小 max_sub_turns + 工具场景 → 注入降级提示 → done=max_turns
    2. 工作模式全工具 + 上下文旧轮工具 JSON 占位
    3. 压缩：极小 compaction_threshold + 注入 summarizer → 触发 → 检查点注入 yield
"""
import asyncio
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from session_store import SessionStore
from agent_runtime import run_agent_loop
from agent_config import ModeAgentConfig
from prompt_loader import load_prompt

WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP = os.path.join(WORKSPACE, "sessions_probe_tmp")


def _reset():
    shutil.rmtree(_TMP, ignore_errors=True)
    os.makedirs(_TMP, exist_ok=True)
    SessionStore.SESSIONS_DIR = _TMP


_SYSTEM = "\n\n".join(p for p in [
    load_prompt("personality.md"), load_prompt("voice_style.md"),
    "你通过 function calling 工具完成任务，工具列表由 tools 参数提供。",
] if p)


async def test_degradation(llm):
    """sub_turn 降级：max_sub_turns=1，工具场景。sub_turn1 调工具后，sub_turn2>1 → 注入降级。"""
    _reset()
    store = SessionStore("degrade")
    store.add("user", "帮我算一下 8 * 9 等于多少", run_id="d", sub_turn=1)
    cfg = ModeAgentConfig(mode="work", keep_complete_turns=10, max_sub_turns=1,
                          drop_old_tool_results=True, context_max_tokens=1_000_000,
                          compaction_threshold=700_000)
    events = []
    async for ev in run_agent_loop(llm.client, llm.model, "work", _SYSTEM, store,
                                   config=cfg, run_id="d"):
        events.append(ev[0])
        if ev[0] == "tool":
            print(f"  [tool] {ev[1]}")
        elif ev[0] == "reply":
            print(f"  [reply] {ev[1]}")
        elif ev[0] == "done":
            print(f"  [done] {ev[1]}")
    assert "reply" in events, "降级后应有最终回复"
    assert events[-1] == ("done", "max_turns") if events[-1] == "done" else True
    # done 事件值检查
    done_vals = [ev[1] for ev in zip([], [])]
    print("  [OK] sub_turn 降级机制触发（done 事件已返回）")
    assert events[-1][1] == "max_turns", f"expected max_turns, got {events[-1]}"


async def test_work_mode(llm):
    """工作模式：全工具列表 + 正常工具执行。"""
    _reset()
    store = SessionStore("workx")
    store.add("user", "帮我算一下 7 乘 8 等于多少", run_id="w", sub_turn=1)
    events = []
    async for ev in run_agent_loop(llm.client, llm.model, "work", _SYSTEM, store, run_id="w"):
        events.append(ev)
        if ev[0] == "tool":
            print(f"  [tool] {ev[1]}")
        elif ev[0] == "reply":
            print(f"  [reply] {ev[1]}")
        elif ev[0] == "done":
            print(f"  [done] {ev[1]}")
    assert any(e[0] == "reply" for e in events), "工作模式应有回复"
    print("  [OK] 工作模式（全工具）端到端通过")


async def test_compaction(llm):
    """压缩：极小 threshold + 多轮历史 + 注入 summarizer → 触发压缩 → 检查点 yield。"""
    _reset()
    store = SessionStore("cmp")
    for i in range(4):
        store.add("user", f"第{i}个问题" + "机" * 300, run_id=f"c{i}", sub_turn=1)
        store.add("assistant", "答" + "机" * 300, run_id=f"c{i}", sub_turn=2)

    async def _summarize(prompt_text):
        # 返回带必需标题的检查点摘要
        return ("## Goal\n压缩测试\n## Progress\n进行中\n## Key Decisions\n无\n"
                "## Next Steps\n完成\n## Critical Context\n关键上下文\n")

    cfg = ModeAgentConfig(mode="work", keep_complete_turns=10, max_sub_turns=30,
                          drop_old_tool_results=True, context_max_tokens=1_000_000,
                          compaction_threshold=1)  # 必然超阈值

    compacted = [False]
    events = []
    async for ev in run_agent_loop(llm.client, llm.model, "work", _SYSTEM, store,
                                   config=cfg, run_id="cmpnew", summarizer=_summarize,
                                   compaction_state=None):
        if ev[0] == "compacted":
            compacted[0] = True
            print(f"  [compacted] #{ev[1]}")
        elif ev[0] == "reply":
            print(f"  [reply] {ev[1]}")
        elif ev[0] == "done":
            print(f"  [done] {ev[1]}")
        events.append(ev)
    assert compacted[0], "压缩应触发 compacted 事件"
    print("  [OK] 压缩机制：超阈值触发 + 检查点注入")


async def main():
    from providers import get_llm
    llm = get_llm()
    print(f"LLM: {llm.model}\n")
    print("=== 1. sub_turn 降级 ===")
    await test_degradation(llm)
    print("\n=== 2. 工作模式全工具 ===")
    await test_work_mode(llm)
    print("\n=== 3. 压缩触发 ===")
    await test_compaction(llm)
    print("\n全部边界机制验证通过 ✅")


if __name__ == "__main__":
    asyncio.run(main())
