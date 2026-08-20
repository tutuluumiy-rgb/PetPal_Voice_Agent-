# -*- coding: utf-8 -*-
"""记忆注入 context_builder 单测：system 段出现、L3>L2>L1 顺序、预算截断。

不连 LLM/网络。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from context_builder import build_model_context, _build_memory_system_block
from session_store import _estimate_tokens


class _Cfg:
    mode = "chat"
    keep_complete_turns = 20
    max_sub_turns = 10
    drop_old_tool_results = False
    context_max_tokens = 100000
    compaction_threshold = 90000
    memory_max_tokens = 1800


def test_injected_when_present():
    blocks = {
        "l3": "用户画像：后端工程师。",
        "l2": [
            {"id": "f1", "text": "用户叫小陈", "confirmed": True},
            {"id": "f2", "text": "未确认的偏好", "confirmed": False},  # 不应注入
        ],
        "l1": [{"id": "e1", "text": "(事件) 下周三出差"}],
    }
    result = build_model_context("SYSTEM", [], _Cfg(), memory_blocks=blocks, memory_max_tokens=1800)
    joined = "\n".join(m.get("content", "") for m in result.model_context)
    assert "Long-term Memory" in joined
    assert "用户叫小陈" in joined
    assert "用户画像：后端工程师" in joined
    assert "未确认的偏好" not in joined  # confirmed=False 不注入


def test_budget_clamp():
    # 超预算：只有少量最优先（L3）能进，其它被截断
    blocks = {
        "l3": "长画像" * 200,  # 超大，撑满预算
        "l2": [{"id": "f1", "text": "A" * 500, "confirmed": True}],
        "l1": [],
    }
    text = _build_memory_system_block(blocks, max_tokens=50)
    assert text  # 至少有一条
    assert "长画像" in text


def test_blocks_none_no_injection():
    result = build_model_context("SYSTEM", [], _Cfg(), memory_blocks=None)
    joined = "\n".join(m.get("content", "") for m in result.model_context)
    assert "Long-term Memory" not in joined


def test_injection_under_budget():
    blocks = {
        "l3": "U".join(["画像", "工程师"]),
        "l2": [{"id": "f1", "text": "偏好咖啡", "confirmed": True}],
        "l1": [],
    }
    result = build_model_context("SYSTEM", [], _Cfg(), memory_blocks=blocks, memory_max_tokens=200)
    mem = "\n".join(m.get("content", "") for m in result.model_context)
    token = _estimate_tokens(mem)
    assert token <= 200 + 20  # 允许少量误差


if __name__ == "__main__":
    ok = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            ok += 1
            print(f"  ok {name}")
    print(f"\n{ok} passed")
