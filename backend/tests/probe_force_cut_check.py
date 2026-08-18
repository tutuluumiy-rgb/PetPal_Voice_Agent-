# -*- coding: utf-8 -*-
"""临时验证：确认强制切短句优化是否生效。

抓取后端 reply / reply_append 消息（即 LLM 逐句切分结果），
打印每一句切出后的【长度】，看首句是否被限制在 FIRST_SENTENCE_MAX_CHARS=12 内。
"""
import asyncio
import json
import os
import sys

import numpy as np
import websockets

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

WS_URL = os.getenv("WS_URL", "ws://127.0.0.1:8001/ws/audio")
SR = 16000


async def synth_16k(tts, text):
    chunks = []
    async for c in tts.synth_stream(text):
        chunks.append(c)
    pcm24 = b"".join(chunks)
    data = np.frombuffer(pcm24, dtype=np.int16).astype(np.float32)
    n_out = int(len(data) * 16000 / 24000)
    x_old = np.linspace(0, 1, len(data))
    x_new = np.linspace(0, 1, n_out)
    return np.interp(x_new, x_old, data).astype(np.int16).tobytes()


async def run_round(ws, audio, label):
    await ws.send(json.dumps({"type": "speech_start", "preRollBase64": None}))
    for i in range(0, len(audio), 2048):
        await ws.send(audio[i:i + 2048])
    await asyncio.sleep(0.15)
    await ws.send(json.dumps({"type": "speech_end"}))
    print(f"\n=== {label} ===")
    first_len = None
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=60)
        if isinstance(raw, bytes):
            continue
        msg = json.loads(raw)
        t = msg.get("type")
        if t == "reply":
            txt = msg.get("text", "")
            print(f"  首句[{len(txt)}字]: {txt}")
        elif t == "reply_append":
            txt = msg.get("text", "")
            print(f"  续句[{len(txt)}字]: {txt}")
        elif t == "asr_final":
            print(f"  [ASR] {msg.get('text')}")
        elif t == "reply_end":
            print("  -- 回复结束 --")
            return


async def main():
    from providers.tts import AliyunTTS
    tts = AliyunTTS()
    # 诱导长首句的问题（触发强制切句才有意义）
    audio = await synth_16k(tts, "请详细介绍一下你自己")
    async with websockets.connect(WS_URL) as ws:
        await ws.recv()  # ready
        await run_round(ws, audio, "长回复场景（自我介绍）")


if __name__ == "__main__":
    asyncio.run(main())
