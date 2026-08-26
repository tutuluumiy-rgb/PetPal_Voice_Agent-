"""调试 testboard 看板：直接连后端 WS /ws/audio，模拟前端发消息，
抓 timing 消息原文，看 avg 字段是否真的累加

不发音频（ASR 会识别噪声），只通过 bypass_asr 模拟 — 不行，main.py 没这个入口。

简化方案：跳过 audio 帧，直接发 speech_start + speech_end，触发 ASR finalize。
ASR 会识别空/噪声返回空字符串，但 LLM 仍会被调（finish_user_speech 后启动 handle_user_speech）。
最后等到的 current/avg 是真实的 timing 数据。

注意：5 轮全部完成才能看 avg 是否累加。
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import websockets

WS_URL = os.getenv("WS_URL", "ws://127.0.0.1:8001/ws/audio")
SAMPLE_RATE = 16000


async def main():
    print(f"=== 连后端 {WS_URL} ===", flush=True)
    timings = []  # 收集所有 timing 消息

    async with websockets.connect(WS_URL) as ws:
        # 收 ready
        ready = json.loads(await ws.recv())
        print(f"[ready] session_id={ready.get('session_id', '?')}", flush=True)

        async def send_silence(duration_ms):
            """发一段静音 PCM"""
            n_samples = SAMPLE_RATE * duration_ms // 1000
            silence = np.zeros(n_samples, dtype=np.int16).tobytes()
            for i in range(0, len(silence), 2048):
                await ws.send(silence[i:i+2048])

        async def send_msg(obj):
            await ws.send(json.dumps(obj))

        # 跑 5 轮
        for round_idx in range(5):
            print(f"\n--- 轮 {round_idx+1}/5 ---", flush=True)
            await send_msg({"type": "speech_start", "preRollBase64": None})
            # 发 1.2s 静音（ASR 正常识别）
            await send_silence(1200)
            await send_msg({"type": "speech_end"})

            # 等 timing 消息：本轮会发 reply_end → 后续 timing
            # 每轮最多等 15s
            round_timings = []
            t_deadline = time.time() + 15
            while time.time() < t_deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1)
                except asyncio.TimeoutError:
                    # 检查是否已经收到 timing
                    if round_timings and any(t.get("type") == "timing" for t in round_timings):
                        break
                    continue
                if isinstance(raw, bytes):
                    continue  # TTS 音频
                msg = json.loads(raw)
                mtype = msg.get("type")
                if mtype == "timing":
                    round_timings.append(msg)
                    # 第一个 timing 就是本轮的，收完就退出循环
                    break
                elif mtype == "asr_final":
                    print(f"  [asr_final] {msg.get('text', '')!r}", flush=True)
                elif mtype == "tts_start":
                    pass  # 不要因 tts_start 阻塞
                elif mtype == "reply_end":
                    pass
            if round_timings:
                timings.append(round_timings[0])
                cur = round_timings[0].get("current", {})
                avg = round_timings[0].get("avg", {})
                cnt = round_timings[0].get("count", 0)
                print(f"  [timing] count={cnt}", flush=True)
                print(f"  current={cur}", flush=True)
                print(f"  avg={avg}", flush=True)
                print(f"  interrupted={cur.get('interrupted', False)}", flush=True)
            else:
                print(f"  [!] 没收到 timing", flush=True)

            # 避免下一轮被前一轮的 audio 干扰
            await asyncio.sleep(0.5)

    # 汇总
    print("\n=== 汇总 ===", flush=True)
    print(f"收到 timing 数: {len(timings)}", flush=True)
    if timings:
        last = timings[-1]
        print(f"最后一条 timing 的 avg 字段: {last.get('avg', {})}", flush=True)
        print(f"最后一条 timing 的 count 字段: {last.get('count', 0)}", flush=True)
        # 检查是否所有 avg 字段都是 0
        last_avg = last.get("avg", {})
        all_zero = all(v == 0 for v in last_avg.values())
        if all_zero:
            print("\n[!!] 诊断：5 轮跑完，最后一条 timing 的 avg 字段全为 0", flush=True)
            print("       可能是 avg_count=0（说明 5 轮都进了 include_in_avg=False 路径）", flush=True)
            print("       或者 timing_sum 没累加", flush=True)
        else:
            print(f"\n[OK] avg 有数据，testboard 应该正常显示：{last_avg}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())