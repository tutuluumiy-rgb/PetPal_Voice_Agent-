"""语音参数设置（voice:settings）— 真实持久化 + TTS 应用

- 落盘：backend/data/voice_settings.json
- 应用（providers/tts.py 调用本模块）：
    voice  (音色 id，见 voice_catalog；'default'/旧预设名 → 用 .env 的 TTS_VOICE)
           → 写入 TTS 参数 voice（真实音色）
    volume (0-100)                    → TTS volume（真实参数，范围一致）
    pitch  (0-100)                    → pitch_rate（映射 0.6~1.4，50→1.0）
- 语气指令前缀：兼容旧的 default/cute/calm/bright 预设；真实音色用中性语气。
"""

from __future__ import annotations

import json
import os

from dotenv import load_dotenv
from voice_catalog import list_voices, current_voice, is_valid_voice

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
SETTINGS_PATH = os.path.join(DATA_DIR, "voice_settings.json")

DEFAULT_SETTINGS = {"volume": 80, "pitch": 50, "voice": "default"}

# 旧「语气预设」→ 指令前缀（兼容已保存的 default/cute/calm/bright；新版本用真实音色）
VOICE_PRESET_INSTRUCT = {
    "default": "用自然温和的语气说",
    "cute": "用软萌可爱、甜一点的语气说",
    "calm": "用沉稳舒缓的语气说",
    "bright": "用明亮轻快的语气说",
}
NEUTRAL_INSTRUCT = "用自然温和的语气说"


def _resolve_voice(voice: str) -> str:
    """把设置的 voice 解析成真正传给 TTS 的音色 id：
    - 'default' 或未知值 → 用当前 .env 的 TTS_VOICE（真实音色）
    - 真实音色 id → 直接用
    """
    if voice in ("default", "") or voice not in {v["id"] for v in list_voices()["voices"]}:
        return current_voice()
    return voice


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
    if voice in {v["id"] for v in list_voices()["voices"]} or voice in VOICE_PRESET_INSTRUCT:
        out["voice"] = voice
    else:
        # 未知音色 → 保持默认音色（当前 .env 的 TTS_VOICE）
        out["voice"] = "default"
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"[voice_settings] 落盘失败: {e}")
    return out


def apply_to_tts_params(params: dict) -> dict:
    """把用户设置合入 TTS 参数（providers/tts.py 每句合成前调用）。

    覆盖规则：
    - voice：真实音色参数（'default'/预设 → 用 .env TTS_VOICE），并合并语气指令前缀
    - volume/pitch 以用户设置为准（Qwen TTS 数值参数：volume[0-100]、pitch_rate[0.5-2.0]）
    """
    try:
        vset = load_voice_settings()
        voice = vset.get("voice", "default")
        # 语气指令：旧预设才有对应指令，真实音色用中性语气
        pre = VOICE_PRESET_INSTRUCT.get(voice)
        instr = pre if pre else NEUTRAL_INSTRUCT
        if instr:
            base = params.get("instructions") or ""
            params["instructions"] = f"{instr}。{base}" if base else instr
        params["volume"] = int(vset.get("volume", 80))
        params["pitch_rate"] = round(0.6 + int(vset.get("pitch", 50)) / 100 * 0.8, 2)
        # 真实音色 id → TTS voice 参数（tts.py 里用 merged['voice'] 覆盖 VOICE_ID）
        params["voice"] = _resolve_voice(voice)
    except Exception as e:
        print(f"[voice_settings] 应用失败（忽略）: {e}")
    return params
