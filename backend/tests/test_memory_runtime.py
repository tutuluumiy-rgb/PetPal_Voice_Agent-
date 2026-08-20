# -*- coding: utf-8 -*-
"""记忆与 agent_runtime 集成单测：带 memory_store 驱动一次 run 不崩且记忆被注入。

mock LLM client（无工具调用→直接最终回复），不连真 API/网络。
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from types import SimpleNamespace

from agent_config import get_mode_config
from session_store import SessionStore
from compaction import CompactionState
from memory_store import MemoryStore
from agent_runtime import run_agent_loop

CHAT = "chat"


class _Cfg:
    enabled = True
    session_idle_timeout_s = 5
    session_archive_text_threshold = 100
    memory_max_tokens = 200
    l1_max_entries = 20
    l2_max_entries = 20
    l2_consolidate_every_n_sessions = 2
    l3_rebuild_every_n_consolidations = 2
    tool_chat_enabled = True


def _fake_client(with_tool=False):
    """构造一个假 AsyncOpenAI：支持流式/非流式两种 create。"""
    calls = {"n": 0}

    async def _stream():
        # 模拟流式 chunk："你好呀" 逐段（无 tool_calls）
        for piece in ["你好", "呀～"]:
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=piece))])

    class _Completions:
        async def create(self, **kwargs):
            calls["n"] += 1
            if kwargs.get("stream"):
                return _stream()
            # 非流式：直接最终回复（无工具调用）
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="你好呀～", tool_calls=None
            ))])

    client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
    return client, calls


async def test_run_with_memory_store(tmp_path):
    s = SessionStore()
    mem = MemoryStore(memories_dir=tmp_path, config=_Cfg())
    mem.add_l2("用户叫小陈", "identity")  # 预置一条记忆，验证注入不崩
    client, calls = _fake_client()
    cfg = get_mode_config(CHAT)

    events = []
    async for ev in run_agent_loop(
        client, "mock-model", CHAT, "SYSTEM", s,
        run_id="r1", memory_store=mem, compaction_state=CompactionState(),
    ):
        events.append(ev)

    # 至少发生了 sub_turn 与 done
    assert any(e[0] == "sub_turn" for e in events)
    assert any(e[0] == "done" for e in events)
    # LLM 被调用过
    assert calls["n"] >= 1


async def test_run_without_memory_store(tmp_path):
    # 不传 memory_store → 兼容旧路径，不崩
    s = SessionStore()
    client, calls = _fake_client()
    cfg = get_mode_config(CHAT)
    events = []
    async for ev in run_agent_loop(client, "mock-model", CHAT, "SYSTEM", s,
                                   run_id="r2", compaction_state=CompactionState()):
        events.append(ev)
    assert any(e[0] == "done" for e in events)


if __name__ == "__main__":
    ok = 0
    with tempfile.TemporaryDirectory(prefix="memory_rt_") as d:
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                c = os.path.join(d, name)
                os.makedirs(c, exist_ok=True)
                asyncio.run(fn(c))
                ok += 1
                print(f"  ok {name}")
    print(f"\n{ok} passed")
