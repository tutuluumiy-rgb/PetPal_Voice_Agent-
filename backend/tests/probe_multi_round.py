"""探针：多轮对话各阶段耗时（排除首次对话，看预热后的真实 E2E）

连续 N 轮短句对话，记录每轮 ASR/LLM首字/LLM首句/TTS首包/E2E，
输出每轮明细 + 排除首轮后的平均值（反映预热后的真实性能）。
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
ROUNDS = 5  # 总轮数
# 多轮不同短句（避免 LLM 缓存命中影响数据）
# 注意：不含触发工具的词（如"天气/时间"），否则会命中 web_search 工具轮、
#       拉高 LLM首句/E2E 均值，污染"普通闲聊"基线对比。
SENTENCES = ["你好呀", "你今天开心吗", "你喜欢吃什么", "给我讲个笑话", "你现在感觉怎么样"]


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


async def run_round(ws, audio):
    await ws.send(json.dumps({"type": "speech_start", "preRollBase64": None}))
    for i in range(0, len(audio), 2048):
        await ws.send(audio[i:i + 2048])
    await asyncio.sleep(0.15)
    await ws.send(json.dumps({"type": "speech_end"}))
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=60)
        if isinstance(raw, bytes):
            continue
        msg = json.loads(raw)
        if msg.get("type") == "timing":
            return msg.get("current", {})


async def main():
    from providers.tts import AliyunTTS

    tts = AliyunTTS()
    audios = []
    for s in SENTENCES:
        audios.append(await synth_16k(tts, s))

    results = []
    async with websockets.connect(WS_URL) as ws:
        await ws.recv()  # ready
        for i in range(ROUNDS):
            cur = await run_round(ws, audios[i % len(audios)])
            results.append(cur)
            # timing current 单位是秒，×1000 转毫秒
            print(f"  轮{i+1} [{SENTENCES[i % len(SENTENCES)]}]: "
                  f"ASR={cur.get('asr',0)*1000:.0f}ms LLM首字={cur.get('llm_first_token',0)*1000:.0f}ms "
                  f"LLM首句={cur.get('llm_first_sentence',0)*1000:.0f}ms TTS首包={cur.get('tts_first_packet',0)*1000:.0f}ms "
                  f"E2E={cur.get('e2e',0)*1000:.0f}ms")

    print("\n" + "=" * 60)
    keys = ["asr", "llm_first_token", "llm_first_sentence", "tts_first_packet", "e2e"]
    labels = {"asr": "ASR", "llm_first_token": "LLM首字", "llm_first_sentence": "LLM首句",
              "tts_first_packet": "TTS首包", "e2e": "E2E"}
    # 首次 vs 后续
    first = results[0]
    rest = results[1:]
    for k in keys:
        fv = first.get(k, 0) * 1000
        rv = (sum(r.get(k, 0) for r in rest) / len(rest) if rest else 0) * 1000
        print(f"  {labels[k]:<10} 首次={fv:7.0f}ms   后续平均(排除首轮)={rv:7.0f}ms   差={fv-rv:7.0f}ms")


if __name__ == "__main__":
    asyncio.run(main())
