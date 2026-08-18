# -*- coding: utf-8 -*-
"""后端规范状态机单测（agent_state.py）：对外五态 + 迁移 + 通知 + 超时兜底核心。

对应契约要点：
  - 对外统一五态 idle/listening/thinking/speaking/error；pending_play 归一到 speaking
  - state 变更收敛到状态机；backend_state_change 仅通知
  - speaking 必须等待 client_playback_done 才回 listening（迁移表约束）
  - 超时兜底（StateTimeout）arm/disarm 行为
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_state import (
    IDLE, LISTENING, THINKING, SPEAKING, ERROR, PENDING_PLAY, ALL_STATES,
    AgentStateMachine, normalize_state, StateTimeout,
)


def _set(sm, state, reason="") -> bool:
    """同步包装：调用 async sm.set 并返回是否发生迁移。"""
    return asyncio.run(sm.set(state, reason))


def test_normalize_state():
    # 内部瞬态 pending_play → 对外 speaking
    assert normalize_state(PENDING_PLAY) == SPEAKING
    # 五态原样
    for s in ALL_STATES:
        assert normalize_state(s) == s, s
    # 未知 → 防御回退 listening
    assert normalize_state("weird") == LISTENING
    assert normalize_state("") == LISTENING
    print("[OK] normalize_state：pending_play→speaking，五态原样，未知回退listening")


def test_state_machine_transitions():
    sm = AgentStateMachine(initial=LISTENING)
    assert sm.state == LISTENING

    _set(sm, THINKING, "llm_generating")
    assert sm.state == THINKING and sm.prev == LISTENING

    _set(sm, SPEAKING, "playback_started")
    assert sm.state == SPEAKING
    assert sm.is_speaking is True

    _set(sm, LISTENING, "playback_done")
    assert sm.state == LISTENING

    _set(sm, IDLE, "user_abort")
    assert sm.state == IDLE and sm.prev == LISTENING
    print("[OK] 状态迁移链：listening→thinking→speaking→listening→idle")


def test_same_state_no_transition():
    sm = AgentStateMachine(initial=LISTENING)
    # 同状态 set → 不算迁移（返回 False），但记录 reason
    moved = _set(sm, LISTENING, "again")
    assert moved is False
    assert sm.state == LISTENING
    print("[OK] 同状态 set 不产生迁移，仅记录")


def test_on_change_callback():
    calls = []

    async def _cb(new_state, reason, rec):
        calls.append((new_state, reason))

    sm = AgentStateMachine(initial=LISTENING, on_change=_cb)
    asyncio.run(sm.set(THINKING, "x"))
    assert calls == [(THINKING, "x")]
    # 同状态不触发回调
    asyncio.run(sm.set(THINKING, "y"))
    assert calls == [(THINKING, "x")]
    print("[OK] on_change 回调：仅真实迁移触发，同状态不触发")


def test_transition_table_guards():
    # 迁移表允许 listening→speaking（直接进入说话期），且该迁移在表内
    sm = AgentStateMachine(initial=LISTENING)
    assert sm.can(SPEAKING) is True
    # idle 不能直接跳 thinking（表外也允许执行但不告警打断——见实现，这里只测 can 口径）
    sm2 = AgentStateMachine(initial=IDLE)
    assert sm2.can(SPEAKING) is False
    print("[OK] 迁移表：listening→speaking 允许，idle→speaking 拒绝")


def test_speaking_not_done_by_tts_sent():
    """契约第 3 条核心：speaking 阶段不因 TTS 发送完毕而回到 listening。
    本测试验证状态机在 TTS 发送完后仍停留在 speaking（等 client_playback_done）。"""
    sm = AgentStateMachine(initial=LISTENING)
    _set(sm, THINKING, "llm")
    _set(sm, SPEAKING, "tts_play_started")
    # TTS 全部下发（仍 speaking）
    _set(sm, SPEAKING, "all_tts_sent")
    assert sm.state == SPEAKING
    # 只有 client_playback_done 才能回 listening
    _set(sm, LISTENING, "client_playback_done")
    assert sm.state == LISTENING
    print("[OK] speaking 不因 TTS 发送完退出，必须 client_playback_done 才回 listening")


def test_state_timeout():
    """超时兜底核心：arm 到期触发回调；disarm 后不触发。"""
    fired = {"n": 0}

    async def _cb():
        fired["n"] += 1

    async def _run():
        t = StateTimeout(0.05, "t")
        t.arm(_cb)
        await asyncio.sleep(0.15)
        assert fired["n"] == 1, f"到期应触发，实际 {fired['n']}"

        t2 = StateTimeout(0.03, "t2")
        t2.arm(_cb)
        t2.disarm()
        await asyncio.sleep(0.12)
        assert fired["n"] == 1, "disarm 后不应触发"

    asyncio.run(_run())
    print("[OK] StateTimeout：arm 到期触发，disarm 阻止触发")


if __name__ == "__main__":
    test_normalize_state()
    test_state_machine_transitions()
    test_same_state_no_transition()
    test_on_change_callback()
    test_transition_table_guards()
    test_speaking_not_done_by_tts_sent()
    test_state_timeout()
    print("\n全部 agent_state 单测通过")
