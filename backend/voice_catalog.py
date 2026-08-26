"""TTS 音色目录（voice:voices）— 按当前 TTS 模型返回可选音色列表

阿里云 Qwen-TTS 常用音色：id 为接口值（传给 TTS 的 voice 参数），label 为人设说明。
当前使用的音色（backend/.env 的 TTS_VOICE）若不在目录里，会动态补到首位。
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

# 阿里云 Qwen-TTS 常用音色（可在控制面板「语音参数设置 / 模型配置」里选择）
# ⚠️ 已用 tests/probe_tts_voice_list.py + probe_tts_ext_voices.py 实测（qwen3-tts-instruct-flash-realtime）：
#    以下 6 个真实可用（含官网「四月/Maia」），其余（Lelian/Amber/Vinnie/Harlem/Lucy/Siyue 等）服务端不识别、无声。
#    换模型（如 CosyVoice）后请重跑探针脚本刷新本表。
_TTS_VOICES = [
    {"id": "Cherry", "label": "Cherry · 甜美女声"},
    {"id": "Serena", "label": "Serena · 温柔女声"},
    {"id": "Chelsie", "label": "Chelsie · 活泼女声"},
    {"id": "Ethan", "label": "Ethan · 沉稳男声"},
    {"id": "Mochi", "label": "Mochi · 沙小弥（聪明伶俐）"},
    {"id": "Maia", "label": "Maia · 四月（知性与温柔的碰撞）"},
]


def _voice_ids() -> set[str]:
    return {v["id"] for v in _TTS_VOICES}


def current_voice() -> str:
    return os.getenv("TTS_VOICE", "Mochi") or "Mochi"


def list_voices(model: str | None = None) -> dict:
    """返回 { model, current, voices } 结构。"""
    current = current_voice()
    voices = list(_TTS_VOICES)
    if current not in _voice_ids():
        voices.insert(0, {"id": current, "label": f"{current}（当前）"})
    return {
        "model": model or os.getenv("TTS_MODEL", ""),
        "current": current,
        "voices": voices,
    }


def is_valid_voice(voice: str) -> bool:
    return voice in _voice_ids() or voice == "default"
