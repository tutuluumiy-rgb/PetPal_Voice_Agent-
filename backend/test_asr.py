"""ASR 调试测试：验证麦克风录音 → 喂音频 → 识别 链路

用法：
  cd backend
  python test_asr.py
"""

import asyncio
import sys
import threading

import numpy as np
import sounddevice as sd

from asr_engine import StreamingASR

SAMPLE_RATE = 16000
CHUNK = 1600


async def main():
    print("=== ASR 调试测试 ===")
    print("录音 3 秒，请在这期间说话...")
    print()

    asr = StreamingASR()
    session_id = "debug_test"

    # 累积所有音频，先看能不能录到
    all_audio = bytearray()

    partial_buf = {"text": ""}
    def on_partial(delta_text):
        partial_buf["text"] += delta_text
        print(f"  [增量] {delta_text}", flush=True)

    asr.start_streaming(session_id, on_partial)

    stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype='float32', blocksize=CHUNK)
    stream.start()

    # 录 3 秒（30 块 × 100ms）
    print("录音中...（3秒）")
    for i in range(30):
        data, _ = stream.read(CHUNK)
        pcm = (data[:, 0] * 32767).astype(np.int16).tobytes()
        all_audio.extend(pcm)
        asr.feed(session_id, pcm)

    stream.stop()
    stream.close()

    # 检查录到的音频能量
    pcm_array = np.frombuffer(bytes(all_audio), dtype=np.int16)
    peak = np.abs(pcm_array).max()
    rms = np.sqrt(np.mean(pcm_array.astype(np.float64) ** 2))
    print(f"\n[调试] 录音统计：峰值={peak}, RMS={rms:.1f}")
    print(f"[调试] 音频总长度：{len(all_audio)/2/16000:.2f} 秒")

    # 最终识别
    print("\n开始识别...")
    final_text = await asr.finalize(session_id)
    print(f"\n【最终识别】{repr(final_text)}")


if __name__ == "__main__":
    asyncio.run(main())
