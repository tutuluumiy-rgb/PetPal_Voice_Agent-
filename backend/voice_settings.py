"""语音参数设置（voice:settings）— 真实持久化 + TTS 应用

- 落盘：backend/data/voice_settings.json
- 应用（providers/tts.py 调用本模块）：
    voice  (default/cute/calm/bright) → 语气指令前缀（instruct 自然语言）
    volume (0-100)                    → TTS volume（真实参数，范围一致）
    pitch  (0-100)                    → pitch_rate（映射 0.6~1.4，50→1.0）
"""

from __future__ import annotations

import json
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
SETTINGS_PATH = os.path.join(DATA_DIR, "voice_settings.json")

DEFAULT_SETTINGS = {"volume": 80, "pitch": 50, "voice": "default"}

# 音色 → 语气指令前缀（真实拼进 TTS instruct）
VOICE_PRESET_INSTRUCT = {
    "default": "用自然温和的语气说",
    "cute": "用软萌可爱、甜一点的语气说",
    "calm": "用沉稳舒缓的语气说",
    "bright": "用明亮轻快的语气说",
}

VOICE_NAMES = tuple(VOICE_PRESET_INSTRUCT.keys())


def load_voice_settings() -> dict:
    """读取设置；缺失/损坏退回默认（并确保目录存在）"""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        out = dict(DEFAULT_SETTINGS)
        if isinstance(data, dict):
            for k in ("volume", "pitch", "voice"):
                if k in data:
                    out[k] = data[k]
        return out
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_SETTINGS)


def save_voice_settings(settings: dict) -> dict:
    """校验并写回，返回规范化后的设置"""
    out = dict(DEFAULT_SETTINGS)
    try:
        out["volume"] = max(0, min(100, int(float(settings.get("volume", 80)))))
    except (TypeError, ValueError):
        pass
    try:
        out["pitch"] = max(0, min(100, int(float(settings.get("pitch", 50)))))
    except (TypeError, ValueError):
        pass
    voice = settings.get("voice")
    if voice in VOICE_NAMES:
        out["voice"] = voice
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"[voice_settings] 落盘失败: {e}")
    return out


def apply_to_tts_params(params: dict) -> dict:
    """把用户设置合入 TTS 参数（providers/tts.py 每句合成前调用）。

    覆盖规则：voice 前缀合并进 instructions；volume/pitch 以用户设置为准
    （是真实 Qwen TTS 数值参数：volume[0-100]、pitch_rate[0.5-2.0]）。
    """
    try:
        vset = load_voice_settings()
        pre = VOICE_PRESET_INSTRUCT.get(vset.get("voice", "default"))
        if pre:
            instr = params.get("instructions") or ""
            params["instructions"] = f"{pre}。{instr}" if instr else pre
        params["volume"] = int(vset.get("volume", 80))
        params["pitch_rate"] = round(0.6 + int(vset.get("pitch", 50)) / 100 * 0.8, 2)
    except Exception as e:
        print(f"[voice_settings] 应用失败（忽略）: {e}")
    return params