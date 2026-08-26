"""情绪状态机：情绪标签 → TTS 参数映射 + 状态平滑（独立文件，便于维护）

方案：LLM 只输出 [情绪] 标签（零延迟），数值参数（语速/音量/音调）由状态机本地映射：
- 每轮 LLM 情绪标签 → 更新状态机 current
- 状态随时间向「平静」平滑衰减（拟人化：情绪不会瞬间消失，也不会一直持续）
- get_tts_params() 输出当前情绪的一组 TTS 参数（含衰减插值）

修改点：
- 调情绪的参数：改 EMOTION_PARAMS（instructions/speech_rate/volume/pitch_rate）
- 调衰减速度：改 DECAY_SECONDS
"""

import time

# 情绪 → TTS 参数（instruct 指令 + 数值参数）
# ⚠️ instructions 字段：仅阿里云 Qwen-TTS 用（TTS_PROVIDER=ali）；MiniMax 只读 speech_rate/volume/pitch_rate/emotion
# speech_rate: 语速 [0.5~2.0]；volume: 音量 [0~100]；pitch_rate: 音调 [0.5~2.0]
EMOTION_PARAMS = {
    "开心": {
        "instructions": "用开心雀跃、声音上扬的语气说，像小猫咪开心时那样轻快",
        "speech_rate": 1.18,
        "volume": 60,
        "pitch_rate": 1.12,
    },
    "兴奋": {
        "instructions": "用激动兴奋、语速稍快的语气说，充满惊喜",
        "speech_rate": 1.25,
        "volume": 65,
        "pitch_rate": 1.2,
    },
    "好奇": {
        "instructions": "用好奇疑惑、带点探询的语气说，尾音上扬",
        "speech_rate": 1.14,
        "volume": 55,
        "pitch_rate": 1.05,
    },
    "委屈": {
        "instructions": "用委屈巴巴、声音软下来带点撒娇的语气说，像要哭又没哭",
        "speech_rate": 0.95,
        "volume": 45,
        "pitch_rate": 0.95,
    },
    "难过": {
        "instructions": "用低落难过、声音轻下去的语气说，慢慢地说",
        "speech_rate": 0.90,
        "volume": 42,
        "pitch_rate": 0.92,
    },
    "困": {
        "instructions": "用慵懒犯困、慢悠悠拖长音的语气说，像刚睡醒",
        "speech_rate": 0.80,
        "volume": 40,
        "pitch_rate": 0.90,
    },
    "平静": {
        "instructions": "用平静温和、自然的语气说",
        "speech_rate": 1.1,
        "volume": 50,
        "pitch_rate": 1.0,
    },
}

# 情绪向「平静」衰减的时长（秒）：距上次情绪更新这么久后，完全回到平静
DECAY_SECONDS = 30.0


class EmotionState:
    """宠物情绪状态机：跨轮保持情绪 + 随时间向平静平滑衰减"""

    def __init__(self):
        self.current = "平静"      # 当前情绪
        self.last_update = time.time()  # 最近一次情绪更新时间戳

    def update(self, emotion: str):
        """LLM 每轮情绪标签更新（仅接受白名单情绪，其他忽略）"""
        if emotion in EMOTION_PARAMS:
            self.current = emotion
            self.last_update = time.time()

    def _decay_ratio(self) -> float:
        """情绪衰减比例：0 = 完全保持当前情绪；1 = 完全回到平静（线性）"""
        elapsed = time.time() - self.last_update
        if DECAY_SECONDS <= 0:
            return 0.0
        return min(1.0, elapsed / DECAY_SECONDS)

    def get_tts_params(self) -> dict:
        """返回当前 TTS 参数（含衰减插值：情绪参数 ↔ 平静参数 按衰减比例过渡）
        - 含 emotion 字段（中文情绪标签），供 TTS provider 精确映射情绪（MiniMax 等）
        """
        cur = EMOTION_PARAMS.get(self.current, EMOTION_PARAMS["平静"])
        base = EMOTION_PARAMS["平静"]
        ratio = self._decay_ratio()

        if ratio <= 0:
            out = dict(cur)
            out["emotion"] = self.current
            return out  # 完全保持当前情绪
        if ratio >= 1:
            self.current = "平静"  # 衰减完成，状态归位平静
            out = dict(base)
            out["emotion"] = "平静"
            return out

        # 线性插值：数值参数向平静过渡；instructions 保持当前情绪（语气描述不插值）
        params = dict(cur)
        for key in ("speech_rate", "volume", "pitch_rate"):
            params[key] = round(base[key] + (cur[key] - base[key]) * (1 - ratio), 2)
        params["emotion"] = self.current
        return params

    @property
    def emotion_label(self) -> str:
        return self.current
