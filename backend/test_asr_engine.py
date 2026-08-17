"""环节1：ASR 引擎单独测试 —— TTS 合成已知文本 → ASR 识别 → 对比

隔离采集链路（麦克风/AEC/VAD/打断），纯测两件事：
1. TTS 合成质量（合成音频本身是否可识别）
2. ASR 引擎识别准确性（qwen3-asr-flash-realtime 对干净音频的表现）

用法：
  cd backend
  python test_asr_engine.py
"""

import asyncio

import numpy as np

from asr_engine import StreamingASR
from tts_engine import TTSEngine

# 与用户实测相关的短句 + 常见对话句
TEXTS = [
    "从五数到十",
    "从二数到十",
    "今天天气怎么样",
    "帮我查一下明天的天气",
]


def downsample_24k_to_16k(pcm_bytes: bytes) -> bytes:
    """24kHz PCM → 16kHz PCM（线性插值，2/3 比率）"""
    data = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
    n_out = int(len(data) * 16000 / 24000)
    if n_out <= 0:
        return b""
    x_old = np.linspace(0, 1, len(data))
    x_new = np.linspace(0, 1, n_out)
    resampled = np.interp(x_new, x_old, data)
    return resampled.astype(np.int16).tobytes()


async def main():
    tts = TTSEngine()
    asr = StreamingASR()

    print("=" * 60)
    print("环节1：ASR 引擎单独测试（干净音频，无 AEC/VAD/打断干扰）")
    print("=" * 60)

    for text in TEXTS:
        # 1. TTS 合成（24kHz PCM）
        chunks = []
        async for chunk in tts.synth_stream(text):
            chunks.append(chunk)
        pcm24 = b"".join(chunks)
        pcm16 = downsample_24k_to_16k(pcm24)
        print(f"\n=== 原文: {text} ===")
        print(f"  TTS: 24k 时长 {len(pcm24)/2/24000:.2f}s, 16k 时长 {len(pcm16)/2/16000:.2f}s")

        # 2. ASR 识别
        session_id = f"asr_test_{len(pcm16)}"
        partial_buf = {"text": ""}

        def on_partial(delta):
            partial_buf["text"] += delta

        asr.start_streaming(session_id, on_partial)
        asr.feed(session_id, pcm16)
        result = await asr.finalize(session_id)

        print(f"  ASR 增量累积: {partial_buf['text']!r}")
        print(f"  ASR 最终结果: {result!r}")
        match = result.strip() == text
        print(f"  判定: {'✓ 完全匹配' if match else '✗ 不匹配'}")


if __name__ == "__main__":
    asyncio.run(main())
