"""AI 宠物「年年」语音管道后端

管道：麦克风 → AEC(浏览器) → VAD(前端Silero体感 + 后端Silero确认) → ASR → LLM → TTS → 扬声器
本文件负责 WebSocket 音频流接入和整体编排。

双层打断架构：
- 前端 Silero VAD：体感层（敏感，快速 ducking + 上报事件）
- 后端 Silero VAD：业务层（准确，二次确认人声/噪声后决策打断）
"""

import asyncio
import json
import os
import uuid

import numpy as np
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

load_dotenv()  # 读取 backend/.env

from asr_engine import StreamingASR
from llm_engine import ChatEngine
from tts_engine import TTSEngine
from vad_engine import SileroVAD

app = FastAPI(title="Ball Ball Pet Voice Pipeline")

# ══════════════════════════════════════════════════════════
# 🎛 后端双层打断参数区 —— 集中调这里！
# 架构：前端 Silero VAD（体感层，敏感）→ 后端 Silero VAD（业务层，准确）
# ══════════════════════════════════════════════════════════

# 【采样率】前端录音采样率，前后端保持一致
SAMPLE_RATE = 16000

# 【说话后静默保护期】球球说完话后多少帧内跳过处理（等混响尾音衰减）
# 单位：帧，1帧 = 30ms（后端音频分帧）
# 调大 → 更安全防回声；调小 → 用户接话响应快
POST_SPEECH_GUARD_FRAMES = 10

# ── 后端 Silero VAD 二次确认参数 ──────────────────────
# 【人声概率阈值】后端判定一帧为「人声」的 Silero 概率阈值
# 前端AEC会消掉大部分插话声，后端缓存里的人声被弱化，所以阈值不能太严
# 调大 → 更严格（只认真人声）；调小 → 更敏感
BACKEND_VAD_THRESHOLD = 0.45

# 【二次确认人声帧占比】缓存音频里，人声帧占总帧数的比例阈值
# 真人声「大部分帧都是人声」，噪声/回声则零星几帧
# 前端AEC弱化后，人声占比会偏低（实测0.07~0.33波动），阈值要够低
# 调大 → 更严格；调小 → 更敏感
CONFIRM_SPEECH_RATIO = 0.05

# 【二次确认最小缓存时长】缓存音频至少多少毫秒才做二次确认
# 缓存太短（<100ms）无法可靠判断，直接拒绝
CONFIRM_MIN_AUDIO_MS = 100

# 【二次确认取音频窗口】只取缓存里「最近」多少毫秒做二次确认
# 原因：球球说话期间，缓存里大部分是球球回声（AEC消过但能量低），
#       你的插话声只在最后。取整个缓存会导致人声占比被回声稀释（如3%）。
#       所以只取「最近」这一段（含你的插话声）。
# 注意：与前端预卷回退量(PRE_ROLL_MS=256)对齐，窗口太宽会混入回声稀释人声占比
# 调大 → 取更多音频，可能混入回声；调小 → 更聚焦你的插话，但可能截断
CONFIRM_WINDOW_MS = 256

# 【Silero 模型路径】后端 VAD 模型（复用前端下载的）
SILERO_MODEL_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "frontend", "vad", "silero_vad.onnx",
)

# ── 全局组件 ──────────────────────────────────────────
backend_vad = SileroVAD(SILERO_MODEL_PATH)  # 后端 Silero VAD（业务层二次确认）
asr = StreamingASR()
llm = ChatEngine()
tts = TTSEngine()


# 【语气词黑名单】ASR 识别结果如果只包含这些词（可重复、可带标点），直接丢弃
# 原因：ASR 会把残余噪声、尾音「幻听」成短语气词，这些词信息量极低
# 自定义：加你想过滤的词，用 | 分隔，注意转义
FILLER_WORDS = "嗯|啊|哦|额|呃|噢|哎|唉|嗯嗯|啊啊|哦哦|额额|呵呵|嘿嘿"

import re as _re


def _is_filler_word(text: str) -> bool:
    """判断文本是否只是语气词（如「嗯」「啊」「嗯嗯」等）"""
    # 去掉标点、空格后，检查是否只由黑名单里的词组成
    cleaned = _re.sub(r"[，。！？、,.!?~～\s]", "", text)
    if not cleaned:
        return True
    # 允许语气词重复，如「嗯嗯」「啊啊啊」
    pattern = _re.compile(f"^({FILLER_WORDS})+$")
    return bool(pattern.match(cleaned))


