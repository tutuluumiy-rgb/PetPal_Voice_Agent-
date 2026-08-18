# -*- coding: utf-8 -*-
"""完整 main 管道 WS 端到端：原生 function calling（agent_runtime）走通。

发送：
  S1  "帮我算一下 6 乘以 7 等于多少"   → 应触发 calculator → 回复答案
  S2  "你好呀"                        → 普通闲聊
监听 reply / 事件，确认回复通过新 agent 环回来。
"""
import asyncio
import json
import os
import sys

import numpy as np
import websockets

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

WS_URL = os.getenv("WS_URL", "ws://127.0.0.1:8002/ws/audio")
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


async def send_utterance(ws, audio, label):
    await ws.send(json.dumps({"type": "speech_start", "preRollBase64": None}))
    for i in range(0, len(audio), 2048):
        await ws.send(audio[i:i + 2048])
    await asyncio.sleep(0.2)
    await ws.send(json.dumps({"type": "speech_end"}))
    print(f"\n=== {label} ===")
    n = 0
    while n < 60:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=20)
        except asyncio.TimeoutError:
            print("  !! 20s 内无新消息，退出")
            return
        if isinstance(raw, bytes):
            continue
        msg = json.loads(raw)
        t = msg.get("type")
        n += 1
        print(f"  [{n}] {t} {msg.get('detail') or msg.get('text') or msg.get('stage') or ''}")
        if t == "reply_end" or t == "reply_start" and n > 3:
            pass
        if t == "reply_end":
            return


async def main():
    from providers.tts import AliyunTTS
    tts = AliyunTTS()
    calc = await synth_16k(tts, "帮我算一下六乘以七等于多少")
    chat = await synth_16k(tts, "你好呀")
    async with websockets.connect(WS_URL) as ws:
        await ws.recv()  # ready
        await send_utterance(ws, calc, "S1 工具场景：6*7")
        await send_utterance(ws, chat, "S2 闲聊：你好呀")


if __name__ == "__main__":
    asyncio.run(main())
