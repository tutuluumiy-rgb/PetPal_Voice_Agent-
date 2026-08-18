# -*- coding: utf-8 -*-
"""端到端：语音「指令+任务」混合句 → 切换模式(文字通知不播报) → 继续送 LLM 生成第一轮回复。

验证点：
    1. parse 命中模式指令 → 切到 work
    2. 收到 mode_changed + notice（文字系统通知）
    3. 收到 LLM 的 reply（说明确实把整句+切换上下文送进了大模型，而非只播报后 return）
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


async def send_utterance(ws, audio, label):
    await ws.send(json.dumps({"type": "speech_start", "preRollBase64": None}))
    for i in range(0, len(audio), 2048):
        await ws.send(audio[i:i + 2048])
    await asyncio.sleep(0.2)
    await ws.send(json.dumps({"type": "speech_end"}))
    print(f"\n=== {label} ===")
    got_notice = False
    got_reply = False
    while not got_reply:
        raw = await asyncio.wait_for(ws.recv(), timeout=60)
        if isinstance(raw, bytes):
            continue
        msg = json.loads(raw)
        t = msg.get("type")
        if t == "asr_final":
            print(f"  [ASR] {msg.get('text')}")
        elif t == "mode_changed":
            got_notice = True
            print(f"  ✅ [mode_changed] mode={msg.get('mode')} notice={msg.get('notice')}")
        elif t == "reply":
            got_reply = True
            print(f"  ✅ [LLM reply] {msg.get('text')}")
        elif t == "reply_append":
            print(f"  [append] {msg.get('text')}")
    print(f"  结果：mode_changed={got_notice} 有LLM回复={got_reply}")


async def main():
    from providers.tts import AliyunTTS
    tts = AliyunTTS()
    # 指令+任务 混合句
    audio = await synth_16k(tts, "帮我切换成工作模式，写一个ppt")
    async with websockets.connect(WS_URL) as ws:
        await ws.recv()  # ready
        await send_utterance(ws, audio, "混合句：帮我切换成工作模式，写一个ppt")


if __name__ == "__main__":
    asyncio.run(main())
