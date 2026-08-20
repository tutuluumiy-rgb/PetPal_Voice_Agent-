# -*- coding: utf-8 -*-
"""agent 底座单测：模式配置 / 会话存储 / 上下文派生(含工作模式工具JSON占位) / 压缩判断"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_config import get_mode_config, CHAT_MODE, WORK_MODE, QWEN_CONTEXT_TOKENS
from session_store import SessionStore
from context_builder import build_model_context, group_into_turns
from compaction import prepare_compaction


def _user_turn(store, run_id, text, with_tool=False, tool_result="查到了：晴天"):
    """模拟一个完整用户轮：user 输入 → （可选工具）assistant tool_calls → tool 结果 → 最终答复"""
    store.add("user", text, run_id=run_id, sub_turn=1)
    if with_tool:
        store.add("assistant", "好的我来查~", run_id=run_id, sub_turn=1,
                  tool_calls=[{
                      "id": f"tc_{run_id}",
                      "type": "function",
                      "function": {"name": "web_search", "arguments": json.dumps({"query": text}, ensure_ascii=False)},
                  }])
        store.add("tool", tool_result, run_id=run_id, sub_turn=1, tool_call_id=f"tc_{run_id}")
    store.add("assistant", "回复：" + text + "好了", run_id=run_id, sub_turn=2)


def test_config():
    chat = get_mode_config(CHAT_MODE)
    work = get_mode_config(WORK_MODE)
    assert chat.keep_complete_turns == 15 and chat.max_sub_turns == 10
    assert work.keep_complete_turns == 5 and work.max_sub_turns == 30
    assert chat.drop_old_tool_results is False
    assert work.drop_old_tool_results is True
    assert chat.context_max_tokens == QWEN_CONTEXT_TOKENS == 1_000_000
    assert chat.compaction_threshold == 700_000
    assert chat.chat_max_rounds == 15 and work.chat_max_rounds == 0
    print("[OK] 模式配置（闲聊15轮/10sub，工作5轮/30sub，1M/0.7→700k；chat 15 轮触发压缩）")


# 工作区内可写的临时会话目录（沙箱禁写系统 TEMP）
_WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP_SESS = os.path.join(_WORKSPACE, "sessions_test_tmp")


def _reset_sessions_dir():
    import shutil
    shutil.rmtree(_TMP_SESS, ignore_errors=True)
    os.makedirs(_TMP_SESS, exist_ok=True)
    SessionStore.SESSIONS_DIR = _TMP_SESS


def test_session_store_persistence():
    _reset_sessions_dir()
    s = SessionStore("t_sess")
    s.add("user", "你好", run_id="r1", sub_turn=1)
    s.add("assistant", "你好呀", run_id="r1", sub_turn=1)
    # 用同一个 session_id 重建 = 从 jsonl 读回
    s2 = SessionStore("t_sess")
    assert len(s2.all()) == 2
    tr = s2.transcript()
    assert tr[0]["role"] == "user" and tr[0]["content"] == "你好"
    assert "id" not in tr[0], "transcript 视图不应带会话层元数据"
    print("[OK] 会话 JSONL 持久化 + 可追溯 transcript 视图")


def test_chat_keeps_all_turns_full():
    _reset_sessions_dir()
    store = SessionStore("c")
    for i in range(25):
        _user_turn(store, f"r{i}", f"第{i}问", with_tool=(i % 3 == 0), tool_result=f"结果{i}")
    cfg = get_mode_config(CHAT_MODE)
    res = build_model_context("SYS", store.transcript(), cfg)
    # 闲聊不压工具结果 → 所有 tool 结果全文保留
    tool_msgs = [m for m in res.included_transcript if m.get("role") == "tool"]
    assert len(tool_msgs) >= 8, "闲聊模式工具结果应全文保留"
    # 完整轮保留（不截断）：user 数量 ≥ 24
    users = [m for m in res.included_transcript if m.get("role") == "user" and isinstance(m.get("content"), str) and "问" in m.get("content","")]
    assert len(users) >= 24, f"闲聊应保留大量轮，实际 {len(users)}"
    print(f"[OK] 闲聊上下文：保留 {len(users)} 轮全文，工具结果未压缩")


def test_work_compacts_old_tool_results():
    _reset_sessions_dir()
    store = SessionStore("w")
    # 12 轮，旧轮(前2)含工具，最近10轮也含工具
    for i in range(12):
        _user_turn(store, f"r{i}", f"问{i}", with_tool=True, tool_result=f"结果{i}")
    cfg = get_mode_config(WORK_MODE)
    res = build_model_context("SYS", store.transcript(), cfg)
    assert res.action == "tool_compact"
    # 找出所有 JSON 工具占位
    compact_records = []
    for m in res.included_transcript:
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            try:
                d = json.loads(m["content"])
                if isinstance(d, dict) and d.get("kind") == "tool_calls_compact":
                    compact_records.append(d)
            except (ValueError, TypeError, json.JSONDecodeError):
                pass
    assert len(compact_records) >= 1, "工作模式旧轮应出现工具 JSON 占位"
    first = compact_records[0]["calls"][0]
    assert first.get("tool_name") == "web_search"
    assert first.get("tool_call_id")
    assert first.get("result", {}).get("result_status")  # 有结果占位或失败
    # 最近10轮（保留窗口）的 tool 结果应仍是原始 tool 消息（全文）
    tool_msgs = [m for m in res.included_transcript if m.get("role") == "tool"]
    assert len(tool_msgs) >= 10, f"最近10轮工具结果应原文保留，实际 {len(tool_msgs)}"
    print(f"[OK] 工作模式：旧轮工具→JSON占位({len(compact_records)}处)，最近10轮工具结果原文保留({len(tool_msgs)}条)")


def test_compaction_decision():
    _reset_sessions_dir()
    from compaction import CompactionState
    cfg = get_mode_config(WORK_MODE)
    cfg2 = get_mode_config(CHAT_MODE)
    # 造 25 轮（超阈值不现实 -> 用极小的 compaction_threshold 来触发）
    store = SessionStore("cmp")
    for i in range(15):
        _user_turn(store, f"r{i}", "很长的内容。" * 200, with_tool=False)
    from agent_config import ModeAgentConfig
    tiny_cfg = ModeAgentConfig(mode=WORK_MODE, keep_complete_turns=10, max_sub_turns=30,
                               drop_old_tool_results=True, context_max_tokens=1_000_000,
                               compaction_threshold=1)  # 阈值1 → 必然超标
    state = CompactionState()
    dec = prepare_compaction(store.transcript(), tiny_cfg, state)
    assert dec.should_compact is True, dec.reason
    assert len(dec.retained_turn_views) == 11  # 10 完整 + 1 当前
    # 正常阈值下（700k）15 短轮不触发
    dec2 = prepare_compaction(store.transcript(), cfg2, state)
    assert dec2.should_compact is False
    print(f"[OK] 压缩判断：超阈值触发(保留11轮={len(dec.retained_turn_views)})，闲聊默认阈值不触发")


def test_compaction_loop_event():
    """端到端：run_agent_loop 在超阈值且轮数超出保留窗口时 yield compacted 事件。

    用 stub client 驱动（不依赖真实 LLM），压缩分支在第一次模型调用前执行，
    因此 stub 只需返回一个无工具调用的最终回复。
    """
    from agent_config import ModeAgentConfig
    from compaction import CompactionState
    from agent_runtime import run_agent_loop

    _reset_sessions_dir()
    store = SessionStore("cmploop")
    for i in range(4):
        store.add("user", f"第{i}个问题" + "机" * 300, run_id=f"c{i}", sub_turn=1)
        store.add("assistant", "答" + "机" * 300, run_id=f"c{i}", sub_turn=2)

    async def _summarize(prompt_text):
        return ("goal\n压缩测试\nconstraints\n无约束\nprogress\n进行中\n"
                "keydecision\n关键决策\nnextsteps\n完成\n")

    cfg = ModeAgentConfig(mode=WORK_MODE, keep_complete_turns=1, max_sub_turns=30,
                          drop_old_tool_results=True, context_max_tokens=1_000_000,
                          compaction_threshold=1)

    client = _StubClient(_StubResp(_StubMsg(content="完成")))

    async def _run():
        events = []
        async for ev in run_agent_loop(client, "qwen-flash", WORK_MODE, "SYS", store,
                                       config=cfg, run_id="cmpnew", summarizer=_summarize):
            events.append(ev[0])
        return events

    events = asyncio.run(_run())
    assert "compacted" in events, f"应出现 compacted 事件，实际 {events}"
    assert "sub_turn" in events and "done" in events
    print(f"[OK] 压缩链路：事件序列 {events}（超阈值+轮数超出保留→compacted 触发）")


class _StubMsg:
    def __init__(self, content="ok", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _StubChoice:
    def __init__(self, msg):
        self.message = msg


class _StubResp:
    def __init__(self, msg):
        self.choices = [_StubChoice(msg)]


class _EmptyStream:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class _StubCompletions:
    def __init__(self, resp):
        self._resp = resp

    async def create(self, **kwargs):
        if kwargs.get("stream"):
            return _EmptyStream()
        return self._resp


class _StubChat:
    def __init__(self, resp):
        self.completions = _StubCompletions(resp)


class _StubClient:
    def __init__(self, resp):
        self.chat = _StubChat(resp)


if __name__ == "__main__":
    test_config()
    test_session_store_persistence()
    test_chat_keeps_all_turns_full()
    test_work_compacts_old_tool_results()
    test_compaction_decision()
    test_compaction_loop_event()
    print("\n全部 agent 底座单测通过")
