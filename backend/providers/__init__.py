"""Provider 工厂：读 .env 配置选择实现，换接口只改配置不动管道

用法（main.py）：
    from providers import get_asr, get_tts, get_llm
    asr = get_asr(); tts = get_tts(); llm = get_llm()

配置（backend/.env）：
    ASR_PROVIDER=ali          # 当前实现：阿里云 qwen3-asr
    TTS_PROVIDER=ali          # 当前实现：阿里云 qwen3-tts
    LLM_PROVIDER=deepseek     # 当前实现：DeepSeek
新增接口：在 providers/ 下写新实现类，注册到对应 get_*() 的映射即可。
"""

import os

from dotenv import load_dotenv

load_dotenv()  # 读取 backend/.env


def get_asr():
    provider = os.getenv("ASR_PROVIDER", "ali")
    if provider == "ali":
        from .asr import AliyunASR

        return AliyunASR()
    raise RuntimeError(f"未知 ASR_PROVIDER: {provider}（可用: ali）")


def get_tts():
    provider = os.getenv("TTS_PROVIDER", "ali")
    if provider == "ali":
        from .tts import AliyunTTS

        return AliyunTTS()
    raise RuntimeError(f"未知 TTS_PROVIDER: {provider}（可用: ali）")


def get_llm():
    provider = os.getenv("LLM_PROVIDER", "deepseek")
    if provider == "deepseek":
        from .llm import DeepSeekLLM

        return DeepSeekLLM()
    if provider == "qwen":
        from .llm import QwenLLM

        return QwenLLM()
    raise RuntimeError(f"未知 LLM_PROVIDER: {provider}（可用: deepseek, qwen）")
