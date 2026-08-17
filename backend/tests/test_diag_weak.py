"""诊断：模拟「开口后弱音窗口」的 Silero 逐帧概率

复现后端二次确认场景：cache = 开口前静音300ms + 开口后语音400ms，
取最近 256ms（开口后 144~400ms）→ 帧占比 0.00 的根因分析。

用法：
  cd backend
  python test_diag_weak.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio

import numpy as np

SAMPLE_RATE = 16000
FRAME_SIZE = 512


def downsample_24k_to_16k(pcm_bytes: bytes) -> bytes:
    data = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
    n_out = int(len(data) * 16000 / 24000)
    x_old = np.linspace(0, 1, len(data))
    x_new = np.linspace(0, 1, n_out)
    return np.interp(x_new, x_old, data).astype(np.int16).tobytes()


def amp_scale(pcm_bytes: bytes, scale: float) -> bytes:
    data = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
    return (data * scale).astype(np.int16).tobytes()


def frame_probs(vad, audio: bytes):
    pcm = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0
    vad.reset_state()
    probs = []
    for i in range(0, len(pcm) - FRAME_SIZE + 1, FRAME_SIZE):
        probs.append(vad.process(pcm[i : i + FRAME_SIZE]))
    return probs


async def main():
    from vad_engine import SileroVAD

    from providers.tts import AliyunTTS

    vad = SileroVAD(r"../frontend/vad/silero_vad.onnx")
    tts = AliyunTTS()

    chunks = []
    async for c in tts.synth_stream("从五数到十"):
        chunks.append(c)
    user16 = downsample_24k_to_16k(b"".join(chunks))

    # 语音起点
    data = np.frombuffer(user16, dtype=np.int16).astype(np.float32)
    n = int(SAMPLE_RATE * 0.02)
    voice_start = 0
    for i in range(0, len(data) - n, n):
        if float(np.sqrt(np.mean(data[i : i + n] ** 2))) > 200.0:
            voice_start = i * 2
            break
    print(f"语音起点: {voice_start/2/SAMPLE_RATE*1000:.0f}ms")

    for scale in (1.0, 0.5, 0.3, 0.2):
        scaled = amp_scale(user16, scale)
        # 窗口 = 开口后 144ms ~ 400ms（模拟 cache 最近 256ms）
        w0 = voice_start + int(SAMPLE_RATE * 0.144 * 2)
        w1 = voice_start + int(SAMPLE_RATE * 0.400 * 2)
        win = scaled[w0:w1]
        probs = frame_probs(vad, win)
        for thr in (0.45, 0.35, 0.30, 0.25, 0.20):
            ratio = sum(1 for p in probs if p >= thr) / len(probs) if probs else 0
            print(f"  x{scale:<4} 阈值{thr:.2f}: 占比={ratio:.3f}", end="  ")
        print()
        if scale == 0.3:
            print(f"    x0.3 逐帧概率: [" + " ".join(f"{p:.2f}" for p in probs) + "]")

    # 对照：完整整段 x0.3 的占比
    probs_full = frame_probs(vad, amp_scale(user16, 0.3))
    print(f"\n整段 x0.3: " + " ".join(f"{p:.2f}" for p in probs_full))


if __name__ == "__main__":
    asyncio.run(main())
