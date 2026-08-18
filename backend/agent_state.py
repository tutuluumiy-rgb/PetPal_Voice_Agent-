# -*- coding: utf-8 -*-
"""规范化后端状态机（供 main.py 接入，也作为 MESSAGE_CONTRACT.md 的实现依据）

双 Agent 协作契约（后端 Agent 侧落地）：
- 对外统一五态：idle / listening / thinking / speaking / error
- 状态迁移的触发源：仅来自前端上报事件（vad_speech_start、vad_speech_end、
  client_playback_done、user_abort、mode_change…）与后端内部结果（LLM/工具/TTS）。
- backend_state_change 仅作为**通知**，不做强制命令，不假设前端一定收到并同步。
- speaking 规则：发送完全部 TTS 数据 ≠ 结束；必须等待前端 client_playback_done，
  才切回 listening。speaking 期间收到 vad_speech_start → 进入打断确认。
- 业务判断优先依赖**事件**，不完全依赖内部 state 变量；state 主要用于日志、调试、对外通知。

向后兼容说明（测试看板 8080）：
- 本模块提供规范五态与迁移中心，但 main.py 现有 `session.state` 字符串
  （含内部瞬态 pending_play）仍保留，供既有逻辑读取，保证测试看板照常跑。
- normalize_state() 把内部瞬态归一到对外五态，仅影响 backend_state_change 通知。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

# ── 对外统一五态 ──────────────────────────────────────
IDLE = "idle"
LISTENING = "listening"
THINKING = "thinking"
SPEAKING = "speaking"
ERROR = "error"

# 内部瞬态（不对外暴露为五态，main.py 内部使用，归一到 speaking）
PENDING_PLAY = "pending_play"

ALL_STATES = (IDLE, LISTENING, THINKING, SPEAKING, ERROR)

# 消息 type 前缀：backend_state_change 统一走这个
STATE_CHANGE_EVENT = "backend_state_change"


def normalize_state(raw: str) -> str:
    """把 main.py 的内部状态字符串归一到对外五态。

    - pending_play 是 speaking 的瞬态子阶段 → 归一为 speaking
    - idle/error 兜底：未知值虽不中断，但日志标注，并回退 listening 之外的安全默认
    """
    if raw == PENDING_PLAY:
        return SPEAKING
    if raw in ALL_STATES:
        return raw
    # 未知状态：防御性回退（不抛异常，避免卡死状态机）
    return LISTENING


# ── 状态迁移表：src -> 允许的 dst 集合 ─────────────────
_TRANSITIONS: dict[str, set[str]] = {
    IDLE: {LISTENING, ERROR},
    LISTENING: {THINKING, SPEAKING, IDLE, ERROR},
    THINKING: {SPEAKING, LISTENING, ERROR},
    SPEAKING: {LISTENING, THINKING, IDLE, ERROR},
    ERROR: {IDLE, LISTENING},
}


@dataclass
class StateChangeRecord:
    """一条状态迁移记录（用于日志、调试、复现）。"""
    from_state: str
    to_state: str
    reason: str
    ts: float


@dataclass
class AgentStateMachine:
    """后端规范状态机。

    state_machine.set() 是**唯一的**状态变更入口（main.py 的状态赋值收敛到这里）。
    每次迁移触发 on_change 回调（main.py 用它在 WebSocket 上发 backend_state_change）。
    """
    initial: str = LISTENING
    on_change: Optional[Callable[[str, str, str], Awaitable[None]]] = None  # (new_state, reason, record)

    state: str = field(init=False)
    prev: str = field(init=False)
    history: list = field(default_factory=list)

    def __post_init__(self):
        self.state = normalize_state(self.initial)
        self.prev = None

    @property
    def is_speaking(self) -> bool:
        """是否处于播报期（含 pending_play 瞬态当 speaking 处理）。"""
        return self.state == SPEAKING

    @property
    def is_active_speaking(self) -> bool:
        """真正正在出声的阶段对外态 speaking。"""
        return self.state == SPEAKING

    def can(self, target: str) -> bool:
        """按迁移表判断是否允许从当前状态切到 target（对外五态口径）。"""
        target = normalize_state(target)
        allowed = _TRANSITIONS.get(self.state, set())
        return target in allowed or target == self.state

    async def set(self, new_state: str, reason: str = "") -> bool:
        """切换到目标状态（可传内部瞬态，自动归一）。返回是否发生了迁移。"""
        normalized = normalize_state(new_state)
        if normalized == self.state:
            # 相同状态：记录 reason 但不算迁移（供调试）
            if reason:
                self.history.append(StateChangeRecord(self.state, self.state, reason, time.time()))
            return False

        # 迁移表校验：非法迁移不强制（避免卡死），但记录 warning
        allowed = _TRANSITIONS.get(self.state, set())
        if normalized not in allowed:
            self.history.append(StateChangeRecord(self.state, normalized, f"UNSAFE:{reason}", time.time()))
            # 不中断，仍执行迁移（日志告警足够；硬拒绝可能在边界时卡住状态机）

        self.prev = self.state
        self.state = normalized
        rec = StateChangeRecord(self.prev, normalized, reason, time.time())
        self.history.append(rec)
        if self.on_change is not None:
            try:
                await self.on_change(normalized, reason, rec)
            except Exception:
                # 通知失败不应影响状态迁移本身
                pass
        return True

    def snapshot(self) -> dict:
        """对外通知/调试用的状态快照。"""
        return {
            "state": self.state,
            "prev": self.prev,
            "last_reason": self.history[-1].reason if self.history else None,
            "last_ts": self.history[-1].ts if self.history else None,
        }


# ── 超时兜底辅助 ──────────────────────────────────────
@dataclass
class StateTimeout:
    """asyncio 超时兜底：到期执行回调（不阻塞其它逻辑）。

    main.py 用它做：
      - listening 收音超时（speech_start 后无 speech_end）
      - speaking 等待 client_playback_done 超时复位

    注意：call_later 回调不能直接是 async 函数，因此这里用同步包装
    （_fire_sync）去调度 async/_fire 协程，避免「coroutine never awaited」。
    """
    seconds: float
    label: str = "state-timeout"
    _handle: object = field(default=None, init=False, repr=False)

    def arm(self, callback):
        """启动定时器，seconds 后调用 callback（async 或 sync）。已 arm 则先 disarm。"""
        self.disarm()
        loop = asyncio.get_event_loop()
        self._handle = loop.call_later(self.seconds, self._fire_sync, callback)

    def _fire_sync(self, callback):
        # call_later 的同步回调入口：把 async 回调交给事件循环执行
        self._handle = None
        if asyncio.iscoroutinefunction(callback):
            try:
                asyncio.ensure_future(self._fire(callback))
            except RuntimeError:
                pass  # 事件循环未运行等异常，静默忽略（兜底不应抛到外部）
        else:
            try:
                callback()
            except Exception as e:
                print(f"[state-timeout] {self.label} 回调异常: {e}")

    async def _fire(self, callback):
        try:
            await callback()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[state-timeout] {self.label} 回调异常: {e}")

    def disarm(self):
        if self._handle is not None:
            try:
                self._handle.cancel()
            except Exception:
                pass
            self._handle = None
