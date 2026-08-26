import asyncio
import sys

sys.path.insert(0, ".")
from providers.minimax_tts import MiniMaxTTS, EMOTION_ENUMS, INTERJECTIONS  # noqa: E402

BROKEN = (
    "西西:哼〈#0.5#〉，要听笑话是吧?我昨天去参加猫猫选美大赛<#0.8#>，"
    "评委问我:\"你这么凶，怎么还来参赛?\"我一瞪眼:\"我这不是凶，是......是气质!\""
    "然后全场猫都笑了<laughs>。结果我一回头哦豁，裁判是只狗，它直接把我淘汰了<groans>。"
    "(憋笑)好嘛，这下连狗都不给我面子<breath>......"
)


async def main():
    t = MiniMaxTTS()
    print("原始文本:", BROKEN, flush=True)
    cleaned = t._clean_text(BROKEN)
    print("\n净化后:", cleaned, flush=True)
    print("\n是否残留尖括号拟声:", "<" in cleaned.replace("<#0.8#>", ""), flush=True)
    # 真实合成确认不报错
    chunks = []
    async for c in t.synth_stream(BROKEN, {}):
        chunks.append(c)
    print("合成:", "OK" if chunks else "FAIL", len(b"".join(chunks)), "bytes", flush=True)


asyncio.run(main())