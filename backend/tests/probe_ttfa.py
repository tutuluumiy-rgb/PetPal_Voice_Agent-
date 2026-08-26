import asyncio
import sys
import statistics

sys.path.insert(0, ".")
from providers.minimax_tts import MiniMaxTTS  # noqa: E402

TEXT = "嗯，那你今天过得怎么样呀？"

async def one(t, i):
    t.first_audio_time = None
    n = 0
    async for c in t.synth_stream(TEXT, {}):
        n += len(c)
    return t.first_audio_time, n

async def main():
    t = MiniMaxTTS()
    print(f"端点: {t._ws_url()}", flush=True)
    ttfa = []
    for i in range(3):
        a, n = await one(t, i)
        ttfa.append(a)
        print(f"  第{i+1}次 首包={a}s  音频={n}B", flush=True)
        await asyncio.sleep(0.5)
    print(f"TTFA 平均={statistics.mean(ttfa):.3f}s  最小={min(ttfa):.3f}s", flush=True)

asyncio.run(main())