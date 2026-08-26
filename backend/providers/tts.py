"""TTS Provider：阿里云 Qwen-TTS 流式版（迁自 tts_engine.py）

用 dashscope 的 QwenTtsRealtime 类，实现真正的流式合成：
- WebSocket 连接，边合成边返回音频增量（response.audio.delta）
- 支持 instructions 指令控制语气（配合情绪标签）
- 首包延迟大幅降低

API Key 从环境变量 DASHSCOPE_API_KEY 读取。
"""

import os
import threading

from dotenv import load_dotenv
import dashscope
from dashscope.audio.qwen_tts_realtime import QwenTtsRealtime, QwenTtsRealtimeCallback, AudioFormat

from voice_style import EMOTION_INSTRUCTIONS
from .base import TTSProvider

load_dotenv()

DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY")
# 改用 instruct 版本：支持自然语言指令控制情感/风格
TTS_MODEL = os.getenv("TTS_MODEL", "qwen3-tts-instruct-flash-realtime")
# 音色：Mochi（沙小弥）—— 聪明伶俐的小大人，童真未泯
VOICE_ID = os.getenv("TTS_VOICE", "Mochi")

# 显式设置 dashscope 的 api key
if DASHSCOPE_API_KEY:
    dashscope.api_key = DASHSCOPE_API_KEY


