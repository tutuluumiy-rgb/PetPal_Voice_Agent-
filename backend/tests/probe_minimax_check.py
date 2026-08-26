import asyncio
import sys

sys.path.insert(0, ".")
from providers.minimax_tts import MiniMaxTTS  # noqa: E402


async def go(text, params):
    t = MiniMaxTTS()
    chunks = []
    async for c in t.synth_stream(text, params):
        chunks.append(c)
    if chunks:
        print(f"OK {text[:20]!r}: {len(b''.join(chunks))} bytes", flush=True)
    else:
        print(f"FAIL {text[:20]!r}", flush=True)


async def main():
    await go("你好呀，我是年年", {"emotion": "平静"})                      # -> calm
    await go("累(breath)死了……好想休息<#1.2#>一下", {"emotion": "难过"})   # 官方插话+停顿


asyncio.run(main())