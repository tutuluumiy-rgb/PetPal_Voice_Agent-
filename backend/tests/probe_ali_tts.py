"""探针：Aliyun Qwen3-TTS-Instruct-Flash-Realtime 合成测试

直接调 AliyunTTS（providers/tts.py）合成一段文本，验证：
- 模型能否正常连接/合成
- 首包时间
- 音频是否有效（非空）
- instruct 参数（情绪指令）是否接受
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


async def synth(text: str, params: dict | None = None, label: str = ""):
    t = AliyunTTS()
    chunks = []
    n = 0
    t0 = time.time()
    try:
        async for c in t.synth_stream(text, params):
            chunks.append(c)
            n += len(c)
    except Exception as e:
        print(f"  [{label}] 异常: {type(e).__name__}: {e}", flush=True)
        return None
    dt = time.time() - t0
    fa = t.first_audio_time
    print(f"  [{label}] 首包={fa}s 整句={dt:.2f}s 音频={n}B", flush=True)
    return chunks


async def main():
    # 1. 平铺直叙（无参数）
    await synth("你好呀，今天过得怎么样？", None, "无参数")
    # 2. 带情绪指令（开心）
    await synth("今天天气真不错呢！", {"instructions": "用开心雀跃、声音上扬的语气说"}, "开心指令")
    # 3. 带情绪状态机全套参数（speech_rate/volume/pitch_rate）
    await synth("这个问题其实不太好回答。", {"instructions": "用平静温和、自然的语气说", "speech_rate": 1.1, "volume": 50, "pitch_rate": 1.0}, "平静全套")
    print("\n[done]", flush=True)


if __name__ == "__main__":
    asyncio.run(main())