# -*- coding: utf-8 -*-
"""记忆抽取器与主动写入单测（mock summarizer，不连 LLM/网络）。

覆盖：会话→L1 抽取、主动写入 memory_add 落层、L1→L2 classify 规则、L3 聚合。
"""
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
    l2_consolidate_every_n_sessions = 2
    l3_rebuild_every_n_consolidations = 2
    tool_chat_enabled = True


async def test_extract_session_writes_l1(tmp_path):
    store = MemoryStore(memories_dir=tmp_path, config=_Cfg())

    async def fake_sum(messages):
        return '[{"text":"用户下周三要出差去上海","category":"event"}]'

    ext = MemoryExtractor(store, summarizer=fake_sum, config=_Cfg())
    n = await ext.extract_session([("user", "下周三我要去上海出差")], {"run": "r1"})
    assert len(n) == 1
    l1 = store.l1_entries()
    assert len(l1) == 1 and l1[0]["layer"] == LAYER_L1
    assert "上海" in l1[0]["text"]


async def test_extract_session_empty_disabled(tmp_path):
    store = MemoryStore(memories_dir=tmp_path, config=_Cfg())

    # summarizer 为 None → enabled=False，不抽取
    ext = MemoryExtractor(store, summarizer=None, config=_Cfg())
    n = await ext.extract_session([("user", "嗨")], {})
    assert n == [] and len(store.l1_entries()) == 0


async def test_active_memory_add_layers(tmp_path):
    store = MemoryStore(memories_dir=tmp_path, config=_Cfg())
    ext = MemoryExtractor(store, config=_Cfg())

    # identity/preference → L2
    ext.memory_add("用户叫小陈", "identity")
    ext.memory_add("用户喜欢咖啡", "preference")
    assert len(store.l2_entries()) == 2
    # goal 也落 L2（L3 是聚合产物）
    ext.memory_add("用户长期目标是要做一款语音助手", "goal")
    assert len(store.l2_entries()) == 3
    # event → L1
    ext.memory_add("昨天用户去爬山了", "event")
    assert len(store.l1_entries()) == 1
    # 非法 category 回退 fact → L2
    ext.memory_add("某条杂项", "whatever")
    assert len(store.l2_entries()) == 4


async def test_classify_event_rule(tmp_path):
    store = MemoryStore(memories_dir=tmp_path, config=_Cfg())
    ext = MemoryExtractor(store, config=_Cfg())
    store.add_l2("用户喜欢喝咖啡", "preference")
    # 已有事实 → merge
    assert ext.classify_event({"text": "用户喜欢喝咖啡", "id": "e1"}).startswith("merge:")
    # 新事实 → new
    assert ext.classify_event({"text": "用户喜欢品红酒", "id": "e2"}) == "new"


async def test_build_l3_narrative(tmp_path):
    store = MemoryStore(memories_dir=tmp_path, config=_Cfg())
    store.add_l2("用户叫小陈", "identity")
    store.add_l2("用户是后端工程师", "fact")

    async def fake_sum(messages):
        return "用户叫小陈，是一位后端工程师。"

    ext = MemoryExtractor(store, summarizer=fake_sum, config=_Cfg())
    nar = await ext.build_l3_narrative()
    assert nar and "小陈" in nar
    store.set_l3(nar)
    assert store.l3_narrative() == "用户叫小陈，是一位后端工程师。"


async def test_bind_memory_tools(tmp_path):
    from tools import memory as mt
    store = MemoryStore(memories_dir=tmp_path, config=_Cfg())
    ext = MemoryExtractor(store, config=_Cfg())
    mt.bind_memory(store, ext)
    r = mt.memory_add("用户喜欢蓝色", "preference")
    assert r.startswith("已记住")
    r2 = mt.memory_forget("蓝色")
    assert "已忘记" in r2
    assert len(store.l2_entries()) == 0


if __name__ == "__main__":
    import asyncio
    ok = 0
    with tempfile.TemporaryDirectory(prefix="memory_ext_test_") as d:
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                c = os.path.join(d, name)
                os.makedirs(c, exist_ok=True)
                asyncio.run(fn(c))
                ok += 1
                print(f"  ok {name}")
    print(f"\n{ok} passed")
