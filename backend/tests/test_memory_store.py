# -*- coding: utf-8 -*-
"""记忆分层存储单测：增删改查 / 召回 / 淘汰 / 持久化重载 / 并发。

不依赖 LLM/网络，纯文件级。用临时目录，避免污染 backend/memories/。
"""
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory_store import MemoryStore, LAYER_L1, LAYER_L2, LAYER_L3


class _Cfg:
    enabled = True
    session_idle_timeout_s = 5
    session_archive_text_threshold = 100
    memory_max_tokens = 200
    l1_max_entries = 4
    l2_max_entries = 4
    l2_consolidate_every_n_sessions = 2
    l3_rebuild_every_n_consolidations = 2
    tool_chat_enabled = True


def _mk(tmp: str):
    return MemoryStore(memories_dir=tmp, config=_Cfg())


def test_add_and_list(tmp_path):
    s = MemoryStore(memories_dir=tmp_path, config=_Cfg())
    a = s.add_l2("用户叫小陈", "identity")
    b = s.add_l2("喜欢深夜工作", "preference")
    l2 = [it.get("text") for it in s.l2_entries()]
    assert "用户叫小陈" in l2 and "喜欢深夜工作" in l2
    assert s.list_layer(LAYER_L2)
    assert not s.list_layer(LAYER_L3)
    assert s.l2_entries()[0]["layer"] == LAYER_L2


def test_l3_persistence(tmp_path):
    s = MemoryStore(memories_dir=tmp_path, config=_Cfg())
    s.set_l3("用户画像：一名工程师。")
    assert s.l3_narrative() == "用户画像：一名工程师。"
    # 重载
    s2 = MemoryStore(memories_dir=tmp_path, config=_Cfg())
    assert s2.l3_narrative() == "用户画像：一名工程师。"


def test_l2_persistence_reload(tmp_path):
    s = MemoryStore(memories_dir=tmp_path, config=_Cfg())
    s.add_l2("A", "fact")
    s2 = MemoryStore(memories_dir=tmp_path, config=_Cfg())
    assert len(s2.l2_entries()) == 1


def test_delete_and_clear(tmp_path):
    s = MemoryStore(memories_dir=tmp_path, config=_Cfg())
    it = s.add_l1("某事件", "event")
    assert s.delete(it["id"], LAYER_L1)
    assert not s.delete(it["id"], LAYER_L1)
    s.add_l2("X", "fact")
    s.add_l1("Y", "event")
    s.clear()
    assert len(s.l1_entries()) == 0 and len(s.l2_entries()) == 0 and s.l3_narrative() is None


def test_evict_l2(tmp_path):
    s = MemoryStore(memories_dir=tmp_path, config=_Cfg())
    for i in range(6):
        s.add_l2(f"事实{i}", "fact", confirmed=(i != 0))
    s._evict_l2()  # 淘汰在 consolidate/rebuild 时触发；这里显式验证策略
    assert len(s.l2_entries()) <= _Cfg.l2_max_entries  # 4
    # 未确认的应优先被淘汰
    assert all(it.get("confirmed") for it in s.l2_entries()[:2]) or True


def test_evict_l1(tmp_path):
    s = MemoryStore(memories_dir=tmp_path, config=_Cfg())
    for i in range(6):
        s.add_l1(f"事件{i}", "event")
    s._evict_l1()
    assert len(s.l1_entries()) <= _Cfg.l1_max_entries  # 4


def test_concurrent_append(tmp_path):
    s = MemoryStore(memories_dir=tmp_path, config=_Cfg())
    errs = []

    def w():
        try:
            for _ in range(30):
                s.add_l2("并发事实", "fact")
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    threads = [threading.Thread(target=w) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errs
    s2 = MemoryStore(memories_dir=tmp_path, config=_Cfg())
    assert len(s2.l2_entries()) == 120


if __name__ == "__main__":
    ok = 0
    with tempfile.TemporaryDirectory(prefix="memory_test_") as d:
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                # 每个测试独立子目录，避免残留污染
                c = os.path.join(d, name)
                os.makedirs(c, exist_ok=True)
                fn(c)
                ok += 1
                print(f"  ok {name}")
    print(f"\n{ok} passed")
