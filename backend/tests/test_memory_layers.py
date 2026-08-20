# -*- coding: utf-8 -*-
"""记忆层间流动单测：会话结束抽取 → L1 → L2 沉淀 → L3 聚合（mock，不连 LLM）。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory_store import MemoryStore, LAYER_L1, LAYER_L2, LAYER_L3
from memory_extractor import MemoryExtractor


class _Cfg:
    enabled = True
    session_idle_timeout_s = 5
    session_archive_text_threshold = 100
    memory_max_tokens = 200
    l1_max_entries = 20
    l2_max_entries = 20
    l2_consolidate_every_n_sessions = 2   # 每 2 次会话沉淀一次 L1→L2
    l3_rebuild_every_n_consolidations = 2  # 每 2 次沉淀重写一次 L3
    tool_chat_enabled = True


def _store_with_ext(tmp):
    store = MemoryStore(memories_dir=tmp, config=_Cfg())

    async def fake_sum(messages):
        # 取 user 消息内容判断任务
        user = messages[-1]["content"] if messages else ""
        # 会话抽取：返回该会话要记的事件
        if "conversation" in user:
            return '[{"text":"用户说下周三去上海出差","category":"event"}]'
        # L3 聚合
        return "用户画像是后端工程师，喜欢咖啡。"

    return store, MemoryExtractor(store, summarizer=fake_sum, config=_Cfg())


async def test_on_session_end_writes_l1_every_session(tmp_path):
    store, ext = _store_with_ext(tmp_path)
    # 第一次会话结束
    n1 = await ext.on_session_end([("user", "下周三我要去上海")], {"session": "s1"})
    assert n1 == 1
    assert len(store.l1_entries()) == 1


async def test_consolidate_on_throttle(tmp_path):
    store, ext = _store_with_ext(tmp_path)
    # 第 1 次会话：只抽 L1（不沉淀，因 1%2 !=0）
    await ext.on_session_end([("user", "A")], {"session": "s1"})
    assert len(store.l1_entries()) == 1 and len(store.l2_entries()) == 0
    # 第 2 次会话：抽 L1 + 触发沉淀（2%2==0）→ L1 清空、进 L2
    await ext.on_session_end([("user", "B")], {"session": "s2"})
    assert len(store.l2_entries()) >= 1


async def test_rebuild_l3_on_throttle(tmp_path):
    store, ext = _store_with_ext(tmp_path)
    for i in range(4):  # 4 次会话 → 2 次沉淀 → 第 4 次触发 L3 重建
        await ext.on_session_end([("user", f"会话{i}")], {"session": f"s{i}"})
    assert store.l3_narrative()  # L3 已被聚合




async def test_consolidate_direct(tmp_path):
    # 直接测 consolidate：L1 与 L2 同义 → merge；不同 → new
    store, ext = _store_with_ext(tmp_path)
    store.add_l2("用户喜欢喝咖啡", "preference")
    store.add_l1("用户喜欢喝咖啡", "event")   # 与 L2 一致 → merge
    store.add_l1("用户养了一只猫", "fact")      # 新 → new
    n = store.consolidate(ext)
    # 100% 由 classify 规则：一条 merge（不新增）+ 一条 new → 新增 1
    assert n == 1
    # L1 清空
    assert len(store.l1_entries()) == 0
    # L2 新增了"养猫"
    assert any("猫" in it["text"] for it in store.l2_entries())


async def test_clear_and_rebuild(tmp_path):
    store, _ = _store_with_ext(tmp_path)
    store.add_l2("事实A", "fact")
    store.set_l3("旧画像")
    store.clear()
    assert len(store.l2_entries()) == 0 and store.l3_narrative() is None


if __name__ == "__main__":
    import asyncio
    ok = 0
    with tempfile.TemporaryDirectory(prefix="memory_layers_") as d:
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                c = os.path.join(d, name)
                os.makedirs(c, exist_ok=True)
                asyncio.run(fn(c))
                ok += 1
                print(f"  ok {name}")
    print(f"\n{ok} passed")
