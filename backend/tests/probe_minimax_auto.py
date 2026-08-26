import asyncio
import sys

sys.path.insert(0, ".")
from providers.minimax_tts import MiniMaxTTS  # noqa: E402


async def go(text, params, label):
    t = MiniMaxTTS()
    chunks = []
    async for c in t.synth_stream(text, params):
        chunks.append(c)
    ok = "OK" if chunks else "FAIL"
    print(f"[{label}] {ok}: {len(b''.join(chunks))} bytes", flush=True)


async def main():
    # 默认：不传 emotion，MiniMax 自动挑情绪
    await go("哎……今天累死了，不想动了", None, "自动情绪")
    # 显式传英文枚举：精细/测试用
    await go("你给我过来，谁让你乱动的！", {"emotion_enum": "angry"}, "显式angry")
    # 真实链路 params（emotion_state），应被忽略 emotion、只取数值参数
    import emotion_state
    es = emotion_state.EmotionState(); es.update("委屈")
    await go("你怎么不理我嘛……", es.get_tts_params(), "emotion_state参数")


asyncio.run(main())