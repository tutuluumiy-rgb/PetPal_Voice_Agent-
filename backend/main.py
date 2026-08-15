"""AI 宠物「球球」语音管道后端

管道：麦克风 → AEC(浏览器) → VAD → ASR → LLM → TTS → 扬声器
本文件负责 WebSocket 音频流接入和整体编排。

关键点：
- AEC 在浏览器端（WebRTC），不在后端，因为需要播放参考信号
- 后端 VAD 用于「判断用户是否说完了」（endpoint detection）
- barge-in：收到打断信号立即停止 TTS 流
"""

import asyncio
import json
import uuid

import numpy as np
import webrtcvad
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

load_dotenv()  # 读取 backend/.env

from asr_engine import StreamingASR
from llm_engine import ChatEngine
from tts_engine import TTSEngine
from personality import PERSONA_PROMPT

app = FastAPI(title="Ball Ball Pet Voice Pipeline")

# ══════════════════════════════════════════════════════════
# 🎛 端点检测（Endpointing）参数区 —— 集中调这里！
# 所有参数都基于「帧」计数，1 帧 = FRAME_MS 毫秒（默认 30ms）
# ══════════════════════════════════════════════════════════

# 【VAD 灵敏度】0=最保守（只认明显人声），3=最激进（有声音就算）
# 调大 → 更容易触发识别，但回声/噪音误报也更多
# 调小 → 更保守，但可能漏掉轻声说话
VAD_AGGRESSIVENESS = 2

# 【采样率】前端录音的采样率，改这里要和前端保持一致
SAMPLE_RATE = 16000

# 【VAD 帧长】每帧多少毫秒，webrtcvad 只支持 10/20/30ms
FRAME_MS = 30
FRAME_SIZE = int(SAMPLE_RATE * FRAME_MS / 1000 * 2)  # 960 bytes (16bit mono)

# 【最短人声时长】连续检测到多少帧人声，才确认「真的有人说话」
# 作用：过滤短促噪音、回声尾巴、咳嗽等（这些通常只有几十毫秒）
# 例子：设为 7 帧 ≈ 210ms，低于这个时长的声音会被忽略
# 调大 → 更严格，能过滤更多误报，但可能漏掉很短的词（如「嗯」「哦」）
# 调小 → 更敏感，短词也能识别，但回声误报变多
MIN_SPEECH_FRAMES = 7

# 【静音尾长】连续多少帧静音，才认为「用户说完了」
# 作用：判断一句话结束，触发 ASR 识别（终点检测）
# 例子：设为 20 帧 ≈ 600ms，用户停顿超过 600ms 就认为说完了
# 调大 → 用户停顿久一点才结束，适合说话慢的人，但响应变慢
# 调小 → 更快响应，但可能把一句话切成好几段
END_SILENCE_FRAMES = 20

# 【说话后静默保护期】球球说完话后（或开始说话后）多少帧内，跳过 VAD
# 作用：球球说话的尾音、混响会残留，这段期间先不处理，避免球球自己触发自己
# 注意：帧计数从 0 开始递增，所以这个值表示「保护多少帧」
# 例子：设为 15 帧 ≈ 450ms
# 调大 → 更安全，但用户立刻接话/插话会被延迟
# 调小 → 用户接话响应更快，但回声尾巴可能误触发
POST_SPEECH_GUARD_FRAMES = 10

# 【打断最小人声时长】球球正在说话时，连续多少人声帧才触发打断（barge-in）
# 打断阈值要比「普通检测」更严格（更大），因为球球说话时回声最强
# 作用：区分「用户真实插话」和「球球自己的回声」
# 例子：设为 12 帧 ≈ 360ms，短促回声不会触发，用户插话（通常更长）能触发
# 调大 → 打断更难触发，需要用户说更长，但能过滤更多回声误触发
# 调小 → 打断更灵敏，但球球回声可能频繁误触发
BARGIN_MIN_SPEECH_FRAMES = 5

# 【打断能量阈值倍率】能量尖峰检测：当前帧能量超过「球球说话时回声基线」的多少倍，立即触发打断
# 原理：球球说话时，麦克风主要录到的是球球自己的回声（有稳定能量基线）。
#       用户插话时，麦克风能量会突然超过这个基线（人声叠加在回声上）。
#       球球自己音量变化是渐进的，用户开口是突变的，所以阈值要够高才能区分。
# 例子：3.0 表示能量超过基线 3 倍才算尖峰（球球自己的音量波动一般到不了3倍）
# 调小 → 更灵敏，轻微能量上升就打断，但球球音量波动可能误触发
# 调大 → 更保守，需要明显的人声叠加才打断，但响应变慢
BARGIN_ENERGY_RATIO = 3.0

# 【能量尖峰连续帧数】能量连续多少帧超过阈值，才确认是用户插话
# 作用：过滤单帧的瞬时噪声/爆音（球球句尾的突然重音可能单帧超阈值）
# 例子：3 帧 ≈ 90ms，连续3帧能量尖峰才打断
BARGIN_ENERGY_SPIKE_FRAMES = 3

# 【能量基线更新率】回声基线（球球说话时的平均能量）的平滑更新系数
# 0~1 之间，越大表示基线更新越快（越能适应音量变化）
# 例子：0.05 表示新能量只占基线的 5%，缓慢适应
BARGIN_ENERGY_SMOOTHING = 0.05

