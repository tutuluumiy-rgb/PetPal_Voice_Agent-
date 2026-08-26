"""模拟真实场景：跑 5 轮，第 3 轮打断（abort_speaking=True 模拟 cancel pipeline）

观察 avg 是否真的累加正确（4 轮完整 + 1 轮打断）
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ".")

os.environ["MINIMAX_TRANSPORT"] = "ws"

from main import (  # noqa: E402
    handle_user_speech,
    ConversationSession,
    tts,
    emotion_state,
    _preheat_tts,
)


CASES = [
    "我今天真的不想上班",
    "中午了吃啥",
    "打断测试",  # 这一轮打断
    "用大白话解释API",
    "聊聊AI",
]


class MockWs:
    def __init__(self):
        self.messages = []
    async def send_json(self, obj):
        self.messages.append((time.time(), obj.get("type", "?"), obj))
    async def send_bytes(self, data):
        pass


async def run_with_barge(idx, text, session, will_barge=False):
    ws = MockWs()
    session.last_asr_time = 0.5
    emotion_state.current = "平静"

    print(f"\n=== 轮 {idx+1}/5：{text}{'  [打断]' if will_barge else ''} ===", flush=True)

    if will_barge:
        # 在 handle_user_speech 启动后立即设 abort_speaking，模拟取消
        async def set_abort():
            await asyncio.sleep(0.05)
            session.abort_speaking = True
            if session.tts_task and not session.tts_task.done():
                session.tts_task.cancel()
        asyncio.create_task(set_abort())

    try:
        await asyncio.wait_for(handle_user_speech(ws, session, text), timeout=30)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass

    timing_msgs = [d for _, t, d in ws.messages if t == "timing"]
    if timing_msgs:
        last = timing_msgs[-1]
        cur = last.get("current", {})
        avg = last.get("avg", {})
        cnt = last.get("count", 0)
        ac = last.get("avg_count", -1)
        print(f"  count={cnt}  avg_count={ac}", flush=True)
        print(f"  current.e2e={cur.get('e2e')}  avg.e2e={avg.get('e2e')}", flush=True)
        print(f"  interrupted={cur.get('interrupted', False)}", flush=True)


async def _main():
    print(f"TTS={type(tts).__name__} transport={tts.transport}", flush=True)
    print("[预热]", flush=True)
    try:
        await _preheat_tts(MockWs(), ConversationSession())
    except Exception as e:
        print(f"  预热失败: {e}", flush=True)

    # 同一个 session 跑 5 轮
    ws_main = MockWs()
    session = ConversationSession()
    for i, text in enumerate(CASES):
        await run_with_barge(i, text, session, will_barge=(i == 2))

    if hasattr(tts, "_close_ws"):
        try:
            await tts._close_ws()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(_main())