class ConversationSession:
    """单个对话会话的状态机"""

    def __init__(self):
        self.session_id = str(uuid.uuid4())[:8]
        self.state = "listening"  # listening / thinking / speaking
        self.history = []  # 对话历史（短期记忆）
        self.silence_frames = 0      # 连续静音帧数（用于终点检测）
        self.speech_frames = 0       # 连续人声帧数（用于最短时长确认）
        self.tts_task = None  # 正在播放的 TTS 任务
        self.pending_user_text = ""  # 累积的用户输入
        self.vad_buffer = b""  # VAD 分帧缓冲（累积到 30ms 再喂 VAD）
        self.last_asr_time = 0  # 最近一次 ASR 耗时
        self.frames_since_speech = 0  # 球球说话后的静默保护计数（从0递增）
        self.is_user_speaking = False  # 当前是否已确认用户正在说话
        self.is_barge_in_speaking = False  # 打断场景：是否已确认用户在插话
        self.barge_energy_baseline = None  # 球球说话时的回声能量基线（用于能量尖峰检测）
        self.barge_consecutive_speech = 0  # 打断场景：连续人声帧数（能量触发后的二次确认）
        self.speaking_start_time = None  # 球球本次开始说话的时间戳（用于算打断延迟）
        self.abort_speaking = False  # 打断标志：置 True 时，LLM/TTS 流水线循环退出
        self.speaking_audio_cache = bytearray()  # 球球说话期间缓存的音频（打断时喂ASR，防窗口吞字）
        self.MAX_SPEAKING_CACHE = SAMPLE_RATE * 2 * 2  # 最多缓存2秒（16000Hz * 2字节 * 2秒）
        # 当前轮次的事件流（一轮对话 = 用户说完到球球回复完）
        self.round_id = 0  # 轮次编号
        self.event_start_time = None  # 本轮第一个事件的时间戳（用于相对计时）
        # 累计统计（用于计算平均值）
        self.timing_count = 0
        self.timing_sum = {"asr": 0.0, "llm_first_token": 0.0, "llm_first_sentence": 0.0, "tts_first_packet": 0.0, "e2e": 0.0, "barge_in": 0.0, "total": 0.0}
        self.barge_count = 0  # 打断次数（单独计数，因为不是每轮都打断）

    def reset_episode(self):
        """新一轮对话开始，清空 VAD 状态"""
        self.silence_frames = 0
        self.speech_frames = 0
        self.pending_user_text = ""
        self.is_user_speaking = False
        self.is_barge_in_speaking = False

    def reset_speech_guard(self):
        """球球开始说话/被打断后，重置静默保护计数（从0重新开始保护）"""
        self.frames_since_speech = 0

    async def emit_event(self, ws, stage: str, detail: str = "", duration: float | None = None):
        """发送一个事件流条目到前端"""
        import time as _time
        # 如果是新一轮的第一个事件，记录起始时间
        if self.event_start_time is None:
            self.event_start_time = _time.time()
        # 相对本轮开始的时间
        ts = round(_time.time() - self.event_start_time, 2)
        await ws.send_json({
            "type": "event",
            "round": self.round_id,
            "stage": stage,
            "detail": detail,
            "ts": ts,          # 相对本轮开始的时间
            "duration": duration,  # 该阶段耗时（可选）
        })


@app.get("/health")
async def health():
    return {"status": "ok", "pipeline": "AEC(browser)→VAD→ASR→LLM→TTS"}


@app.websocket("/ws/audio")
async def audio_ws(ws: WebSocket):
    await ws.accept()
    session = ConversationSession()
    await ws.send_json({"type": "ready", "session_id": session.session_id})

    try:
        while True:
            msg = await ws.receive()
            # 二进制 = 音频帧
            if msg.get("bytes"):
                await handle_audio_frame(ws, session, msg["bytes"])
            # 文本 = 控制消息
            elif msg.get("text"):
                await handle_control_message(ws, session, msg["text"])
    except WebSocketDisconnect:
        pass  # 正常断开
    except RuntimeError:
        pass  # Starlette 断开后 receive 抛 RuntimeError，忽略
    except Exception as e:
        print(f"[session {session.session_id}] 异常: {e}")
    finally:
        await cleanup_session(session)