# ── 全局组件 ──────────────────────────────────────────
vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)
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
    """处理音频：分帧 → VAD → 端点检测 → ASR

    状态机（两种场景）：
    A. 球球在说话（state=speaking）：检测「用户插话」→ 用更严格的打断阈值
    B. 球球不在说话：检测「用户正常说话」→ 用普通阈值
    """
    # 1. 累积音频，按 30ms(960字节) 分帧喂 VAD
    session.vad_buffer += pcm

    while len(session.vad_buffer) >= FRAME_SIZE:
        frame = session.vad_buffer[:FRAME_SIZE]
        session.vad_buffer = session.vad_buffer[FRAME_SIZE:]

        # 2. VAD 判断这一帧有没有人声
        try:
            is_speech = vad.is_speech(frame, SAMPLE_RATE)
        except Exception:
            is_speech = False

        # ── 静默保护期：球球刚说完/刚开始说话，先跳过（等混响尾音衰减）──
        if session.frames_since_speech < POST_SPEECH_GUARD_FRAMES:
            session.frames_since_speech += 1
            continue  # 保护期内，忽略这一帧

        # ── 场景 A：球球正在说话 / 思考 ──
        # 打断检测已完全移到前端（Silero VAD + 环形缓存预卷）。
        # thinking（LLM思考）和 speaking（球球说话）期间都跳过端点检测：
        # 这两个阶段主循环不该把音频识别成「用户说话」，否则球球回声会被误收。
        if session.state == "speaking" or session.state == "thinking":
            continue

        # ── 场景 B：球球不在说话，正常端点检测 ──
        if is_speech:
            session.speech_frames += 1
            session.silence_frames = 0
        else:
            session.silence_frames += 1

        # 最短人声时长确认
        if session.speech_frames >= MIN_SPEECH_FRAMES and not session.is_user_speaking:
            session.is_user_speaking = True

        # 终点检测：连续静音够 END_SILENCE_FRAMES 帧，认为说完了
        if session.is_user_speaking and session.silence_frames >= END_SILENCE_FRAMES:
            # 立即同步切换到 thinking 状态，关闭「listening→thinking」窗口，
            # 防止球球上一句的回声被主循环收进端点检测识别成「用户说话」
            session.state = "thinking"
            # 后台任务执行 ASR→LLM→TTS，不阻塞音频接收循环
            asyncio.create_task(finish_user_speech(ws, session))
            session.reset_episode()
            continue

    # 3. 音频喂给 ASR（只有确认用户在说话时才喂，避免回声进 ASR）
    if session.is_user_speaking and session.state != "speaking":
        await asr.feed(session.session_id, pcm)


async def finish_user_speech(ws: WebSocket, session: ConversationSession):
    """用户说完了：跑 ASR 识别，过滤语气词噪声，交给 LLM/TTS"""
    import time
    # 新一轮开始
    session.round_id += 1
    session.event_start_time = None
    await session.emit_event(ws, "VAD", "检测到用户说完（静音尾长）")

    t_asr_start = time.time()
    await session.emit_event(ws, "ASR", "开始识别", duration=0)
    text = await asr.finalize(session.session_id)
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


async def handle_user_speech(ws: WebSocket, session: ConversationSession, text: str):
    """用户说完一句话：进入思考 → LLM 流式逐句 → TTS 逐句合成播放"""
    import time

    session.state = "thinking"
    t0 = time.time()
    # 主对话框显示用户说的话（ASR 识别结果）
    await ws.send_json({"type": "user_text", "text": text})
    await session.emit_event(ws, "LLM", "开始生成回复", duration=0)

    # ── 句子级流水线：LLM 边生成边逐句发 TTS ──
    # 不再等 LLM 生成完才 TTS，而是 LLM 生成第一句就开始合成播放
    full_reply = ""
    emotion = "平静"
    t_llm_first_sentence = None  # LLM 出第一句的时间（衡量首响延迟）

    session.state = "speaking"
    session.reset_speech_guard()
    session.barge_energy_baseline = None
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
    elif msg_type == "client_playback_done":
        # 前端真正播放完，发来此消息 → 后端才进入 listening + 保护期
        # 这是「播放结束」和「TTS发送完」分离的关键
        if session.state == "speaking":
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

        # ── 关键：接收前端传来的「预卷音频」，喂给 ASR ──
        # 前端在环形缓存里切出了「你开口前256ms」的音频，补上 VAD 触发延迟，
        # 让 ASR 能识别出完整的话（不丢第一个字）
        pre_roll_b64 = msg.get("preRollBase64")
        if pre_roll_b64:
            try:
                import base64 as _b64
                pre_roll_pcm = _b64.b64decode(pre_roll_b64)
                await asr.feed(session.session_id, pre_roll_pcm)
                # 标记用户已在说话，让后续实时音频继续喂 ASR
                session.is_user_speaking = True
                print(f"[打断] 已喂入预卷音频 {len(pre_roll_pcm)} 字节")
            except Exception as e:
                print(f"[打断] 预卷音频解码失败: {e}")

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


async def cleanup_session(session: ConversationSession):
    """连接断开时清理"""
    if session.tts_task and not session.tts_task.done():
        session.tts_task.cancel()
    asr.reset(session.session_id)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8001)
