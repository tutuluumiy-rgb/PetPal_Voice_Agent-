"""探针：测普通聊天 vs 工具场景的各阶段耗时（timing current 数值）"""
import asyncio, json, os, sys
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


async def run(ws, audio, label):
    await ws.send(json.dumps({"type": "speech_start", "preRollBase64": None}))
    for i in range(0, len(audio), 2048):
        await ws.send(audio[i:i + 2048])
    await asyncio.sleep(0.15)
    await ws.send(json.dumps({"type": "speech_end"}))
    print(f"\n=== {label} ===")
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=40)
        if isinstance(raw, bytes):
            continue
        msg = json.loads(raw)
        if msg.get("type") == "timing":
            c = msg.get("current", {})
            print(f"  ASR={c.get('asr')}ms LLM首字={c.get('llm_first_token')}ms LLM首句={c.get('llm_first_sentence')}ms TTS首包={c.get('tts_first_packet')}ms E2E={c.get('e2e')}ms")
            return
        if msg.get("type") == "asr_final":
            print(f"  [ASR] {msg.get('text')}")


async def main():
    from providers.tts import AliyunTTS
    tts = AliyunTTS()
    chat_audio = await synth_16k(tts, "你好呀")
    tool_audio = await synth_16k(tts, "帮我查一下北京明天的天气")
    async with websockets.connect(WS_URL) as ws:
        await ws.recv()  # ready
        await run(ws, chat_audio, "普通聊天（你好呀）")
        await run(ws, tool_audio, "工具场景（查天气）")


if __name__ == "__main__":
    asyncio.run(main())