async def handle_audio_frame(ws: WebSocket, session: ConversationSession, pcm: bytes):
    """处理音频：只负责分发，不做端点检测

    双层架构下，端点检测（判断说话起止）完全由前端 VAD 的
    speech_start / speech_end 事件驱动，后端主循环只做：
    1. speaking/thinking 期间：缓存音频（打断时回放，防窗口吞字）
    2. listening 且 is_user_speaking：喂给 ASR 实时识别
    """
    if session.state in ("speaking", "thinking", "pending_play"):
        # 球球说话/思考/待播期间：缓存音频（打断信号到达后回放给 ASR）
        session.speaking_audio_cache.extend(pcm)
        if len(session.speaking_audio_cache) > session.MAX_SPEAKING_CACHE:
            session.speaking_audio_cache = session.speaking_audio_cache[-session.MAX_SPEAKING_CACHE:]
        return

    # listening 状态：如果用户正在说话（前端 speech_start 已触发），喂给 ASR
    if session.is_user_speaking:
        asr.feed(session.session_id, pcm)


def _confirm_real_speech(audio_cache: bytearray) -> bool:
    """后端 Silero VAD 二次确认：判断「首分片」是真人声还是噪声/回声

    audio_cache: 球球说话期间缓存 + 前端预卷 合并后的音频（PCM 16bit）
    二次确认只取最近 CONFIRM_WINDOW_MS 毫秒（最大观察窗口）：
    - 前端预卷(256ms)补VAD触发延迟的首字
    - 后端缓存提供后续实时音频
    取「最近一段」是为了聚焦你的插话声，避免被球球回声稀释。
    返回：True = 确认真人声，应该打断；False = 噪声/回声，拒绝打断
    """
    if len(audio_cache) < int(SAMPLE_RATE * CONFIRM_MIN_AUDIO_MS / 1000 * 2):
        # 缓存太短，无法可靠判断
        print(f"[二次确认] 缓存太短（{len(audio_cache)}字节），拒绝")
        return False

    # 只取「最近 CONFIRM_WINDOW_MS 毫秒」的音频（最大观察窗口）
    # 首分片 = 前端预卷(256ms) + 后端后续音频，取最近一段聚焦你的插话声
    window_bytes = int(SAMPLE_RATE * CONFIRM_WINDOW_MS / 1000 * 2)
    recent_audio = bytes(audio_cache[-window_bytes:]) if len(audio_cache) > window_bytes else bytes(audio_cache)

    # 调试：打印音频特征，确认数据正确
    total_bytes = len(audio_cache)
    recent_bytes = len(recent_audio)
    recent_ms = recent_bytes / 2 / SAMPLE_RATE * 1000
    # 计算 RMS 能量（判断是否有实际声音）
    try:
        import numpy as _np
        recent_np = _np.frombuffer(recent_audio, dtype=_np.int16).astype(_np.float32)
        rms = float(_np.sqrt(_np.mean(recent_np ** 2))) if len(recent_np) > 0 else 0.0
        peak = float(_np.abs(recent_np).max()) if len(recent_np) > 0 else 0.0
        print(f"[二次确认DEBUG] 缓存总{total_bytes}字节({total_bytes/2/16000*1000:.0f}ms), 取最近{recent_bytes}字节({recent_ms:.0f}ms), RMS={rms:.0f}, 峰值={peak:.0f}")
    except Exception as e:
        print(f"[二次确认DEBUG] 能量计算失败: {e}")

    try:
        is_speech, ratio = backend_vad.is_speech(
            recent_audio,
            BACKEND_VAD_THRESHOLD,
            ratio_threshold=CONFIRM_SPEECH_RATIO,
        )
        print(f"[二次确认] 取最近{CONFIRM_WINDOW_MS}ms, 人声帧占比={ratio:.2f}, 阈值={CONFIRM_SPEECH_RATIO}, 结果={'确认人声' if is_speech else '判定噪声'}")
        return is_speech
    except Exception as e:
        print(f"[二次确认] 异常: {e}")
        # 异常时保守处理：拒绝打断（避免误打断）
        return False


