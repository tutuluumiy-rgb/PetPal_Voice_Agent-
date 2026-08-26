"""真实模拟 testboard 5 轮交互流程：连后端 WS，按 testboard 协议完整跑 5 轮

复现 testboard 在浏览器里的行为：麦克风帧 + speech_start + speech_end → 等后端回复 → tts_start + audio bytes → reply_end + timing
最后查看所有 timing 消息的原文，看 avg 累加情况。
"""
import asyncio
import base64
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import websockets

WS_URL = os.getenv("WS_URL", "ws://127.0.0.1:8001/ws/audio")
SAMPLE_RATE = 16000

CASES = [
    "你好",
    "今天天气怎么样",
    "我今天不想上班",
    "中午吃什么",
    "为什么手机会卡",
]

# 给每个用例预录 1.5s"模拟语音"——用静音 + 一点噪声
def synth_silence_with_noise(duration_ms, seed=42):
    rng = np.random.default_rng(seed)
    n = SAMPLE_RATE * duration_ms // 1000
    # 极低噪声让 ASR 识别为短词（避免完全静音被滤掉）
    samples = (rng.normal(0, 0.005, n) * 32767).astype(np.int16)
    return samples.tobytes()


async def main():
    timings = []
    print(f"=== 连后端 {WS_URL} ===", flush=True)
    async with websockets.connect(WS_URL) as ws:
        ready = json.loads(await ws.recv())
        print(f"[ready] session_id={ready.get('session_id')}", flush=True)

        for i, text in enumerate(CASES):
            print(f"\n--- 轮 {i+1}/5：{text} ---", flush=True)
            # speech_start + 静音 + speech_end
            await ws.send(json.dumps({"type": "speech_start", "preRollBase64": None}))
            pcm = synth_silence_with_noise(1500, seed=i)
            for off in range(0, len(pcm), 2048):
                await ws.send(pcm[off:off+2048])
            await asyncio.sleep(0.1)
            await ws.send(json.dumps({"type": "speech_end"}))

            # 收本轮消息直到收到 timing
            t_deadline = time.time() + 20
            last_timing = None
            while time.time() < t_deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1)
                except asyncio.TimeoutError:
                    if last_timing:
                        break
                    continue
                if isinstance(raw, bytes):
                    continue
                msg = json.loads(raw)
                if msg["type"] == "timing":
                    last_timing = msg
                if last_timing and msg["type"] == "tts_start" and False:
                    pass
                # 等到 timing 出现就跳出（这是本轮的 timing）
                if last_timing:
                    # 再等一帧确保时序稳定
                    await asyncio.sleep(0.05)
                    break

            if last_timing:
                timings.append(last_timing)
                print(f"  count={last_timing.get('count')} avg_count={last_timing.get('avg_count', 'N/A')}", flush=True)
                print(f"  current.e2e={last_timing['current'].get('e2e')} avg.e2e={last_timing['avg'].get('e2e')}", flush=True)
                print(f"  interrupted={last_timing['current'].get('interrupted', False)}", flush=True)

            await asyncio.sleep(0.3)

    print(f"\n=== 汇总 ===", flush=True)
    print(f"收到 timing 数: {len(timings)}", flush=True)
    if timings:
        last = timings[-1]
        print(f"\n最后一条 timing：", flush=True)
        print(f"  count={last.get('count')}", flush=True)
        print(f"  avg_count={last.get('avg_count', 'N/A')}", flush=True)
        print(f"  current.e2e={last['current'].get('e2e')}", flush=True)
        print(f"  avg.e2e={last['avg'].get('e2e')}", flush=True)
        print(f"  avg={last['avg']}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())