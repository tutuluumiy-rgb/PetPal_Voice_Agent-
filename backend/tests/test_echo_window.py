"""验证：降阈值后「纯西西回声」是否会被误判为人声（自打断风险）

场景：西西正在说话（AEC 后残留 x0.15），前端 VAD 误触发 speech_start，
二次确认取最近 512ms —— 若占比高 → 西西会自己打断自己。

用法：
  cd backend
  python test_echo_window.py
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


async def main():
    from main import _confirm_real_speech

    from providers.tts import AliyunTTS

    tts = AliyunTTS()

    chunks = []
    async for c in tts.synth_stream("一、二、三、四、五、六、七、八、九、十！数完啦！有奖励吗？"):
        chunks.append(c)
    ball16 = downsample_24k_to_16k(b"".join(chunks))

    print("=" * 66)
    print("纯西西回声（AEC 后残留）二次确认判定")
    print("=" * 66)
    # 取西西说话的「语音主体」段（中间 1s，避开句首句尾静音）
    mid = ball16[len(ball16)//2 : len(ball16)//2 + int(SAMPLE_RATE * 1 * 2)]
    for scale in (0.15, 0.10, 0.05, 0.03):
        echo = amp_scale(mid, scale)
        pre_roll = bytes(bytearray(int(SAMPLE_RATE * 0.256 * 2)))  # 静音预卷
        dec = _confirm_real_speech(bytearray(pre_roll) + bytearray(echo))
        rms = float(np.sqrt(np.mean(np.frombuffer(echo, dtype=np.int16).astype(np.float32) ** 2)))
        print(f"  西西回声 x{scale:<5} RMS={rms:7.1f} → {'误判人声(会自打断!)' if dec else '正确判噪声 ✓'}")


if __name__ == "__main__":
    asyncio.run(main())