class AliyunTTS(TTSProvider):
    def __init__(self):
        if not DASHSCOPE_API_KEY:
            raise RuntimeError("未配置 DASHSCOPE_API_KEY，请在 backend/.env 里填写")
        self.cancel_event = threading.Event()  # 全局取消标志，打断时设置
        self.first_audio_time = None  # 暴露给 main.py 读取（TTS 首包时间）

    def cancel(self):
        """打断时调用：立即停止当前合成"""
        self.cancel_event.set()

    # ── 朗读文本预处理：把 LLM 可能输出的特殊符号转成口语读法 ──
    # 直接原样给 TTS，Qwen3-TTS 可能把 `+ - * / % ** ()` 等按字面/英文读（实测读出奇怪内容）。
    # 这里在合成前做最小化清洗，避免改变语义：
    #   · 数学运算符号 → 中文读法（3+5=8 → 三加五等于八）
    #   · markdown 强调符号（**、*、#、`）→ 去除
    #   · 列表符号（- 、* 、1. ）→ 去除（读列表语气词已由 LLM 输出）
    #   · 括号内的英文/代码残留不易判断，只处理明确的数学场景
    _SYMBOL_MAP = {
        "+": "加",
        "-": "减",
        "*": "乘",
        "/": "除以",
        "=": "等于",
        "%": "百分之",
        "×": "乘",
        "÷": "除以",
        "＋": "加",
        "－": "减",
        "＝": "等于",
        "＞": "大于",
        "＜": "小于",
        "≥": "大于等于",
        "≤": "小于等于",
        "≠": "不等于",
    }

    def _clean_text(self, text: str) -> str:
        if not text:
            return text
        import re as _re
        t = text
        # 0) 数学运算先处理（保护 * / 等算符不被下面 markdown 删除误伤）
        #    用较严正则避免误伤正文里的 "- "（列表）或 "a-b"（英文名）
        #    注意中括号里的 * 要转义（正则特殊字符）
        t = _re.sub(
            r"(\d+(?:\.\d+)?)\s*([+*\-/×÷＋－＝=<>]{1,2})\s*(\d+(?:\.\d+)?)",
            lambda m: f"{m.group(1)}{self._SYMBOL_MAP.get(m.group(2).strip(), m.group(2))}{m.group(3)}",
            t,
        )
        # 1) markdown 符号（**加粗**、`code`、# 标题、~~删除~~、图片）→ 去除
        t = _re.sub(r"`{1,3}|[#]{1,3}|\*{1,3}|_{1,3}|~{1,3}", "", t)
        t = _re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t)
        # 3) 百分数：5% → 百分之五（把 % 移到数字前，读"百分之五"）
        t = _re.sub(
            r"(\d+(?:\.\d+)?)\s*%",
            lambda m: f"百分之{m.group(1)}",
            t,
        )
        # 4) 列表行首符号："- 第一点" / "* 第一点" / "1. 第一点" → 去掉符号保留内容
        t = _re.sub(r"(?m)^[\s]*[-*•]\s+", "", t)
        return t

    @staticmethod
    def _normalize_num(s: str) -> str:
        return s  # 数字本身 Qwen 会读对，不需要转换；保留原样

    async def synth_stream(self, text: str, params: dict | None = None):
        """流式合成：边合成边 yield 音频块（PCM bytes）

        用法：
            async for chunk in tts.synth_stream(text, params):
                ...
        """
        import asyncio
        import queue
        import base64
        import time

        # 每句合成开始时清空取消标志（恢复首次提交的逻辑）
        # 这样打断信号 cancel_event 是「当前句」级信号：
        # 打断 → cancel_event=True → 当前句循环 break
        # 下一句开始时 clear() → 但 handle_user_speech 的 abort_speaking 已阻止走到下一句
        self.cancel_event.clear()

        params = params or {}
        # 兼容两种参数格式：
        #   A. 情绪状态机输出（emotion_state.EmotionState.get_tts_params）：
        #      {instructions, speech_rate, volume, pitch_rate}
        #   B. 旧格式：{"emotion": "开心"} → 查 EMOTION_INSTRUCTIONS 兜底
        if "speech_rate" in params:
            instruction = params.get("instructions") or EMOTION_INSTRUCTIONS["平静"]
            speech_rate = params.get("speech_rate")
            volume = params.get("volume")
            pitch_rate = params.get("pitch_rate")
        else:
            emotion = params.get("emotion", "平静")
            instruction = EMOTION_INSTRUCTIONS.get(emotion, EMOTION_INSTRUCTIONS["平静"])
            speech_rate = None
            volume = None
            pitch_rate = None

        # ── 用户语音设置真实应用（voice:settings：音色→真实音色 id + 语气前缀；音量/音调→数值参数）──
        voice_id = VOICE_ID  # 默认音色（.env TTS_VOICE），应用失败时兜底
        try:
            from voice_settings import apply_to_tts_params
            merged = apply_to_tts_params({
                "instructions": instruction or "",
                "volume": 50 if volume is None else volume,
                "pitch_rate": 1.0 if pitch_rate is None else pitch_rate,
            })
            instruction = merged.get("instructions") or instruction
            volume = merged.get("volume")
            pitch_rate = merged.get("pitch_rate")
            # 用户选择的真实音色（voice:settings → voice_catalog 里的音色 id）
            voice_id = merged.get("voice") or VOICE_ID
        except Exception as e:
            print(f"[tts] voice:settings 应用失败（忽略）: {e}")

        audio_queue = queue.Queue()
        done_event = threading.Event()
        t_start = time.time()
        first_audio_time = {"t": None}  # 首包到达时间（相对 t_start）
        self.first_audio_time = None  # 每句重置，暴露给 main.py

        class _Callback(QwenTtsRealtimeCallback):
            def on_open(self):
                pass

            def on_event(self, message):
                # 打印事件类型，排查 QwenTTS 实际返回的事件
                if isinstance(message, dict):
                    ev_type = message.get("type", "?")
                    if ev_type != "response.audio.delta":
                        print(f"[TTS事件] {ev_type}")
                # message 是 dict，音频增量在 response.audio.delta 里
                if isinstance(message, dict) and message.get("type") == "response.audio.delta":
                    delta = message.get("delta", "")
                    # delta 是 base64 编码的音频数据
                    if delta:
                        # 记录首包时间（第一次收到音频）
                        if first_audio_time["t"] is None:
                            first_audio_time["t"] = time.time() - t_start
                        try:
                            audio_queue.put(("data", base64.b64decode(delta)))
                        except Exception:
                            pass
                elif isinstance(message, dict) and message.get("type") == "response.done":
                    audio_queue.put(("done", None))
                    done_event.set()
                elif isinstance(message, dict) and "done" in message.get("type", ""):
                    # 兼容其他 done 类型（如 response.audio.done）
                    audio_queue.put(("done", None))
                    done_event.set()

            def on_close(self):
                audio_queue.put(("done", None))
                done_event.set()

        # 创建 realtime TTS 实例
        tts = QwenTtsRealtime(
            model=TTS_MODEL,
            callback=_Callback(),
        )

        def _run():
            try:
                tts.connect()
                # 配置会话：音色、格式、语气指令 + 情绪数值参数（语速/音量/音调）
                # SDK 对 None 参数不覆盖默认值（speech_rate/volume/pitch_rate 未设置时保持默认）
                tts.update_session(
                    voice=voice_id,
                    response_format=AudioFormat.PCM_24000HZ_MONO_16BIT,  # 24kHz PCM
                    speech_rate=speech_rate,
                    volume=volume,
                    pitch_rate=pitch_rate,
                    instructions=instruction,
                )
                # 发送文本并提交（先清洗特殊符号 → 口语读法）
                tts.append_text(self._clean_text(text))
                tts.commit()
                # 等待 done 信号（由 on_event 设置），不消费音频队列
                done_event.wait(timeout=15)
                tts.close()
            except Exception as e:
                audio_queue.put(("error", str(e)))
                done_event.set()

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()

        # 只由这里消费音频块（唯一消费者）
        # 用短轮询（50ms）替代长阻塞，这样打断信号能即时生效
        while True:
            # 检查是否被取消（打断）
            if self.cancel_event.is_set():
                done_event.set()
                break

            try:
                kind, payload = audio_queue.get(timeout=0.05)  # 50ms 短轮询
            except queue.Empty:
                continue

            if kind == "data":
                # 首包数据到达时，记录首包时间
                if first_audio_time["t"] is not None and self.first_audio_time is None:
                    self.first_audio_time = round(first_audio_time["t"], 2)
                yield payload
            elif kind == "done":
                break
            elif kind == "error":
                print(f"[TTS流式错误] {payload}")
                break

    async def speak_and_send(self, ws, text: str, session_id: str, params: dict | None = None):
        """流式合成并发送给前端"""
        print(f"[TTS] 开始合成: {text[:30]}")
        await ws.send_json(
            {"type": "tts_start", "session_id": session_id, "text": text}
        )
        try:
            async for chunk in self.synth_stream(text, params):
                await ws.send_bytes(chunk)
        except Exception as e:
            print(f"[TTS] 合成异常: {e}")
            raise
        print(f"[TTS] 合成完成并发送")
        await ws.send_json({"type": "tts_end", "session_id": session_id})