async def finish_user_speech(ws: WebSocket, session: ConversationSession):
    """用户说完了（前端 speech_end 事件触发）：跑 ASR 识别，过滤语气词噪声，交给 LLM/TTS"""
    import time
    # 新一轮开始
    session.round_id += 1
    session.event_start_time = None
    await session.emit_event(ws, "VAD", "用户说完（前端 speech_end 事件）")

    t_asr_start = time.time()
    await session.emit_event(ws, "ASR", "开始识别", duration=0)
    print(f"[ASR调试] finalize 前，session_id={session.session_id}")
    text = await asr.finalize(session.session_id)
    print(f"[ASR调试] finalize 返回: {repr(text)}")
    t_asr = time.time() - t_asr_start
    text = (text or "").strip()

    # ── 语气词黑名单过滤 ──
    if not text or _is_filler_word(text):
        await session.emit_event(ws, "ASR", f"识别为语气词『{text}』，已过滤", duration=round(t_asr, 2))
        session.reset_episode()
        # 语气词被过滤，恢复 listening 状态，让主循环继续端点检测
        session.state = "listening"
        session.reset_speech_guard()
        return

    await session.emit_event(ws, "ASR", f"识别结果：{text}", duration=round(t_asr, 2))
    session.last_asr_time = round(t_asr, 2)
    # 通知前端 ASR 完成，收尾流式展示（把 asr_partial 的状态转正为最终结果）
    await ws.send_json({"type": "asr_final", "text": text})
    await handle_user_speech(ws, session, text)
    session.reset_episode()


async def handle_barge_in(ws: WebSocket, session: ConversationSession):
    """打断：立即停止 TTS，球球先应一声「嗯？」"""
    import time
    # 计算打断延迟 = 从球球开始说话到被打断的时间
    barge_latency = None
    if session.speaking_start_time is not None:
        barge_latency = round(time.time() - session.speaking_start_time, 2)
        # 累计平均打断延迟
        session.barge_count += 1
        session.timing_sum["barge_in"] += barge_latency
        avg_barge = round(session.timing_sum["barge_in"] / session.barge_count, 2)
    else:
        avg_barge = None

    print(f"[DEBUG打断] speaking_start_time={session.speaking_start_time}, 延迟={barge_latency}, 平均={avg_barge}")

    await session.emit_event(
        ws, "Barge-in",
        f"检测到用户插话，打断延迟 {barge_latency if barge_latency is not None else '?'}s",
    )
    # 1. 设置 TTS 取消标志（让合成循环立即退出）
    tts.cancel()
    # 2. 取消 asyncio 任务（可能已经在发送中）
    if session.tts_task and not session.tts_task.done():
        session.tts_task.cancel()
    # 3. 设置打断标志，让 handle_user_speech 的逐句流水线退出
    session.abort_speaking = True
    await ws.send_json({"type": "barge_in"})
    # 发送打断延迟平均值到前端
    if avg_barge is not None:
        await ws.send_json({"type": "barge_avg", "avg": avg_barge, "count": session.barge_count})
    # 球球被打断后先回应「嗯？」（后台播放，不阻塞音频接收）
    asyncio.create_task(tts.speak_and_send(ws, "嗯？", session.session_id))
    session.state = "listening"
    # 打断后清空端点检测状态和能量基线，让用户重新开始说
    session.reset_episode()
    session.reset_speech_guard()
    session.barge_energy_baseline = None
    session.barge_consecutive_speech = 0
    session.speaking_start_time = None
    # 打断后设置一个「打断后保护期」，让「嗯？」播放完、残留声音衰减，避免误识别
    session.frames_since_speech = 0  # 从0重新开始保护
    # 用 POST_SPEECH_GUARD_FRAMES 作为打断后的保护时长
    # （「嗯？」约1秒，保护期设大一点，下面这行在参数区调）


