"""Provider 工厂：读 .env 配置选择实现，换接口只改配置不动管道

用法（main.py）：
    from providers import get_asr, get_tts, get_llm
    asr = get_asr(); tts = get_tts(); llm = get_llm()

配置（backend/.env）：
    ASR_PROVIDER=ali          # 当前实现：阿里云 qwen3-asr
    TTS_PROVIDER=ali          # 当前实现：阿里云 qwen3-tts；minimax=MiniMax Speech 2.8
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
    if provider == "minimax":
        from .minimax_tts import MiniMaxTTS

        return MiniMaxTTS()
    raise RuntimeError(f"未知 TTS_PROVIDER: {provider}（可用: ali, minimax）")


def get_llm():
    provider = os.getenv("LLM_PROVIDER", "deepseek")
    if provider == "deepseek":
        from .llm import DeepSeekLLM

        return DeepSeekLLM()
    if provider == "qwen":
        from .llm import QwenLLM

        return QwenLLM()
    raise RuntimeError(f"未知 LLM_PROVIDER: {provider}（可用: deepseek, qwen）")


# ── LLM 按模式选择 ─────────────────────────────────────
# 工作模式默认 DeepSeek（工具调用更稳，用户确认）；闲聊跟随 LLM_PROVIDER。
# WORK_MODE_LLM_PROVIDER 可覆盖工作模式的供应商（deepseek|qwen）。
_llm_mode_cache: dict[str, object] = {}


def get_llm_for_mode(mode: str | None = None):
    """按会话模式返回 LLM 实例（按 provider 缓存，改 .env 后重启生效）：
      - mode == "work" → WORK_MODE_LLM_PROVIDER（默认 deepseek）
      - 其他（chat / None）→ LLM_PROVIDER（默认 deepseek，当前 .env 为 qwen）
    """
    from . import llm as _llm_mod

    provider = os.getenv("WORK_MODE_LLM_PROVIDER", "deepseek") if mode == "work" \
        else os.getenv("LLM_PROVIDER", "deepseek")
    if provider == "deepseek":
        cls = _llm_mod.DeepSeekLLM
    elif provider == "qwen":
        cls = _llm_mod.QwenLLM
    else:
        raise RuntimeError(f"未知 LLM provider: {provider}（可用: deepseek, qwen）")
    inst = _llm_mode_cache.get(provider)
    if inst is None:
        inst = cls()
        _llm_mode_cache[provider] = inst
    return inst
