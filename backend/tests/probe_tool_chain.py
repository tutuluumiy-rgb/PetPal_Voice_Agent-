"""探针：验证搜索工具调用 + 最终回复 TTS 链路

直接调 handle_user_speech（真实后端组件），用一句会触发 web_search 的话，
观察：
1. on_tool 是否触发（打印 [工具调用])
2. 工具执行结果
3. 最终 reply 是否到达 + TTS 是否合成
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ".")

os.environ["TTS_PROVIDER"] = "ali"

from main import (  # noqa: E402
    handle_user_speech,
    ConversationSession,
    tts,
    emotion_state,
)


class MockWs:
    def __init__(self):
        self.messages = []
        self.audio_bytes = 0

    async def send_json(self, obj):
        ts = time.time()
        self.messages.append((ts, obj.get("type", "?"), obj))

    async def send_bytes(self, data):
        self.audio_bytes += len(data)


async def main():
    print(f"TTS={type(tts).__name__}", flush=True)
    ws = MockWs()
    session = ConversationSession()
    session.last_asr_time = 0.5
    emotion_state.current = "平静"

    # 触发 web_search 的话
    text = "帮我搜索一下最近的人工智能新闻"
    print(f"用户: {text}\n", flush=True)

    t0 = time.time()
    try:
        await asyncio.wait_for(
            handle_user_speech(ws, session, text),
            timeout=60,
        )
    except asyncio.TimeoutError:
        print("  [!] handle_user_speech 超时（60s）——链路卡住!", flush=True)

    print(f"\n=== 消息序列（阶段）===", flush=True)
    for ts, mt, data in ws.messages:
        if mt in ("reply", "reply_append", "tts_start", "tts_end", "reply_end", "mode_changed"):
            if mt in ("reply", "reply_append"):
                print(f"  [+{ts-t0:.1f}s] {mt}: {data.get('text', '')[:60]!r}", flush=True)
            else:
                print(f"  [+{ts-t0:.1f}s] {mt}", flush=True)

    print(f"\n=== 汇总 ===", flush=True)
    replies = [d for _, m, d in ws.messages if m == "reply"]
    tts_starts = [d for _, m, d in ws.messages if m == "tts_start"]
    reply_ends = [d for _, m, d in ws.messages if m == "reply_end"]
    timings = [d for _, m, d in ws.messages if m == "timing"]
    print(f"reply 数: {len(replies)}", flush=True)
    print(f"tts_start 数: {len(tts_starts)}", flush=True)
    print(f"reply_end 数: {len(reply_ends)}", flush=True)
    print(f"timing 数: {len(timings)}", flush=True)
    print(f"音频字节: {ws.audio_bytes}", flush=True)
    full = "".join(d.get("text", "") for _, m, d in ws.messages if m in ("reply", "reply_append"))
    print(f"\n完整回复文本: {full!r}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())