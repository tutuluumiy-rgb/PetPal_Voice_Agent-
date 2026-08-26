import asyncio
import sys

sys.path.insert(0, ".")
from providers.minimax_tts import MiniMaxTTS  # noqa: E402

SENTENCES = ["你好呀，我是年年", "今天过得怎么样呀", "要不要我给你讲个笑话"]


async def main():
    import os
    os.environ["MINIMAX_TRANSPORT"] = "ws"
    t = MiniMaxTTS()
    print("transport:", t.transport, flush=True)
    last_ws_id = None
    for i, s in enumerate(SENTENCES):
        t.first_audio_time = None
        n = 0
        async for c in t.synth_stream(s, {}):
            n += len(c)
        ws = t._ws
        reused = "复用" if ws is not None and ws is last_ws_id else "新建/首次"
        last_ws_id = ws
        print(f"  句{i+1} 首包={t.first_audio_time}s 音频={n}B 连接={reused} ready={t._ws_ready}", flush=True)
    await t._close_ws()
    print("done", flush=True)


asyncio.run(main())