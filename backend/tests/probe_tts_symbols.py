"""探针：Qwen3-TTS 对特殊符号的真实读音（定位"读数奇怪"的具体场景）

合成下面这些 LLM 可能输出的文本，听/看行为：
- 数学：3+5=8, 7*9, x/y, 10-3, 5%
- markdown：**加粗**, - 列表, # 标题
- 代码/路径：a/b/c, file.txt, 19.99元
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ".")

os.environ["TTS_PROVIDER"] = "ali"
os.environ["TTS_MODEL"] = "qwen3-tts-instruct-flash-realtime"

from providers.tts import AliyunTTS  # noqa: E402


async def synth(text):
    t = AliyunTTS()
    n = 0
    t0 = time.time()
    try:
        async for c in t.synth_stream(text, {"instructions": "用平静温和、自然的语气说"}):
            n += len(c)
    except Exception as e:
        print(f"  [{text[:20]}] 异常: {type(e).__name__}: {e}", flush=True)
        return
    print(f"  [{text!r}] → 音频 {n//1024}KB, 首包 {t.first_audio_time}s", flush=True)


async def main():
    cases = [
        "3加5等于8。",
        "3+5=8。",
        "7乘以9等于63。",
        "7*9=63。",
        "10减去3等于7。",
        "10-3=7。",
        "5除以2等于2.5。",
        "5/2=2.5。",
        "折扣是5%。",
        "这个文件在 a/b/c 目录下。",
        "标题：**重点内容**。",
        "- 第一点\n- 第二点",
        "19.99元。",
        "code_func(x, y) 返回值。",
    ]
    for c in cases:
        await synth(c)
        await asyncio.sleep(0.2)
    print("\n[done]", flush=True)


if __name__ == "__main__":
    asyncio.run(main())