async def handle_speech_start(ws: WebSocket, session: ConversationSession, pre_roll_b64: str = None):
    """后端业务决策：前端检测到人声，基于当前状态决定是否打断

    双层架构：前端负责体感（已做 ducking + 上传预卷），后端负责业务决策。
    二次确认输入 = 前端预卷(256ms) + 后端最近音频，最大观察窗口 CONFIRM_WINDOW_MS。
    """
    import time as _t
    _t_recv = _t.time()
    print(f"[状态机] speech_start 到达, 当前 state={session.state}, 到达时刻={_t_recv:.3f}")

    if session.state == "pending_play":
        # 待播态：TTS已下发但喇叭未响，用户说话了 → 直接丢弃待播任务，切 listening
        print(f"[状态机] state=pending_play → 丢弃待播任务，切 listening")
        # 取消当前 TTS 任务（还没播，直接取消）
        if session.tts_task and not session.tts_task.done():
            session.tts_task.cancel()
        session.abort_speaking = True
        # 启动 ASR，接收用户话
        loop = asyncio.get_event_loop()
        partial_buffer = {"text": ""}
        def _on_partial(delta_text):
            partial_buffer["text"] += delta_text
            async def _send():
                await ws.send_json({"type": "asr_partial", "text": partial_buffer["text"]})
            asyncio.run_coroutine_threadsafe(_send(), loop)
        asr.start_streaming(session.session_id, _on_partial)
        session.is_user_speaking = True
        session.state = "listening"
        session.silence_frames = 0
        session.speech_frames = 0
        session.frames_since_speech = POST_SPEECH_GUARD_FRAMES
        # 历史标记：上一轮被打断
        session.history.append({
            "role": "user",
            "content": "(主人打断了球球的上一轮回复，请重新听主人接下来的话)",
        })
        return

    if session.state == "speaking":
        # 球球正在说话 → 需要后端 VAD 二次确认「这是真人声还是噪声/回声」
        print(f"[状态机] state=speaking → 后端 VAD 二次确认")
        # 二次确认 = 前端预卷 + 后端最近音频 合并
        confirm_audio = session.speaking_audio_cache
        if pre_roll_b64:
            try:
                import base64 as _b64
                pre_roll_pcm = _b64.b64decode(pre_roll_b64)
                # 预卷在最后（你开口的那段），拼到缓存末尾
                confirm_audio = confirm_audio + bytearray(pre_roll_pcm)
                print(f"[状态机] 合并前端预卷 {len(pre_roll_pcm)} 字节")
            except Exception as e:
                print(f"[状态机] 预卷解码失败: {e}")

        if not _confirm_real_speech(confirm_audio):
            # 二次确认失败：可能是球球回声或噪声，拒绝打断
            print(f"[状态机] 二次确认失败（噪声/回声），拒绝打断，恢复音量")
            await session.emit_event(ws, "后端VAD", "二次确认判定噪声 → 拒绝打断")
            await ws.send_json({"type": "barge_reject"})
            return

        # 二次确认通过：真打断
        _t_confirm = _t.time()
        print(f"[状态机] 二次确认通过 → 确认真打断, 后端处理耗时={(_t_confirm - _t_recv)*1000:.0f}ms")
        await session.emit_event(ws, "后端VAD", "二次确认判定人声 → 确认真打断")
        print(f"[打断DEBUG] 打断前 tts_task 状态: done={session.tts_task.done() if session.tts_task else 'None'}")
        # 1. 取消 TTS 和 LLM 流水线
        tts.cancel()
        if session.tts_task and not session.tts_task.done():
            session.tts_task.cancel()
        session.abort_speaking = True
        print(f"[打断DEBUG] 已设置 abort_speaking=True 和 cancel tts_task")

        # 2. 通知前端「确认真打断」，前端完全静音
        # 计算后端处理耗时（到达→确认）并随 barge_confirm 传给前端
        _t_now = _t.time()
        _backend_ms = (_t_now - _t_recv) * 1000
        print(f"[打断DEBUG] barge_confirm 已发送, 后端处理耗时={_backend_ms:.0f}ms")
        await ws.send_json({
            "type": "barge_confirm",
            "backend_ms": round(_backend_ms, 1),  # 后端二次确认耗时
        })

        # 3. 启动 ASR 会话，准备接收用户插话内容
        loop = asyncio.get_event_loop()
        partial_buffer = {"text": ""}
        def _on_partial(delta_text):
            partial_buffer["text"] += delta_text
            async def _send():
                await ws.send_json({"type": "asr_partial", "text": partial_buffer["text"]})
            asyncio.run_coroutine_threadsafe(_send(), loop)
        asr.start_streaming(session.session_id, _on_partial)

        # 4. 把球球说话期间缓存的音频喂给 ASR（防窗口吞字）
        if len(session.speaking_audio_cache) > 0:
            asr.feed(session.session_id, bytes(session.speaking_audio_cache))
            session.speaking_audio_cache = bytearray()
            print(f"[状态机] 已喂入 speaking 缓存音频")

        # 5. 进入 listening，重置端点检测，让用户的话能被识别
        session.state = "listening"
        session.is_user_speaking = True
        session.silence_frames = 0
        session.speech_frames = 0
        session.frames_since_speech = POST_SPEECH_GUARD_FRAMES

        # 6. 打断延迟已改为前端上报（barge_latency 消息），这里不再计算
        session.speaking_start_time = None

        # 7. 打断历史标记：让 LLM 不延续旧话题
        session.history.append({
            "role": "user",
            "content": "(主人刚才打断了球球的上一轮回复，请不要再继续上一个话题，重新听主人接下来的话)",
        })

    elif session.state == "thinking":
        # 球球在思考（LLM生成中）→ 用户开口，可能是插话，也可能只是咳嗽
        # 保守处理：先 ducking（前端已做），等 speech_end 确认是不是真说话
        print(f"[状态机] state=thinking → 暂不打断，等 speech_end 确认")

    elif session.state == "listening":
        # 球球没在说话 → 这是正常说话，不是打断
        # 启动 ASR 会话，开始累积用户音频
        print(f"[状态机] state=listening → 正常说话，启动 ASR")
        loop = asyncio.get_event_loop()
        partial_buffer = {"text": ""}
        def _on_partial(delta_text):
            partial_buffer["text"] += delta_text
            async def _send():
                await ws.send_json({"type": "asr_partial", "text": partial_buffer["text"]})
            asyncio.run_coroutine_threadsafe(_send(), loop)
        asr.start_streaming(session.session_id, _on_partial)
        session.is_user_speaking = True
        session.silence_frames = 0
        session.speech_frames = 0


