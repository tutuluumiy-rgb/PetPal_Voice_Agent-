"""MiniMax 真实合成探针：走 MiniMaxTTS 链路，合成一句，报告结果并存 wav。

用法: python tests/probe_minimax_live.py [文本] [情绪]
"""
from __future__ import annotations

import asyncio
import io
import os
import sys
import wave

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers.minimax_tts import MiniMaxTTS  # noqa: E402

SR = 24000
CH = 1
W = 2


async def main():
    text = sys.argv[1] if len(sys.argv) > 1 else "你好呀，我是年年，欢迎回家"
    params = {}
    if len(sys.argv) > 2:
        params["emotion"] = sys.argv[2]

    tts = MiniMaxTTS()
    print(f"[probe] model={tts.model} voice={tts.voice_id} url={tts._url()}", flush=True)

    chunks = []
    errors = []
    try:
        async for c in tts.synth_stream(text, params):
            chunks.append(c)
    except Exception as e:  # noqa: BLE001
        errors.append(repr(e))

    if errors:
        print(f"[probe] 失败: {errors[0]}", flush=True)
        return
    if not chunks:
        print("[probe] 失败: 无音频输出（检查 base_url/GroupId/额度）", flush=True)
        return

    pcm = b"".join(chunks)
    dur = len(pcm) / SR / W
    print(f"[probe] OK: {len(pcm)} 字节 ≈ {dur:.2f}s（24kHz 16bit 单声道 PCM）", flush=True)

    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "minimax_test.wav")
    with wave.open(out, "wb") as w:
        w.setnchannels(CH)
        w.setsampwidth(W)
        w.setframerate(SR)
        w.writeframes(pcm)
    print(f"[probe] 已存: {out}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())