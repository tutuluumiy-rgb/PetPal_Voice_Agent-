"""诊断打断后 ASR 音频丢失：直接调 handle_user_speech + 控制 audio 流

模拟完整时序：
1. 第一轮：用户说话 → 球球回复（多句）
2. 球球播第一句时用户打断（state=speaking 路径）
3. 观察：cache_tail 喂入 / preRoll 处理 / 用户继续说 audio 流向

控制方式：直接调 handle_user_speech + 模拟 audio 帧流。
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
    handle_audio_frame,
    handle_speech_start,
    handle_speech_end,
    ConversationSession,
    tts,
    emotion_state,
)


class MockWs:
    def __init__(self):
        self.messages = []
    async def send_json(self, obj):
        ts = time.time()
        self.messages.append((ts, obj.get("type", "?"), obj))
    async def send_bytes(self, data):
        pass  # 不打印 audio


def fake_silence(duration_ms, seed=1, amp=0.05):
    """合成'类语音'（不同 seed 不同节奏）"""
    import numpy as np
    rng = np.random.default_rng(seed)
    n = 16000 * duration_ms // 1000
    return (rng.normal(0, amp, n) * 32767).astype("int16").tobytes()


async def main():
    ws = MockWs()
    session = ConversationSession()
    session.last_asr_time = 0.5
    emotion_state.current = "平静"

    print("=== 阶段1: 第一轮对话，触发球球回复 ===\n", flush=True)
    t0 = time.time()
    # 不通过 handle_user_speech 触发——直接模拟 handle_audio_frame + speech_start
    # 因为我们要控制时序
    await handle_speech_start(ws, session, None)  # 用户开始说话
    # 发 1.5s 用户音频
    user_audio = fake_silence(1500, seed=1, amp=0.05)
    for off in range(0, len(user_audio), 2048):
        await handle_audio_frame(ws, session, user_audio[off:off+2048])
    await handle_speech_end(ws, session)  # 用户说完
    # 等 LLM + TTS 完成（看 ball 状态）
    print(f"  speech_end 后 state={session.state}", flush=True)
    # 球球开始 TTS 后，state 应该是 speaking 或 pending_play
    # 模拟 ball 播放中
    await asyncio.sleep(2)  # 让 TTS 走一段
    print(f"  2s 后 state={session.state}", flush=True)

    print("\n=== 阶段2: 用户打断（球球已 speaking）===", flush=True)
    t1 = time.time()
    await handle_speech_start(ws, session, None)  # 用户打断
    print(f"  speech_start 后 state={session.state}, is_user_speaking={session.is_user_speaking}", flush=True)
    # 发 2s 用户音频（模拟打断后继续说话）
    barge_audio = fake_silence(2000, seed=100, amp=0.05)
    for off in range(0, len(barge_audio), 2048):
        await handle_audio_frame(ws, session, barge_audio[off:off+2048])
    print(f"  发完2s 打断后音频, 累计丢失={getattr(session, 'barge_audio_lost_bytes', 0)}B", flush=True)
    await handle_speech_end(ws, session)  # 用户说完
    print(f"  speech_end 后 state={session.state}", flush=True)

    # 等收尾
    await asyncio.sleep(2)
    print(f"\n=== 阶段3: 收尾 ===\n  最终 state={session.state}", flush=True)
    print(f"  barge_audio_lost_bytes={getattr(session, 'barge_audio_lost_bytes', 0)}B", flush=True)


if __name__ == "__main__":
    asyncio.run(main())