async def handle_speech_end(ws: WebSocket, session: ConversationSession):
    """前端检测到人声结束：触发最终识别"""
    print(f"[状态机] speech_end 到达, 当前 state={session.state}")

    if session.state == "thinking":
        # 思考期间用户开口又结束：可能是咳嗽/短促噪声，也可能是真的要说话
        # 这里简单处理：如果是 thinking 且用户说了话，等下一轮正常识别
        print(f"[状态机] state=thinking → speech_end，忽略（思考期不识别）")
        return

    if session.is_user_speaking:
        # 用户在说话，现在说完了 → 触发最终识别
        print(f"[状态机] 用户说完，触发 finalize")
        await finish_user_speech(ws, session)


async def handle_user_speech(ws: WebSocket, session: ConversationSession, text: str):
    """用户说完一句话：进入思考 → LLM 流式逐句 → TTS 逐句合成播放"""
    import time

    session.state = "thinking"
    t0 = time.time()
    # 用户说的话已由 asr_final 消息展示（ASR 流式收尾），这里不再发 user_text
    await session.emit_event(ws, "LLM", "开始生成回复", duration=0)

    # ── 句子级流水线：LLM 边生成边逐句发 TTS ──
    # 不再等 LLM 生成完才 TTS，而是 LLM 生成第一句就开始合成播放
    full_reply = ""
    emotion = "平静"
    t_llm_first_sentence = None  # LLM 出第一句的时间（衡量首响延迟）

    session.state = "pending_play"  # 待播态：等前端 play_start 才转 speaking
    session.reset_speech_guard()
    session.barge_energy_baseline = None
    # 新一轮回复开始，清空「说话期间音频缓存」（上一轮打断/说话残留的）
    session.speaking_audio_cache = bytearray()
    session.barge_consecutive_speech = 0
    session.speaking_start_time = time.time()
    session.abort_speaking = False  # 新一轮说话，清空打断标志

    # 发「回复开始」信号，前端据此标记 ballIsPlaying=true
    await ws.send_json({"type": "reply_start"})

    t_tts_start = time.time()

    async for sentence, emo in llm.chat_stream(text, session.history):
        # 被打断：停止后续句子的生成和播放
        if session.abort_speaking:
            print(f"[打断DEBUG] 循环检测到 abort_speaking，退出流水线")
            break

        if full_reply == "":
            emotion = emo
            t_llm_first_sentence = time.time() - t0
            # 第一句：通知前端显示完整回复的开头（后续追加）
            await ws.send_json({"type": "reply", "text": sentence, "emotion": emotion})
        else:
            # 后续句子：追加显示
            await ws.send_json({"type": "reply_append", "text": sentence})

        full_reply += sentence

        # 逐句合成 TTS（顺序播放，保证句子顺序正确）
        await session.emit_event(ws, "TTS", f"合成: {sentence[:20]}", duration=0)
        tts_params = {"emotion": emotion}
        session.tts_task = asyncio.create_task(
            tts.speak_and_send(ws, sentence, session.session_id, tts_params)
        )
        try:
            await session.tts_task  # 等这句播完再播下一句（保证顺序）
        except asyncio.CancelledError:
            # 这句被打断取消，退出流水线
            print(f"[打断DEBUG] 当前句 TTS 被取消，退出流水线")
            break
        except Exception:
            # WebSocket 断开等异常，退出流水线，避免后台任务抛未捕获异常
            return

    t_llm = time.time() - t0
    t_tts = time.time() - t_tts_start

    # 记录对话历史
    session.history.append({"role": "user", "content": text})
    session.history.append({"role": "assistant", "content": full_reply})

    await session.emit_event(
        ws, "LLM",
        f"首字 {getattr(llm, 'first_token_time', 0)}s，首句 {round(t_llm_first_sentence or 0,2)}s，完成 {round(t_llm,2)}s，情绪[{emotion}]",
        duration=round(t_llm, 2),
    )

    # ── 统计本轮耗时，发送看板数据 ──
    tts_first = getattr(tts, "first_audio_time", None)  # TTS首包（第一句合成首包）
    # 端到端首响 = ASR + LLM首字 + LLM生成第一句 + TTS首包
    # t_llm_first_sentence 是「LLM开始到第一句生成完」的时间，包含首字+生成第一句
    e2e = round(
        getattr(session, "last_asr_time", 0)
        + (t_llm_first_sentence if t_llm_first_sentence else getattr(llm, "first_token_time", 0))
        + (tts_first if tts_first else 0),
        2,
    )
    current = {
        "asr": getattr(session, "last_asr_time", 0),
        "llm_first_token": getattr(llm, "first_token_time", 0),
        "llm_first_sentence": round(t_llm_first_sentence or 0, 2),  # 新增：LLM首句
        "tts_first_packet": tts_first if tts_first else 0,
        "e2e": e2e,
        "total": round(getattr(session, "last_asr_time", 0) + t_llm + t_tts, 2),
    }
    session.timing_count += 1
    avg = {}
    for k, v in current.items():
        session.timing_sum[k] += v
        avg[k] = round(session.timing_sum[k] / session.timing_count, 2)

    # 发「回复结束」信号，前端据此标记 ballIsPlaying=false
    await ws.send_json({"type": "reply_end"})

    try:
        await ws.send_json({
            "type": "timing",
            "current": current,
            "avg": avg,
            "count": session.timing_count,
        })
    except Exception:
        pass

    # 关键修复：TTS 发送完 ≠ 前端播放完。
    # 这里【不】立即进入 listening，而是保持 speaking 状态，
    # 等前端真正播放完、发来 playback_done 消息后，才进入 listening + 保护期。
    # 否则前端还在播，后端就 listening，用户插话走了正常端点检测，被保护期吞首字。
    # session.state 保持 "speaking"，由 client_playback_done 消息切换。


