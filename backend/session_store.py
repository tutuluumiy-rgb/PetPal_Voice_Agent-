"""会话层：独立 session + 全量 JSONL 持久化 + 可追溯（run/sub_turn/tool_call_id）

会话层与上下文层分离：
- 会话层（本文件）：每次独立对话一个 session，所有消息【原样、完整、永删】地
  追加进 backend/sessions/<session_id>.jsonl，一行一条消息，始终可追溯。
- 上下文层（context_builder）：每次 sub_turn 从会话层【派生】送模型的视图，
  派生出的摘除/占位只影响视图，绝不改会话文件。
"""

from __future__ import annotations

import json
import os
import threading
import uuid


def _now_ts() -> float:
    import time
    return time.time()


def _estimate_tokens(value) -> int:
    """本地估算 token（教学近似：字符数/4）。"""
    s = json.dumps(value, ensure_ascii=False, default=str)
    return max(1, (len(s) + 3) // 4)


# ── 消息类型常量 ─────────────────────────────────────────
# role 与 OpenAI 兼容；用 extra 元数据标记会话层追踪字段
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_TOOL = "tool"


class SessionStore:
    """一个 session 的完整历史存档。

    线程安全（语音后端有并发 sub_turn 写回）。"""
    SESSIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions")

    def __init__(self, session_id: str | None = None):
        self.session_id = session_id or uuid.uuid4().hex[:12]
        os.makedirs(self.SESSIONS_DIR, exist_ok=True)
        self._path = os.path.join(self.SESSIONS_DIR, f"{self.session_id}.jsonl")
        self._lock = threading.Lock()
        self._messages: list[dict] = []
        self._load()

    def _load(self):
        if os.path.exists(self._path):
            try:
                with open(self._path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                self._messages.append(json.loads(line))
                            except json.JSONDecodeError:
                                continue
            except OSError:
                pass

    # ── 追加 ──────────────────────────────────────────────
    def add(self, role: str, content, *, run_id: str | None = None,
            sub_turn: int | None = None, tool_call_id: str | None = None,
            tool_calls: list | None = None, meta: dict | None = None) -> dict:
        """追加一条消息并持久化。返回存好的消息 dict。"""
        msg = {
            "id": uuid.uuid4().hex[:12],
            "role": role,
            "content": content,
            "ts": round(_now_ts(), 3),
        }
        if run_id is not None:
            msg["run_id"] = run_id
        if sub_turn is not None:
            msg["sub_turn"] = sub_turn
        if tool_call_id is not None:
            msg["tool_call_id"] = tool_call_id
        if tool_calls is not None:
            msg["tool_calls"] = tool_calls
        if meta:
            msg["meta"] = meta
        with self._lock:
            self._messages.append(msg)
            self._flush([msg])
        return msg

    def _flush(self, msgs: list[dict]):
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                for m in msgs:
                    f.write(json.dumps(m, ensure_ascii=False) + "\n")
        except OSError as e:
            print(f"[session_store] 追加持久化失败: {e}")

    # ── 读取 ──────────────────────────────────────────────
    def all(self) -> list[dict]:
        """会话层完整消息（原样）。"""
        with self._lock:
            return list(self._messages)

    def transcript(self) -> list[dict]:
        """供上下文派生的原始 message 视图（去掉会话层元数据，保留 role/content/
        tool_calls/tool_call_id）。这是『完整真相』，上下文派生基于它。"""
        out = []
        for m in self._messages:
            view = {"role": m.get("role")}
            if m.get("content") is not None:
                view["content"] = m["content"]
            if m.get("tool_calls") is not None:
                view["tool_calls"] = m["tool_calls"]
            if m.get("tool_call_id") is not None:
                view["tool_call_id"] = m["tool_call_id"]
            out.append(view)
        return out

    def messages_by_run(self, run_id: str) -> list[dict]:
        with self._lock:
            return [m for m in self._messages if m.get("run_id") == run_id]

    def estimate_tokens(self) -> int:
        return sum(_estimate_tokens(m) for m in self._messages)


def new_session_id() -> str:
    return uuid.uuid4().hex[:12]
