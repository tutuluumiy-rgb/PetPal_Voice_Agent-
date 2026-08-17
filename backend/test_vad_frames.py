"""环节2-细粒度：Silero VAD 逐帧概率分析

验证：为什么「正常音量人声」在 _confirm_real_speech 里被判噪声（占比 0.00），
而弱化版（x0.3）反而确认（0.12）。打印最近 256ms 每帧概率 + 增益敏感性。

用法：
  cd backend
  python test_vad_frames.py
"""

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


def frame_probs(vad, audio: bytes, threshold: float):
    """返回 (每帧概率列表, 人声帧占比)"""
    pcm = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0
    vad.reset_state()
    probs = []
    for i in range(0, len(pcm) - FRAME_SIZE + 1, FRAME_SIZE):
        frame = pcm[i : i + FRAME_SIZE]
        probs.append(vad.process(frame))
    if not probs:
        return [], 0.0
    ratio = sum(1 for p in probs if p >= threshold) / len(probs)
    return probs, ratio


async def main():
    from vad_engine import SileroVAD

    from tts_engine import TTSEngine

    vad = SileroVAD(r"../frontend/vad/silero_vad.onnx")
    tts = TTSEngine()

    chunks = []
    async for c in tts.synth_stream("从五数到十"):
        chunks.append(c)
    voice16 = downsample_24k_to_16k(b"".join(chunks))

    # 最近 256ms 窗口（与 _confirm_real_speech 一致）
    window_bytes = int(SAMPLE_RATE * 256 / 1000 * 2)  # 8192
    recent = voice16[-window_bytes:]
    print(f"整段 {len(voice16)/2/SAMPLE_RATE*1000:.0f}ms, 最近256ms = {len(recent)} 字节")

    for scale in (1.0, 0.5, 0.3, 0.2, 0.15, 0.1):
        scaled = amp_scale(recent, scale)
        probs, ratio = frame_probs(vad, scaled, 0.45)
        rms = float(np.sqrt(np.mean(np.frombuffer(scaled, dtype=np.int16).astype(np.float32) ** 2)))
        probs_str = " ".join(f"{p:.2f}" for p in probs)
        print(f"  x{scale:<4} RMS={rms:7.1f} 占比={ratio:.3f}  逐帧概率: [{probs_str}]")

    # 对比：取整段中间 256ms（语音主体）
    mid = voice16[len(voice16)//2 - window_bytes//2 : len(voice16)//2 + window_bytes//2]
    print(f"\n中间256ms（语音主体）:")
    for scale in (1.0, 0.3, 0.1):
        scaled = amp_scale(mid, scale)
        probs, ratio = frame_probs(vad, scaled, 0.45)
        probs_str = " ".join(f"{p:.2f}" for p in probs)
        print(f"  x{scale:<4} 占比={ratio:.3f}  逐帧概率: [{probs_str}]")


if __name__ == "__main__":
    asyncio.run(main())