async def handle_control_message(ws: WebSocket, session: ConversationSession, text: str):
    """处理控制消息（JSON）"""
    try:
        msg = json.loads(text)
    except json.JSONDecodeError:
        return

    msg_type = msg.get("type")
    if msg_type == "stop":
        # 手动停止（按钮）
        if session.tts_task and not session.tts_task.done():
            session.tts_task.cancel()
        session.state = "listening"
    elif msg_type == "speech_start":
        # 前端 VAD 检测到人声（纯事件上报 + 预卷上传，后端做业务决策）
        await session.emit_event(ws, "前端VAD", "检测到人声（speech_start）")
        await handle_speech_start(ws, session, msg.get("preRollBase64"))
    elif msg_type == "speech_end":
        # 前端 VAD 检测到人声结束
        await session.emit_event(ws, "前端VAD", "人声结束（speech_end）")
        await handle_speech_end(ws, session)
    elif msg_type == "barge_latency":
        # 前端上报「用户开口 → 停止播报」的真实打断延迟
        latency = msg.get("latency")
        if latency is not None:
            session.barge_count += 1
            session.timing_sum["barge_in"] += latency
            avg = round(session.timing_sum["barge_in"] / session.barge_count, 3)
            await ws.send_json({"type": "barge_avg", "avg": avg, "count": session.barge_count})
            await session.emit_event(ws, "Barge-in", f"打断延迟 {round(latency*1000)}ms")
    elif msg_type == "client_play_start":
        # 前端上报「喇叭真正开始发声」→ 打开打断窗口，确保 state=speaking
        # 解决排队模式：TTS已下发但喇叭未响时，后端误以为listening
        if session.state == "listening" or session.state == "pending_play":
            session.state = "speaking"
            print("[播放开始] 前端喇叭发声，后端进入 speaking（打断窗口打开）")
    elif msg_type == "client_playback_done":
        # 前端真正播放完，发来此消息 → 后端才进入 listening + 保护期
        # 这是「播放结束」和「TTS发送完」分离的关键
        if session.state in ("speaking", "pending_play"):
            session.state = "listening"
            session.reset_speech_guard()  # 播放真正结束后，才开始保护尾音回声
            print("[播放完成] 前端播放完毕，后端进入 listening")
    elif msg_type == "client_barge_in":
        # 前端本地打断：取消 TTS 生成，进入 listening（音频继续流式接收，不丢字）
        print(f"[打断DEBUG] 收到 client_barge_in, state={session.state}, abort={session.abort_speaking}")
        # 打断响应延迟由前端测量（你开口 → 球球闭嘴），这里接收并统计
        barge_latency = msg.get("latency")  # 前端传来的真实打断响应延迟（秒）
        session.barge_count += 1
        if barge_latency is not None:
            session.timing_sum["barge_in"] += barge_latency
            avg_barge = round(session.timing_sum["barge_in"] / session.barge_count, 3)
            await ws.send_json({"type": "barge_avg", "avg": avg_barge, "count": session.barge_count})
            await session.emit_event(ws, "Barge-in", f"前端检测到插话，打断响应 {round(barge_latency*1000)}ms")
        else:
            await session.emit_event(ws, "Barge-in", "前端检测到插话")

        tts.cancel()
        if session.tts_task and not session.tts_task.done():
            session.tts_task.cancel()
        # 设置打断标志，让 handle_user_speech 的逐句流水线退出
        session.abort_speaking = True

        # ── 打断后启动流式 ASR 会话（和正常说话路径一致）──
        # 否则预卷和后续音频只被累积，没有会话在识别，finalize 返回空
        loop = asyncio.get_event_loop()
        partial_buffer = {"text": ""}
        def _on_partial(delta_text):
            partial_buffer["text"] += delta_text
            async def _send():
                await ws.send_json({"type": "asr_partial", "text": partial_buffer["text"]})
            asyncio.run_coroutine_threadsafe(_send(), loop)
        asr.start_streaming(session.session_id, _on_partial)

        # ── 关键：接收前端传来的「预卷音频」，喂给 ASR ──
        # 前端在环形缓存里切出了「你开口前256ms」的音频，补上 VAD 触发延迟，
        # 让 ASR 能识别出完整的话（不丢第一个字）
        pre_roll_b64 = msg.get("preRollBase64")
        if pre_roll_b64:
            try:
                import base64 as _b64
                pre_roll_pcm = _b64.b64decode(pre_roll_b64)
                asr.feed(session.session_id, pre_roll_pcm)  # feed 是同步方法，不要 await
                # 标记用户已在说话，让后续实时音频继续喂 ASR
                session.is_user_speaking = True
                print(f"[打断] 已喂入预卷音频 {len(pre_roll_pcm)} 字节")
            except Exception as e:
                print(f"[打断] 预卷音频解码失败: {e}")

        # 关键修复：把「球球说话期间缓存的音频」喂给 ASR
        # 这段音频是打断信号到达前、用户插话的那段窗口，之前被 continue 丢弃了
        if len(session.speaking_audio_cache) > 0:
            asr.feed(session.session_id, bytes(session.speaking_audio_cache))
            session.is_user_speaking = True
            print(f"[打断] 已喂入 speaking 缓存音频 {len(session.speaking_audio_cache)} 字节")
            session.speaking_audio_cache = bytearray()

        # 进入 listening，但不 reset_episode（保留已喂的 ASR 音频，让用户的话能被识别）
        session.state = "listening"
        session.barge_energy_baseline = None
        session.barge_consecutive_speech = 0
        session.speaking_start_time = None
        # 关键：打断后【不要】重置静默保护期！
        # 打断时用户已经在说话，实时音频必须立即喂 ASR。
        # 如果重置保护期，用户打断后 600ms 内的实时音频会被 continue 丢弃，
        # 导致「开口前256ms预卷 + 600ms后的实时音频」中间断档，吞掉首字。
        # 让 frames_since_speech 保持 >= POST_SPEECH_GUARD_FRAMES（即保护期已过），
        # 这样主循环立即接收实时音频。
        session.frames_since_speech = POST_SPEECH_GUARD_FRAMES

        # 关键修复2：重置端点检测的静音/人声帧计数
        # 否则打断前累积的 silence_frames 旧值会立即触发 finalize，
        # 导致用户的话还没说完就被截断识别（吞掉后半句）
        session.silence_frames = 0
        session.speech_frames = 0

        # 关键修复3：打断后标记对话历史，让 LLM 知道「上一轮被打断了」
        # 避免球球继续之前的话题（比如继续数数）
        session.history.append({
            "role": "user",
            "content": "(主人刚才打断了球球的上一轮回复，请不要再继续上一个话题，重新听主人接下来的话)",
        })


async def cleanup_session(session: ConversationSession):
    """连接断开时清理"""
    if session.tts_task and not session.tts_task.done():
        session.tts_task.cancel()
    asr.reset(session.session_id)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8001)
