# -*- coding: utf-8 -*-
"""端到端方案A：状态查询不误触发切换；切换后查询 LLM 能答对当前模式。

序列：
  S1 问"你现在是什么模式？"           → 不应有 mode_changed（不切），应有 LLM 回复（闲聊）
  S2 "打开工作模式"                   → 应 mode_changed=work
  S3 问"你现在是什么模式？"           → LLM 应回答"工作模式"（读 system prompt 当前模式标注）
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


async def send_utterance(ws, audio, label, expect_mode_change):
    await ws.send(json.dumps({"type": "speech_start", "preRollBase64": None}))
    for i in range(0, len(audio), 2048):
        await ws.send(audio[i:i + 2048])
    await asyncio.sleep(0.2)
    await ws.send(json.dumps({"type": "speech_end"}))
    print(f"\n=== {label} (期望 mode_changed={expect_mode_change}) ===")
    saw_mode = False
    replies = []
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=60)
        if isinstance(raw, bytes):
            continue
        msg = json.loads(raw)
        t = msg.get("type")
        if t == "asr_final":
            print(f"  [ASR] {msg.get('text')}")
        elif t == "mode_changed":
            saw_mode = True
            print(f"  ⚠️ [mode_changed] mode={msg.get('mode')} notice={msg.get('notice')}")
        elif t == "reply":
            replies.append(msg.get("text"))
            print(f"  [LLM] {msg.get('text')}")
        elif t == "reply_append":
            replies.append(msg.get("text"))
            print(f"  [继续] {msg.get('text')}")
        elif t == "reply_end":
            break
    full = "".join(replies)
    status = "✅" if (saw_mode == expect_mode_change) else "❌"
    print(f"  {status} saw_mode_changed={saw_mode} (期望 {expect_mode_change})")
    return saw_mode, full


async def main():
    from providers.tts import AliyunTTS
    tts = AliyunTTS()
    q = await synth_16k(tts, "你现在是什么模式")
    w = await synth_16k(tts, "打开工作模式")
    async with websockets.connect(WS_URL) as ws:
        await ws.recv()  # ready
        await send_utterance(ws, q, "S1 状态查询（应不切，走LLM）", expect_mode_change=False)
        await send_utterance(ws, w, "S2 打开工作模式（应切work）", expect_mode_change=True)
        await send_utterance(ws, q, "S3 状态查询（LLM应答工作模式，不切）", expect_mode_change=False)


if __name__ == "__main__":
    asyncio.run(main())
