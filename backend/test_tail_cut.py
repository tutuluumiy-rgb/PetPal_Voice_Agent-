"""验证「尾字被掐」：同一段音频，完整基线 vs 截断+迟到尾音拼接

- 场景A（基线）：完整音频 → speech_start → 全部音频 → speech_end → 识别
- 场景B（模拟前端时序）：speech_start → 前85%音频 → speech_end → 迟到尾音15%
  （真实前端 VAD onSpeechEnd 可能比 ScriptProcessor 最后一块音频早 ~100-200ms）

同一段 TTS 音频，排除合成波动。若 A 完整而 B 丢尾字 → 后端需等尾音块到达再 finalize（已加 sleep 0.2 修复）

用法（先启动后端，可用 WS_URL 环境变量指定端口）：
  cd backend
  python test_tail_cut.py
"""

import asyncio
import json
import os

import numpy as np
import websockets

WS_URL = os.getenv("WS_URL", "ws://127.0.0.1:8001/ws/audio")
SAMPLE_RATE = 16000


async def synth_16k(tts, text: str) -> bytes:
    chunks = []
    async for c in tts.synth_stream(text):
        chunks.append(c)
    pcm24 = b"".join(chunks)
    data = np.frombuffer(pcm24, dtype=np.int16).astype(np.float32)
    n_out = int(len(data) * 16000 / 24000)
    x_old = np.linspace(0, 1, len(data))
    x_new = np.linspace(0, 1, n_out)
    return np.interp(x_new, x_old, data).astype(np.int16).tobytes()


async def run_scenario(ws, head: bytes, tail: bytes, label: str):
    """跑一个识别场景，返回识别文本"""
    await ws.send(json.dumps({"type": "speech_start", "preRollBase64": None}))
    for i in range(0, len(head), 2048):
        await ws.send(head[i : i + 2048])
    await ws.send(json.dumps({"type": "speech_end"}))
    if tail:
        # 迟到尾音（speech_end 之后才发）
        for i in range(0, len(tail), 2048):
            await ws.send(tail[i : i + 2048])
    print(f"[{label}] 已提交 head={len(head)/2/SAMPLE_RATE*1000:.0f}ms + 迟到尾音={len(tail)/2/SAMPLE_RATE*1000:.0f}ms")

    result = None
    while True:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=20)
        except asyncio.TimeoutError:
            break
        if isinstance(raw, bytes):
            continue
        msg = json.loads(raw)
        if msg.get("type") == "asr_final":
            result = msg.get("text", "")
            break
    print(f"[{label}] 识别: {result!r}")
    return result or ""


async def main():
    from tts_engine import TTSEngine

    tts = TTSEngine()
    user_audio = await synth_16k(tts, "从五数到十")
    print(f"[素材] 同一段音频 {len(user_audio)/2/SAMPLE_RATE*1000:.0f}ms")
    cut = int(len(user_audio) * 0.85)
    head, tail = user_audio[:cut], user_audio[cut:]

    async with websockets.connect(WS_URL) as ws:
        ready = json.loads(await ws.recv())
        print(f"[连接] session={ready['session_id']}")

        # 场景A：完整基线
        r_a = await run_scenario(ws, user_audio, b"", "A完整基线")

        # 场景B：截断 + 迟到尾音（同一连接第二轮）
        r_b = await run_scenario(ws, head, tail, "B截断+迟到尾音")

    print("=" * 60)
    ok_a = "十" in r_a
    ok_b = "十" in r_b
    print(f"A 完整基线: {r_a!r} -> {'完整' if ok_a else '异常'}")
    print(f"B 截断+迟到尾音: {r_b!r} -> {'尾字完整' if ok_b else '尾字被掐/错乱'}")
    if ok_a and ok_b:
        print("结论: 尾字修复有效 ✓（迟到尾音被识别）")
    elif ok_a and not ok_b:
        print("结论: 尾字被掐 ✗（迟到尾音丢失，sleep 可能不足或顺序问题）")
    else:
        print("结论: 基线本身异常（TTS 合成/ASR 波动），需重试或换素材")


if __name__ == "__main__":
    asyncio.run(main())
