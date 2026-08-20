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

from providers import get_asr, get_llm, get_tts
from providers.llm import strip_emotion_tags
from vad_engine import SileroVAD
from emotion_state import EmotionState
from mode_state import ModeState, parse_mode_command, build_switch_context
from prompt_loader import build_system_prompt, load_user_profile, get_active_user_id
from session_store import SessionStore
from compaction import CompactionState, COMPACTION_SYSTEM_PROMPT
from agent_runtime import run_agent_loop
from agent_config import DEFAULT_MEMORY_CONFIG
from memory_store import MemoryStore
from memory_extractor import MemoryExtractor
from memory_fs import MemoryFs
from tools import memory as _mem_tools
from agent_state import (
    LISTENING as ST_LISTENING,
    THINKING as ST_THINKING,
    SPEAKING as ST_SPEAKING,
    IDLE as ST_IDLE,
    ERROR as ST_ERROR,
    AgentStateMachine,
    normalize_state,
    STATE_CHANGE_EVENT,
    StateTimeout,
)

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
# 实测（test_diag_weak.py）：开口瞬间的弱音段 Silero 概率仅 0.2~0.47，
# 0.45 阈值会把正常插话误判为噪声（「误报」 根因之一），降到 0.35 兼顾敏感度与回声鲁棒
BACKEND_VAD_THRESHOLD = 0.6

# 【二次确认人声帧占比】缓存音频里，人声帧占总帧数的比例阈值
# 真人声「大部分帧都是人声」，噪声/回声则零星几帧
# 前端AEC弱化后，人声占比会偏低（实测0.07~0.33波动），阈值要够低
# 调大 → 更严格；调小 → 更敏感
CONFIRM_SPEECH_RATIO = 0.05

# 【短缓存人声占比（更严）】cache 不足 2 个观察窗口时（能量基线不可用，无法做
# 能量跃升辅助判断），用此更严的人声占比阈值，防止「人声类误报」（清嗓子/说话声/
# 耳机漏音）在 cache 短时被 15ms 快速误断。真插话占比通常 0.5+，0.3 仍能通过。
CONFIRM_SPEECH_RATIO_SHORT = 0.3

# 【二次确认最小缓存时长】缓存音频至少多少毫秒才做二次确认
# 缓存太短（<200ms）无法可靠判断，直接拒绝（宁可不打断，不误打断）
CONFIRM_MIN_AUDIO_MS = 200

# 【二次确认取音频窗口】只取缓存里「最近」多少毫秒做二次确认
# 原因：球球说话期间，缓存里大部分是球球回声（AEC消过但能量低），
#       你的插话声只在最后。取整个缓存会导致人声占比被回声稀释（如3%）。
#       所以只取「最近」这一段（含你的插话声）。
# 注意：与前端预卷回退量(PRE_ROLL_MS=256)对齐，窗口太宽会混入回声稀释人声占比
# 实测：256ms 窗口（8帧）太短，恰好取到开口弱音段时占比≈0（误报）；
#       加大到 512ms（16帧）覆盖语音主体，显著降低弱音段误拒
# 调大 → 覆盖更多语音，弱音段不易误拒；调小 → 更聚焦你的插话，但可能截断
CONFIRM_WINDOW_MS = 512

# 【二次确认能量跃升阈值】最近窗口 vs 前一窗口（球球回声基线）的能量比值
# 用户插话 = 能量从回声基线显著跃升；平稳噪音/持续回声 = 前后能量相近
# Silero VAD 无法区分「球球回声」和「用户插话」（都是人声特征），
# 实测纯回声占比 0.5+ 会被误判 → 用能量跃升辅助判别：
#   比值低于阈值 且 前窗口确有声音 → 判定平稳噪音/回声，拒绝打断
# 1.3→2.0→3.0：球球说话中语音本身能量有起伏（字间波动 jump 常见 1.3~2，强音字可更高），
#           原阈值把球球自身波动误判成"插话"→ 后端判定过松（前端都 misfire 了后端还 confirm）；
#           提到 3.0 后，只有明显的能量突变（真插话）才确认，后端判定更准确
# 调大 → 更严格（只认明显能量突变）；调小 → 更宽松（弱插话也能打断）
CONFIRM_ENERGY_JUMP = 3.0

# 【Silero 模型路径】后端 VAD 模型（复用测试看板下载的）
SILERO_MODEL_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "testboard", "vad", "silero_vad.onnx",
)

# ── 状态机超时兜底（契约第 5 条）──────────────────
# 【listening 收音超时】收到 vad_speech_start 后，若长时间没有 vad_speech_end / vad_cancel
# （前端 VAD 未报结束 / 消息丢失），自动退出收音，避免 is_user_speaking 一直卡 True。
# 30s：给长话让路；且 _auto_exit_speech 会“仍在收音频则续期”（长话保护），实际更宽松。
SPEECH_TIMEOUT_S = 30

# 【speaking 播放超时】进入 speaking 后，若长时间收不到 client_playback_done
# （前端播放完成事件丢失 / 页面切后台），安全超时兜底复位回 listening，避免状态卡死。
SPEAKING_PLAYBACK_TIMEOUT_S = 20

# ── 全局组件 ──────────────────────────────────────────
backend_vad = SileroVAD(SILERO_MODEL_PATH)  # 后端 Silero VAD（业务层二次确认）
# 通过工厂获取云接口（可插拔：换 ASR/TTS/LLM 只改 .env 的 *_PROVIDER 配置）
asr = get_asr()
llm = get_llm()
tts = get_tts()
# 宠物情绪状态机（跨轮保持情绪，随时间向平静衰减，输出 TTS 数值参数）
emotion_state = EmotionState()
# 双模式全局状态机（闲聊/工作；默认闲聊；语音指令 + 手动均可切换）
mode_state = ModeState()

# ── 记忆模块（分层：L1 事件 / L2 事实 / L3 自传；会话结束归档 + 主动记忆工具）──
# 按当前启用用户分目录存记忆（backend/memories/<ACTIVE_USER>/）
memory_store = MemoryStore.for_user(get_active_user_id())
_mem_config = DEFAULT_MEMORY_CONFIG
# v2 扁平文件系统记忆层（MEMORY.md / memory/YYYY-MM-DD.md / tool_result/ / dialog/）
# 每用户独立 working_dir，避免多用户串味
memory_fs = MemoryFs(working_dir=os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "memories", get_active_user_id()
))


async def _memory_summarizer(messages):
    """记忆抽取/聚合/判重的 LLM 回调（独立小调用，不污染主回复链路）。

    messages: [{role:"system",...},{role:"user",...}]，指令已由 extractor 拼好。
    """
    resp = await llm.client.chat.completions.create(
        model=llm.model, messages=messages, temperature=0.3, max_tokens=1200, stream=False,
    )
    return resp.choices[0].message.content or ""


memory_extractor = MemoryExtractor(memory_store, summarizer=_memory_summarizer, config=_mem_config)
_mem_tools.bind_memory(memory_store, memory_extractor, memory_fs=memory_fs)


# 【语气词黑名单】ASR 识别结果如果只包含这些词（可重复、可带标点），直接丢弃
# 原因：ASR 会把残余噪声、尾音「幻听」成短语气词，这些词信息量极低
# 自定义：加你想过滤的词，用 | 分隔，注意转义
FILLER_WORDS = "嗯|啊|哦|额|呃|噢|哎|唉|嗯嗯|啊啊|哦哦|额额|呵呵|嘿嘿"

import re as _re

# 原生工具调用开始时的轻量进度播报（空=不播报；快工具/提问类不播，直接出结果）
_TOOL_PROGRESS = {
    "get_weather": "好的，我来查一下天气~",
    "web_search": "好的，我来搜一下~",
    "read": "好，我来看一下文件~",
    "bash": "好的，我来执行一下~",
    "write": "好的，我来写一下~",
    "edit": "好的，我来改一下~",
    "calculator": "",          # 快，不播报
    "ask_user_questions": "",  # 提问类，不播报
}


def _is_filler_word(text: str) -> bool:
    """判断文本是否只是语气词（如「嗯」「啊」「嗯嗯」等）"""
    # 去掉标点、空格后，检查是否只由黑名单里的词组成
    cleaned = _re.sub(r"[，。！？、,.!?~～\s]", "", text)
    if not cleaned:
        return True
    # 允许语气词重复，如「嗯嗯」「啊啊啊」
    pattern = _re.compile(f"^({FILLER_WORDS})+$")
    return bool(pattern.match(cleaned))


async def _sync_backend_state(ws, session, reason="", force=False):
    """契约第 2 条：把 session.state 归一到规范五态并发布 backend_state_change 通知。

    - 仅作为**通知**，不做强制命令，不假设前端一定收到。
    - 通过 _last_notified_state 去重：状态未变化则不重复广播。
    - 设计为与现有 session.state 赋值并联：不阻碍既有控制流转（向后兼容测试看板）。
    """
    import time as _t
    norm = normalize_state(session.state)
    if not force and session._last_notified_state == norm:
        return norm
    session._last_notified_state = norm
    # 同步到规范化状态机对象的 state（作为规范围/调试记录）
    session.state_machine.state = norm
    try:
        await ws.send_json({
            "type": STATE_CHANGE_EVENT,
            "state": norm,
            "reason": reason or "",
            "ts": round(_t.time(), 3),
        })
        print(f"[状态机] backend_state_change → {norm} ({reason or 'no-reason'})")
    except Exception:
        pass
    return norm


async def _auto_exit_speech(ws, session, reason="speech_timeout"):
    """契约第 5 条·listening 收音超时兜底：
    vad_speech_start 后 long 时间无 vad_speech_end → 自动退出收音（复位 ASR 与说话状态）。

    防误伤长话：若前端仍在持续上传音频（用户还在说话），则续期重 arm 一次（有上限），
    而非强行重置 ASR —— 否则一句 >SPEECH_TIMEOUT_S 的长话会被中途切断、后半句丢失。
    """
    import time as _t
    if not session.is_user_speaking and session.state != "listening":
        return
    if not session.is_user_speaking:
        return

    # 仍在收音频（<1.5s 内还有音频到达）→ 用户还在讲 → 续期，不误伤长话
    if session.last_audio_recv_ts and (_t.time() - session.last_audio_recv_ts < 1.5):
        if session.speech_timeout_grace < 2:  # 最多续 2 次（总约 3×SPEECH_TIMEOUT_S）
            session.speech_timeout_grace += 1
            print(f"[状态机] 仍在收音频，收音超时续期 # {session.speech_timeout_grace}（长话保护）")
            session.speech_timeout.arm(lambda: _auto_exit_speech(ws, session))
            return

    print(f"[状态机] 收音超时（{SPEECH_TIMEOUT_S}s 无 speech_end），自动退出收音")
    try:
        asr.reset(session.session_id)
    except Exception:
        pass
    session.is_user_speaking = False
    session.speech_start_ts = None
    session.speech_timeout_grace = 0
    session.speech_timeout.disarm()
    if session.state not in ("speaking", "pending_play"):
        session.state = "listening"
    await _sync_backend_state(ws, session, reason)


async def _auto_reset_speaking(ws, session, reason="playback_timeout"):
    """契约第 5 条·speaking 播放超时兜底：
    speaking 后 long 时间无 client_playback_done → 安全复位回 listening（防状态卡死）。
    """
    if session.state not in ("speaking", "pending_play"):
        return
    print(f"[状态机] speaking 播放超时（{SPEAKING_PLAYBACK_TIMEOUT_S}s 无 client_playback_done），安全复位")
    session.state = "listening"
    session.speaking_playback_timeout.disarm()
    if session.tts_task and not session.tts_task.done():
        try:
            session.tts_task.cancel()
        except Exception:
            pass
    await _sync_backend_state(ws, session, reason)
class ConversationSession:
    """单个对话会话的状态机"""

    def __init__(self):
        self.session_id = str(uuid.uuid4())[:8]
        self.state = "listening"  # listening / thinking / speaking（内部流转；pending_play 为 speaking 瞬态）
        # ── 规范化状态机（契约）：对外统一五态 + 状态通知 + 超时兜底 ──
        self.state_machine = AgentStateMachine(initial="listening")
        self._last_notified_state = None     # 已发布过的规范态（用于 backend_state_change 去重）
        self.speech_timeout = StateTimeout(SPEECH_TIMEOUT_S, "speech")
        self.speaking_playback_timeout = StateTimeout(SPEAKING_PLAYBACK_TIMEOUT_S, "playback")
        self.history = []  # 对话历史（短期记忆，兼容旧路径，新架构以 store 为准）
        self.store = SessionStore()  # 会话层：全量 JSONL 持久化 + run/sub_turn/tool_call_id 可追溯
        self.agent_compaction = CompactionState()  # 会话级压缩检查点（跨 run 保持）
        self.silence_frames = 0      # 连续静音帧数（用于终点检测）
        self.speech_frames = 0       # 连续人声帧数（用于最短时长确认）
        self.tts_task = None  # 正在播放的 TTS 任务
        self.user_speech_task = None  # 整条「LLM+TTS 流水线」任务（架构修复：独立任务，不阻塞主循环）
        self.pending_user_text = ""  # 累积的用户输入
        self.vad_buffer = b""  # VAD 分帧缓冲（累积到 30ms 再喂 VAD）
        self.last_asr_time = 0  # 最近一次 ASR 耗时
        self.speech_start_ts = None  # 最近一次 speech_start 到达时刻（用于会话过期兜底）
        self.frames_since_speech = 0  # 球球说话后的静默保护计数（从0递增）
        self.is_user_speaking = False  # 当前是否已确认用户正在说话
        self.last_audio_recv_ts = 0.0  # 最近一次收到前端音频的时间戳（用于收音超时“仍在说话则续期”）
        self.speech_timeout_grace = 0  # 收音超时续期次数（避免无限续）
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


# ── Phase ⑤ 预留接口：user_profile / personality 查看与修改（前端 web 可视化用）──
def _profile_json_path(user_id: str | None = None) -> str:
    from prompt_loader import USERS_DIR
    user_id = user_id or get_active_user_id()
    return os.path.join(USERS_DIR, user_id, "profile.json")


@app.get("/memory/profile")
async def memory_get_profile(user_id: str | None = None):
    """读取当前用户的 user_profile（profile.json + MEMORY.md 长期主干）。"""
    uid = user_id or get_active_user_id()
    p = _profile_json_path(uid)
    profile = {}
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                profile = json.load(f)
        except (OSError, json.JSONDecodeError):
            profile = {}
    return {
        "user_id": uid,
        "profile": profile,
        "rendered": load_user_profile(uid),
        "memory_md": memory_fs.read_memory_md(),
        "memory_md_tokens": memory_fs.memory_md_tokens(),
    }


@app.put("/memory/profile")
async def memory_put_profile(payload: dict, user_id: str | None = None):
    """更新 user_profile（profile.json 字段覆写；可选 text 追加到 MEMORY.md）。"""
    uid = user_id or get_active_user_id()
    p = _profile_json_path(uid)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    profile = {}
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                profile = json.load(f)
        except (OSError, json.JSONDecodeError):
            profile = {}
    # 允许按字段覆写
    for k in ("basic", "reply_style", "likes", "dislikes", "daily"):
        if k in payload:
            profile[k] = payload[k]
    with open(p, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)
    # 可选：把一句话沉淀进 MEMORY.md（长期事务/偏好主干）
    if payload.get("text"):
        memory_fs.append_memory_md(str(payload["text"])[:200])
    return {"ok": True, "user_id": uid, "profile": profile}


@app.get("/memory/personality")
async def memory_get_personality():
    """读取 personality.md（宠物人格，前端可视化编辑）。"""
    from prompt_loader import load_prompt as _lp
    return {"personality.md": _lp("personality.md")}


@app.put("/memory/personality")
async def memory_put_personality(payload: dict):
    """覆写 personality.md。"""
    text = payload.get("content", "")
    if not isinstance(text, str):
        return {"ok": False, "error": "content 需要为字符串"}
    from prompt_loader import PROMPTS_DIR
    with open(os.path.join(PROMPTS_DIR, "personality.md"), "w", encoding="utf-8") as f:
        f.write(text.strip() + "\n")
    return {"ok": True}


@app.websocket("/ws/audio")
async def audio_ws(ws: WebSocket):
    await ws.accept()
    session = ConversationSession()
    await ws.send_json({"type": "ready", "session_id": session.session_id})
    # 契约：连接建立发布初始状态通知（listening）
    await _sync_backend_state(ws, session, "connected")

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
    import time as _t
    session.last_audio_recv_ts = _t.time()  # 记录音频到达时刻（收音超时“仍在说话则续期”依据）

    if session.state in ("speaking", "thinking", "pending_play"):
        # 球球说话/思考/待播期间：缓存音频（打断信号到达后回放给 ASR）
        session.speaking_audio_cache.extend(pcm)
        if len(session.speaking_audio_cache) > session.MAX_SPEAKING_CACHE:
            session.speaking_audio_cache = session.speaking_audio_cache[-session.MAX_SPEAKING_CACHE:]
        return

    # listening 状态：如果用户正在说话（前端 speech_start 已触发），喂给 ASR
    if session.is_user_speaking:
        # 球球说完后的静默保护期（POST_SPEECH_GUARD_FRAMES 帧内跳过喂 ASR，防尾音回声误识别）
        # 修复：保护期之前只赋值从未检查，实际未生效。
        # 注意：用户确认开口（speech_start 到达）时 handle_speech_start 会立即结束保护，
        #       所以这里不会吞掉用户首字（开口前的首字由前端预卷 256ms 补上）。
        if session.frames_since_speech < POST_SPEECH_GUARD_FRAMES:
            session.frames_since_speech += 1
            return
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

    # 能量跃升检测（防噪音/回声被误判为打断）：
    # Silero VAD 无法区分「球球回声」和「用户插话」（都是人声特征，实测纯回声占比 0.5+ 误判）。
    # 用户插话 = 能量从球球回声基线显著跃升；平稳噪音/持续回声 = 前后窗口能量相近。
    # 若前一窗口（回声基线）确有声音、且最近窗口能量没有显著跃升 → 判定平稳噪音/回声，拒绝。
    prev_bytes = int(SAMPLE_RATE * CONFIRM_WINDOW_MS / 1000 * 2)
    if len(audio_cache) >= 2 * prev_bytes:
        try:
            import numpy as _np2
            prev_audio = bytes(audio_cache[-2 * prev_bytes:-prev_bytes])
            prev_np = _np2.frombuffer(prev_audio, dtype=_np2.int16).astype(_np2.float32)
            prev_rms = float(_np2.sqrt(_np2.mean(prev_np ** 2))) if len(prev_np) > 0 else 0.0
            recent_np2 = _np2.frombuffer(recent_audio, dtype=_np2.int16).astype(_np2.float32)
            recent_rms2 = float(_np2.sqrt(_np2.mean(recent_np2 ** 2))) if len(recent_np2) > 0 else 0.0
            if prev_rms > 30:  # 前一窗口确有声音（球球回声基线存在）
                jump = recent_rms2 / prev_rms if prev_rms > 0 else 0.0
                if jump < CONFIRM_ENERGY_JUMP:
                    print(f"[二次确认] 能量无跃升（jump={jump:.2f} < {CONFIRM_ENERGY_JUMP}，前窗RMS={prev_rms:.0f}→近窗RMS={recent_rms2:.0f}），判定平稳噪音/回声，拒绝打断")
                    return False
                print(f"[二次确认] 能量跃升（jump={jump:.2f} ≥ {CONFIRM_ENERGY_JUMP}），判定用户插话")
        except Exception as e:
            print(f"[二次确认] 能量跃升检测异常: {e}")

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
        # cache 不足 2 窗口（能量基线不可用，无法做能量跃升辅助）→ 用更严的人声占比，
        # 防止「人声类误报」在短缓存时被快速误断（cache 足够长时能量判断已拦下平稳噪音）
        ratio_thr = CONFIRM_SPEECH_RATIO if len(audio_cache) >= 2 * prev_bytes else CONFIRM_SPEECH_RATIO_SHORT
        is_speech, ratio = backend_vad.is_speech(
            recent_audio,
            BACKEND_VAD_THRESHOLD,
            ratio_threshold=ratio_thr,
        )
        print(f"[二次确认] 取最近{CONFIRM_WINDOW_MS}ms, 人声帧占比={ratio:.2f}, 阈值={ratio_thr}, 结果={'确认人声' if is_speech else '判定噪声'}")
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
    # 注：真流式 ASR 下，音频已实时入队发送（同连接 FIFO），speech_end 到达前所有音频都
    #     已进入 ASR 会话，finalize 只是 commit 收尾——无需再 sleep 等待尾音块（移除旧的非流式兜底）。
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
        # 修复：若这次是「打断后无有效输入」（用户其实没真说话 / 误断），
        # 通知前端恢复之前被打断的播报（前端 barge_confirm 时只是静音等待，未销毁播放器）。
        # 正常说话被过滤时前端无 pendingResume，此消息无害。
        try:
            await ws.send_json({"type": "resume_playback"})
        except Exception:
            pass
        return

    await session.emit_event(ws, "ASR", f"识别结果：{text}", duration=round(t_asr, 2))
    session.last_asr_time = round(t_asr, 2)
    # 通知前端 ASR 完成，收尾流式展示（把 asr_partial 的状态转正为最终结果）
    await ws.send_json({"type": "asr_final", "text": text})

    # ── 语音指令：模式切换（打开工作模式/打开闲聊模式/切换模式，子串部分命中）──
    # 命中 → 切模式 + 发【文字】系统通知（不播报 TTS），然后把用户整句输入 + 切换状态
    # 上下文一起送进 LLM 生成第一轮回复（继续处理用户本句的实际任务）。
    matched, target = parse_mode_command(text)
    switch_ctx = None
    if matched:
        new_mode = mode_state.toggle() if target is None else mode_state.switch(target)
        print(f"[模式] 语音指令切换 → {mode_state.name()}")
        await session.emit_event(ws, "模式", f"系统通知：已经切换到{mode_state.name()}")
        await ws.send_json({
            "type": "mode_changed",
            "mode": new_mode,
            "notice": f"已经切换到{mode_state.name()}，继续处理你的请求",
        })
        switch_ctx = build_switch_context(new_mode)
        # 不 return：继续走 handle_user_speech，把 extra_context 一并送入 LLM

    # ── 架构修复：handle_user_speech 作为独立任务运行，不阻塞主循环 ──
    # 之前同步 await 导致 LLM+TTS 流水线（10~30s）期间 WebSocket 消息无人处理，
    # 打断消息（speech_start/音频帧）只能排队等整轮回复完成 → 打断延迟秒级。
    # 任务化后主循环立即回到 receive，speech_start 实时处理 → 打断延迟降至百毫秒级。
    if session.user_speech_task and not session.user_speech_task.done():
        # 上一轮流水线还在跑（用户连续说话）→ 先取消再启动新一轮，防两个流水线并发
        print(f"[流水线] 上一轮流水线仍在运行，先取消再启动新一轮")
        session.user_speech_task.cancel()
        try:
            await session.user_speech_task
        except (asyncio.CancelledError, Exception):
            pass
    session.user_speech_task = asyncio.create_task(
        handle_user_speech(ws, session, text, extra_context=switch_ctx)
    )
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

    # 防重入：listening 且已在识别中（前端 VAD 抖动/双触发）→ 忽略本次 speech_start
    # 修复：之前重复触发会再次 start_streaming，覆盖 ASR 会话，丢掉已累积的音频
    if session.state == "listening" and session.is_user_speaking:
        # 过期兜底：若上次 speech_start 已很久（>8s）且没有 speech_end / vad_cancel，
        # 说明是前端误报（misfire 后未撤销），先重置说话状态再重新开始，
        # 否则 is_user_speaking 永远卡 True，用户真实说话会被下面 else 忽略 → LLM 被阻塞
        if session.speech_start_ts is not None and _t.time() - session.speech_start_ts > 8:
            print(f"[状态机] 上次 speech_start 已过期（{_t.time()-session.speech_start_ts:.0f}s 无结束），重置说话状态")
            asr.reset(session.session_id)
            session.is_user_speaking = False
            session.speech_start_ts = None
        else:
            print(f"[状态机] speech_start 重复触发（is_user_speaking=True），忽略")
            return

    if session.state == "pending_play":
        # 待播态：TTS已下发但喇叭未响，用户说话了 → 直接丢弃待播任务，切 listening
        # （快速打断，不走 ASR——与 speaking 分支一致的 16ms 级响应）
        print(f"[状态机] state=pending_play → 丢弃待播任务，切 listening")
        # 取消当前 TTS 任务（还没播，直接取消）+ 整条流水线任务
        if session.tts_task and not session.tts_task.done():
            session.tts_task.cancel()
        session.abort_speaking = True
        if session.user_speech_task and not session.user_speech_task.done():
            session.user_speech_task.cancel()
        # 启动 ASR，接收用户话
        loop = asyncio.get_event_loop()
        partial_buffer = {"text": ""}
        def _on_partial(delta_text):
            partial_buffer["text"] = delta_text  # 流式 partial 是全量修订(stash)，覆盖而非追加
            async def _send():
                await ws.send_json({"type": "asr_partial", "text": partial_buffer["text"]})
            asyncio.run_coroutine_threadsafe(_send(), loop)
        asr.start_streaming(session.session_id, _on_partial)
        session.speech_start_ts = _t.time()  # 记录本次 speech_start 时刻（会话过期兜底）
        # 把预卷 + 待播期间缓存的音频喂给 ASR（防窗口吞字）
        if pre_roll_b64:
            try:
                import base64 as _b64
                pre_roll_pcm = _b64.b64decode(pre_roll_b64)
                asr.feed(session.session_id, pre_roll_pcm)
                print(f"[状态机] pending_play：已喂入前端预卷")
            except Exception as e:
                print(f"[状态机] pending_play：预卷解码失败: {e}")
        if len(session.speaking_audio_cache) > 0:
            asr.feed(session.session_id, bytes(session.speaking_audio_cache))
            session.speaking_audio_cache = bytearray()
            print(f"[状态机] pending_play 打断：已喂入待播缓存音频")
        session.is_user_speaking = True
        session.state = "listening"
        # 契约：pending_play 被用户打断 → 复位 listening + 通知；启动收音超时兜底
        session.speaking_playback_timeout.disarm()
        await _sync_backend_state(ws, session, "interrupted_while_pending_play")
        session.speech_timeout.arm(lambda: _auto_exit_speech(ws, session))
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
        # 二次确认输入 = 前端预卷 + 后端最近音频 合并
        # 修复：预卷（开口前256ms）必须拼在【前面】。_confirm_real_speech 只取
        #       「最近 CONFIRM_WINDOW_MS=256ms」，如果预卷拼末尾，最近256ms 全是
        #       开口前的静音/回声 → RMS≈0 → 误判噪声 → 正常插话被误报拒绝（实测复现）
        pre_roll_pcm = None
        if pre_roll_b64:
            try:
                import base64 as _b64
                pre_roll_pcm = _b64.b64decode(pre_roll_b64)
                print(f"[状态机] 解码前端预卷 {len(pre_roll_pcm)} 字节")
            except Exception as e:
                print(f"[状态机] 预卷解码失败: {e}")

        confirm_audio = bytearray(pre_roll_pcm or b"") + session.speaking_audio_cache

        if not _confirm_real_speech(confirm_audio):
            # 二次确认失败：可能是球球回声或噪声，拒绝打断
            print(f"[状态机] 二次确认失败（噪声/回声），拒绝打断，恢复音量")
            await session.emit_event(ws, "后端VAD", "二次确认判定噪声 → 拒绝打断")
            await ws.send_json({"type": "barge_reject"})
            return

        # 二次确认通过：确认真打断（快速，~16ms，不走 ASR——ASR 确认太慢影响打断体验）
        _t_confirm = _t.time()
        _backend_ms = (_t_confirm - _t_recv) * 1000
        print(f"[状态机] 二次确认通过 → 确认真打断, 后端处理耗时={_backend_ms:.0f}ms")
        await session.emit_event(ws, "后端VAD", "二次确认判定人声 → 确认真打断")
        print(f"[打断DEBUG] 打断前 tts_task 状态: done={session.tts_task.done() if session.tts_task else 'None'}")
        # 1. 取消 TTS 和 LLM 流水线（先停当前句 TTS，再取消整条流水线任务）
        tts.cancel()
        if session.tts_task and not session.tts_task.done():
            session.tts_task.cancel()
        session.abort_speaking = True
        if session.user_speech_task and not session.user_speech_task.done():
            session.user_speech_task.cancel()
        print(f"[打断DEBUG] 已设置 abort_speaking=True、cancel tts_task 和 user_speech_task")

        # 2. 通知前端「确认真打断」，前端 ducking 等待（流水线已取消，缓冲播完自然结束）
        print(f"[打断DEBUG] barge_confirm 已发送, 后端处理耗时={_backend_ms:.0f}ms")
        await ws.send_json({
            "type": "barge_confirm",
            "backend_ms": round(_backend_ms, 1),  # 后端二次确认耗时
        })

        # 3. 启动 ASR 会话，准备接收用户插话内容
        loop = asyncio.get_event_loop()
        partial_buffer = {"text": ""}
        def _on_partial(delta_text):
            partial_buffer["text"] = delta_text  # 流式 partial 是全量修订(stash)，覆盖而非追加
            async def _send():
                await ws.send_json({"type": "asr_partial", "text": partial_buffer["text"]})
            asyncio.run_coroutine_threadsafe(_send(), loop)
        asr.start_streaming(session.session_id, _on_partial)
        session.speech_start_ts = _t.time()  # 记录本次 speech_start 时刻（会话过期兜底）

        # 4. 把用户插话相关音频喂给 ASR（防窗口吞字）：
        #    - 前端预卷（开口前256ms）：补 VAD 触发延迟丢的首字
        #    - 缓存尾部最近 1s：用户开口后的部分
        #    修复：之前喂整段缓存（最长2s，大部分是球球回声），
        #          ASR 先识别出球球说的话再识别用户话 → 识别错乱/丢字（实测「从无属」）
        if pre_roll_pcm:
            asr.feed(session.session_id, pre_roll_pcm)
            print(f"[状态机] 已喂入前端预卷 {len(pre_roll_pcm)} 字节")
        if len(session.speaking_audio_cache) > 0:
            cache_tail_ms = 1000  # 只取最近 1 秒（用户开口后的音频）
            tail_bytes = int(SAMPLE_RATE * cache_tail_ms / 1000 * 2)
            cache_tail = session.speaking_audio_cache[-tail_bytes:]
            asr.feed(session.session_id, bytes(cache_tail))
            session.speaking_audio_cache = bytearray()
            print(f"[状态机] 已喂入缓存尾部 {len(cache_tail)} 字节")

        # 5. 进入 listening，重置端点检测，让用户的话能被识别
        session.state = "listening"
        # 契约：确认真打断 → 复位 listening + 通知；取消播放超时，启动收音超时
        session.speaking_playback_timeout.disarm()
        await _sync_backend_state(ws, session, "barge_confirmed")
        session.speech_timeout.arm(lambda: _auto_exit_speech(ws, session))
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
            partial_buffer["text"] = delta_text  # 流式 partial 是全量修订(stash)，覆盖而非追加
            async def _send():
                await ws.send_json({"type": "asr_partial", "text": partial_buffer["text"]})
            asyncio.run_coroutine_threadsafe(_send(), loop)
        asr.start_streaming(session.session_id, _on_partial)
        session.speech_start_ts = _t.time()  # 记录本次 speech_start 时刻（会话过期兜底）
        session.is_user_speaking = True
        session.silence_frames = 0
        session.speech_frames = 0
        # 用户已被前端 VAD 确认真开口 → 立即结束静默保护期，
        # 否则球球说完后用户马上开口，保护期内的实时音频会被跳过吞掉首字
        session.frames_since_speech = POST_SPEECH_GUARD_FRAMES
        # 契约：listening 收音超时兜底（speech_start 后若无 speech_end，自动退出收音）
        session.speech_timeout.arm(lambda: _auto_exit_speech(ws, session))


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
        session.speech_timeout.disarm()  # 收音结束，取消超时兜底
        await finish_user_speech(ws, session)


def _build_timing_stats(session, t0, t_llm_first_sentence, t_llm, t_tts):
    """计算本轮耗时统计（current + avg），正常收尾与打断补发共用

    - 正常收尾：LLM 完整生成 + TTS 完整发送
    - 打断补发：t_tts 传 0（TTS 未完整发送），数据是部分值，但仍计入平均
    """
    tts_first = getattr(tts, "first_audio_time", None)  # TTS首包（第一句合成首包）
    # llm.first_token_time 在新架构下可能为 None（属性存在但未设置），必须兜底
    llm_first_token = getattr(llm, "first_token_time", None) or 0
    # 端到端首响 = ASR + LLM首字 + LLM生成第一句 + TTS首包
    # t_llm_first_sentence 是「LLM开始到第一句生成完」的时间，包含首字+生成第一句
    e2e = round(
        (getattr(session, "last_asr_time", 0) or 0)
        + (t_llm_first_sentence if t_llm_first_sentence else llm_first_token)
        + (tts_first or 0),
        2,
    )
    current = {
        "asr": getattr(session, "last_asr_time", 0) or 0,
        "llm_first_token": llm_first_token,
        "llm_first_sentence": round(t_llm_first_sentence or 0, 2),  # 新增：LLM首句
        "tts_first_packet": tts_first or 0,
        "e2e": e2e,
        "total": round((getattr(session, "last_asr_time", 0) or 0) + t_llm + t_tts, 2),
    }
    session.timing_count += 1
    avg = {}
    for k, v in current.items():
        session.timing_sum[k] += v
        avg[k] = round(session.timing_sum[k] / session.timing_count, 2)
    return current, avg


async def _safe_send_timing(ws, current, avg, count):
    """独立任务发送 timing（供打断补发使用——任务取消状态下不能直接 await 发送）"""
    try:
        await ws.send_json({"type": "timing", "current": current, "avg": avg, "count": count})
    except Exception:
        pass


async def handle_user_speech(ws: WebSocket, session: ConversationSession, text: str,
                             extra_context: str | None = None):
    """用户说完一句话：进入思考 → LLM 流式逐句 → TTS 逐句合成播放

    extra_context: 可选系统上下文（模式切换状态），一并送入 LLM 生成第一轮回复。
    """
    import time

    session.state = "thinking"
    t0 = time.time()
    # 契约：进入 thinking（LLM 生成）→ 发布状态通知
    await _sync_backend_state(ws, session, "llm_generating")
    # 用户说的话已由 asr_final 消息展示（ASR 流式收尾），这里不再发 user_text
    await session.emit_event(ws, "LLM", "开始生成回复", duration=0)

    # ── 句子级流水线：LLM 边生成边逐句发 TTS ──
    # 不再等 LLM 生成完才 TTS，而是 LLM 生成第一句就开始合成播放
    full_reply = ""
    emotion = "平静"
    t_llm_first_sentence = None  # LLM 出第一句的时间（衡量首响延迟）

    session.state = "pending_play"  # 待播态：等前端 play_start 才转 speaking
    # 竞态修复（任务化后）：client_play_start 可能在任务设置 pending_play 之前到达
    #（state 还是 listening，主循环已把它切到 speaking）→ 不要覆盖回 pending_play
    if session.state != "speaking":
        session.state = "pending_play"
    # 契约：进入播报阶段（pending_play 归一为 speaking）→ 发布状态通知 + 播放超时兜底
    await _sync_backend_state(ws, session, "tts_play_started")
    # 进入 speaking 时取消收音超时（若残留），并 arm 播放完成超时
    session.speech_timeout.disarm()
    session.speaking_playback_timeout.arm(lambda: _auto_reset_speaking(ws, session))
    session.reset_speech_guard()
    session.barge_energy_baseline = None
    # 新一轮回复开始，清空「说话期间音频缓存」（上一轮打断/说话残留的）
    session.speaking_audio_cache = bytearray()
    session.barge_consecutive_speech = 0
    session.speaking_start_time = time.time()
    session.abort_speaking = False  # 新一轮说话，清空打断标志

    # 契约：正式开始下发回复前，通知前端切断占位音频（前端控制播放权，后端只发停止通知）
    try:
        await ws.send_json({"type": "stop_placeholder"})
    except Exception:
        pass

    # 发「回复开始」信号，前端据此标记 ballIsPlaying=true
    await ws.send_json({"type": "reply_start"})

    t_tts_start = time.time()

    # ── 架构修复：整条流水线包 try/except/finally ──
    # 任务化后，打断时主循环会 cancel 本任务。这里捕获 CancelledError 做清理
    # （停掉当前句 TTS，防后台线程继续发音频），并重新抛出传播取消状态。
    # ── 会话层：本 run 生成 run_id + 记录用户输入（可追溯）──
    import uuid as _uuid
    run_id = _uuid.uuid4().hex[:8]
    mode = mode_state.get_mode()
    system_prompt = build_system_prompt(mode)
    if extra_context:
        system_prompt += "\n\n" + extra_context
    session.store.add("user", text, run_id=run_id, sub_turn=1)

    progress_task = None  # 工具开始前的进度播报任务
    progress_announced = False  # 整个 run 内是否已播过一次进度占位（多工具/多 sub_turn 只播一次）

    async def _on_tool(stage, name, call_id, args):
        """原生工具调用开始（agent_runtime 以 4 参调用：stage, name, call_id, args）。
        stage: "start"（工具开始执行）。事件 + 长工具轻量 TTS 进度播报。

        去抖：**整个 run 内进度占位只播一次**。并行多工具（如"同时搜两个"）或
        多 sub_turn 依次调工具，都只播第一句占位，避免连续播两遍。
        """
        nonlocal progress_task, progress_announced
        await session.emit_event(ws, "工具", f"执行中：{name}")
        progress_text = _TOOL_PROGRESS.get(name)
        if progress_text and not progress_announced:
            progress_announced = True
            progress_task = asyncio.create_task(
                tts.speak_and_send(ws, progress_text, session.session_id, {"emotion": "平静"})
            )

    async def _summarize(prompt_text: str) -> str:
        """独立无工具模型调用，生成/更新压缩检查点（超长对话才触发）。

        摘要长度按 COMPACT_SUMMARY_RATIO(0.1) 约束：约为被压缩历史 token 的 10%，
        设上下限 [200, 2000] 防止过短/过长。
        """
        from agent_config import COMPACT_SUMMARY_RATIO
        # 粗略估算被压缩历史 token（prompt 文本长度 / 4），乘 0.1 作为摘要预算
        hist_tokens = max(1, len(prompt_text) // 4)
        budget = max(200, min(2000, int(hist_tokens * COMPACT_SUMMARY_RATIO)))
        resp = await llm.client.chat.completions.create(
            model=llm.model,
            messages=[
                {"role": "system", "content": COMPACTION_SYSTEM_PROMPT},
                {"role": "user", "content": prompt_text},
            ],
            max_tokens=budget,
            stream=False,
        )
        return resp.choices[0].message.content or ""

    try:
        # 新架构：原生 function calling 多 sub_turn agent 环（会话层/上下文层/压缩/工具并发都在环内）
        async for ev in run_agent_loop(
            llm.client, llm.model, mode, system_prompt, session.store,
            run_id=run_id,
            user_profile=load_user_profile(get_active_user_id()),
            compaction_state=session.agent_compaction,
            summarizer=_summarize,
            on_tool=_on_tool,
            memory_fs=memory_fs,
        ):
            # 被打断：停止后续句子的生成和播放
            if session.abort_speaking:
                print(f"[打断DEBUG] 循环检测到 abort_speaking，退出流水线")
                break

            kind = ev[0]
            if kind == "tool":
                continue  # 工具进度已由 on_tool 处理
            if kind != "reply":
                continue

            sentence, emo = ev[1], ev[2]

            if full_reply == "":
                # 第一条正式回复：立即停止进度播报，直接转最终回复（不等进度播完）
                if progress_task is not None and not progress_task.done():
                    progress_task.cancel()
                    try:
                        await progress_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    progress_task = None
                    # 通知前端立即停掉进度音频播放（否则前端还会播完已缓冲的进度音频）
                    try:
                        await ws.send_json({"type": "stop_playback"})
                    except Exception:
                        pass
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
            # 情绪状态机：本轮情绪标签更新状态 → 输出 TTS 数值参数（语速/音量/音调 + 语气指令）
            emotion_state.update(emo)
            tts_params = emotion_state.get_tts_params()
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
    except asyncio.CancelledError:
        # 任务被外部取消（打断）：清理当前句 TTS，防后台线程继续发音频
        print(f"[流水线] handle_user_speech 被取消（打断），补发部分统计")
        # 修复：任务处于取消状态不能再 await，用 create_task 补发「打断轮次」的部分 timing。
        # 否则被打断的轮次不统计 → 看板数据滞后/缺失（用户连续打断时只统计到少量轮次）
        t_llm = time.time() - t0
        current, avg = _build_timing_stats(session, t0, t_llm_first_sentence, t_llm, 0)
        current["interrupted"] = True  # 标记：该轮被打断（部分数据）
        asyncio.create_task(_safe_send_timing(ws, current, avg, session.timing_count))
        if session.tts_task and not session.tts_task.done():
            session.tts_task.cancel()
        raise
    except Exception as e:
        # LLM 流式异常（网络/限流）→ 静默退出，避免任务抛未捕获异常卡死状态机
        print(f"[流水线] LLM 流式异常: {e}")
        return
    finally:
        # 任何退出路径都确保当前句 TTS 任务被清理
        if session.tts_task and not session.tts_task.done():
            session.tts_task.cancel()

    t_llm = time.time() - t0
    t_tts = time.time() - t_tts_start

    # 记录对话历史（旧路径兼容）+ 会话层持久化（新架构：完整可追溯）
    session.history.append({"role": "user", "content": text})
    session.history.append({"role": "assistant", "content": full_reply})
    session.store.add("assistant", full_reply, run_id=run_id)

    await session.emit_event(
        ws, "LLM",
        f"首字 {getattr(llm, 'first_token_time', 0)}s，首句 {round(t_llm_first_sentence or 0,2)}s，完成 {round(t_llm,2)}s，情绪[{emotion}]",
        duration=round(t_llm, 2),
    )

    # ── 统计本轮耗时 + 发 reply_end / timing（健壮化：任何统计或发送失败，
    #    reply_end 也必须发出，否则前端 ballIsPlaying 状态卡死）──
    try:
        current, avg = _build_timing_stats(session, t0, t_llm_first_sentence, t_llm, t_tts)
        # 修复：打断时任务取消被内层 except CancelledError 消费，break 后走正常收尾路径。
        # 这里通过 abort_speaking 标记「该轮被打断」，让看板能区分打断轮（部分数据）与完整轮。
        if session.abort_speaking:
            current["interrupted"] = True
            print(f"[流水线] 该轮被打断，timing 标记 interrupted=True")
        await ws.send_json({"type": "reply_end"})
        await ws.send_json({
            "type": "timing",
            "current": current,
            "avg": avg,
            "count": session.timing_count,
        })
    except Exception as _e:
        # 收尾异常不应影响前端状态：诊断打印 + 尽力补发 reply_end
        print(f"[流水线] 收尾统计/发送异常: {type(_e).__name__}: {_e}", file=__import__("sys").stderr)
        try:
            await ws.send_json({"type": "reply_end"})
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
        # 手动停止（按钮）：终止推理/工具/TTS，复位 listening（保持连接，向后兼容测试看板）
        if session.tts_task and not session.tts_task.done():
            session.tts_task.cancel()
        if session.user_speech_task and not session.user_speech_task.done():
            session.user_speech_task.cancel()
        session.state = "listening"
        session.speech_timeout.disarm()
        session.speaking_playback_timeout.disarm()
        await _sync_backend_state(ws, session, "user_stop")
    elif msg_type == "user_abort":
        # 契约第 4 条：立即终止 LLM 推理、终止工具调用、取消 TTS、清空 buffer、复位 idle
        print(f"[状态机] 收到 user_abort，全链路终止 → idle")
        if session.tts_task and not session.tts_task.done():
            session.tts_task.cancel()
        if session.user_speech_task and not session.user_speech_task.done():
            session.user_speech_task.cancel()
        try:
            tts.cancel()
        except Exception:
            pass
        try:
            asr.reset(session.session_id)
        except Exception:
            pass
        # 清空缓冲
        session.speaking_audio_cache = bytearray()
        session.vad_buffer = b""
        session.is_user_speaking = False
        session.abort_speaking = True
        session.speech_timeout.disarm()
        session.speaking_playback_timeout.disarm()
        session.state = "idle"
        await _sync_backend_state(ws, session, "user_abort")
        await session.emit_event(ws, "系统", "已中止当前对话")
    elif msg_type == "set_mode":
        # 手动切换模式：{"type":"set_mode","mode":"chat"|"work"} 或 "toggle"
        requested = msg.get("mode")
        if requested == "toggle":
            new_mode = mode_state.toggle()
        elif requested in ("chat", "work"):
            new_mode = mode_state.switch(requested)
        else:
            new_mode = mode_state.get_mode()
        await ws.send_json({"type": "mode_changed", "mode": new_mode})
        print(f"[模式] 手动切换 → {mode_state.name()}")
    elif msg_type == "get_mode":
        # 查询当前模式
        await ws.send_json({"type": "mode_changed", "mode": mode_state.get_mode()})
    elif msg_type == "speech_start":
        # 前端 VAD 检测到人声（纯事件上报 + 预卷上传，后端做业务决策）
        await session.emit_event(ws, "前端VAD", "检测到人声（speech_start）")
        await handle_speech_start(ws, session, msg.get("preRollBase64"))
    elif msg_type == "vad_cancel":
        # 前端判定上次 speech_start 是误报（onVADMisfire）→ 撤销 ASR 会话 + 重置说话状态。
        # 修复：之前 misfire 只恢复音量，后端已启动的 ASR 会话和 is_user_speaking 会一直卡着，
        #      导致后续用户真实说话被防重入忽略 / 音频喂进噪声会话 → 识别错乱、LLM 被阻塞
        print(f"[状态机] 收到 vad_cancel（前端判定误报），撤销 ASR 会话")
        await session.emit_event(ws, "前端VAD", "误报撤销（vad_cancel）")
        asr.reset(session.session_id)
        session.is_user_speaking = False
        session.speech_start_ts = None
        session.speech_timeout.disarm()  # 误报撤销 → 取消收音超时
        # 通知前端清理流式识别残留行（"你：xxx▌" 没有 asr_final 转正会卡住）
        try:
            await ws.send_json({"type": "asr_cancel"})
        except Exception:
            pass
    elif msg_type == "speech_end":
        # 前端 VAD 检测到人声结束
        import time as _t_end
        session.last_speech_end_recv_ts = _t_end.time()  # 记录后端收到 speech_end 时刻（算网络延迟）
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
            # 契约：speaking + 通知 + 播放完成超时兜底（防 client_playback_done 丢失）
            await _sync_backend_state(ws, session, "playback_started")
            session.speech_timeout.disarm()
            session.speaking_playback_timeout.arm(lambda: _auto_reset_speaking(ws, session))
    elif msg_type == "client_playback_done":
        # 前端真正播放完，发来此消息 → 后端才进入 listening + 保护期
        # 这是「播放结束」和「TTS发送完」分离的关键
        if session.state in ("speaking", "pending_play"):
            session.state = "listening"
            session.reset_speech_guard()  # 播放真正结束后，才开始保护尾音回声
            # 契约：speaking → listening + 通知 + 取消播放超时
            session.speaking_playback_timeout.disarm()
            await _sync_backend_state(ws, session, "playback_done")
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
        # 设置打断标志 + 取消整条流水线任务，让 handle_user_speech 立即退出
        session.abort_speaking = True
        if session.user_speech_task and not session.user_speech_task.done():
            session.user_speech_task.cancel()

        # ── 打断后启动流式 ASR 会话（和正常说话路径一致）──
        # 否则预卷和后续音频只被累积，没有会话在识别，finalize 返回空
        loop = asyncio.get_event_loop()
        partial_buffer = {"text": ""}
        def _on_partial(delta_text):
            partial_buffer["text"] = delta_text  # 流式 partial 是全量修订(stash)，覆盖而非追加
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
        # 契约：打断 → 复位 listening + 通知；取消播放超时，启动收音超时兜底
        session.speaking_playback_timeout.disarm()
        await _sync_backend_state(ws, session, "client_barge_in")
        if session.is_user_speaking:
            session.speech_timeout.arm(lambda: _auto_exit_speech(ws, session))
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
    """连接断开时清理：取消任务、复位状态机超时、清理 ASR。"""
    if session.tts_task and not session.tts_task.done():
        session.tts_task.cancel()
    if session.user_speech_task and not session.user_speech_task.done():
        session.user_speech_task.cancel()
    try:
        session.speech_timeout.disarm()
        session.speaking_playback_timeout.disarm()
    except Exception:
        pass
    asr.reset(session.session_id)

    # 记忆会话结束归档（异步，不阻塞断开；仅在记忆开启时）
    if _mem_config.enabled:
        try:
            asyncio.create_task(_archive_session_memory(session))
        except Exception as e:
            print(f"[memory] 归档调度失败: {e}")


async def _archive_session_memory(session: ConversationSession):
    """把本次会话的整体 transcript 归档进记忆：抽取 L1 →（按节流）沉淀 L2 → 聚合 L3。"""
    try:
        pairs = []
        for m in session.store.transcript():
            role = m.get("role", "?")
            content = m.get("content")
            if isinstance(content, str) and content.strip():
                pairs.append((role, content))
        if not pairs:
            return
        source = {"session_id": getattr(session, "session_id", None)}
        await memory_extractor.on_session_end(pairs, source=source)
    except Exception as e:
        print(f"[memory] 会话归档失败（不阻塞）: {e}")


# ── 每日主动记忆持久化（design 2.5：压缩后 + 每日主动一次，异步、不阻塞）──
_daily_persist_date: str = ""


async def _daily_persist_once():
    """后台任务：每天至少执行一次低层记忆持久化（写入当日日志/对话基线）。"""
    global _daily_persist_date
    try:
        import datetime as _d
        today = _d.date.today().isoformat()
        if _daily_persist_date == today:
            return
        _daily_persist_date = today
        # 每日基线：给当日 dialog 写一条占位/归档条目，确保当日文件存在供回溯
        try:
            memory_fs.upsert_dialog({
                "id": f"daily-{today}",
                "kind": "daily_baseline",
                "date": today,
                "created_at": _d.datetime.now().isoformat(timespec="seconds"),
            })
        except Exception as e:
            print(f"[memory] 每日基线落盘失败(不阻塞): {e}")
    except Exception as e:
        print(f"[memory] 每日持久化失败(不阻塞): {e}")


async def _daily_persist_loop():
    while True:
        try:
            await _daily_persist_once()
        except Exception:
            pass
        await asyncio.sleep(3600)  # 每小时检查一次（已写则当天不再写）


@app.on_event("startup")
async def _start_daily_persist():
    asyncio.create_task(_daily_persist_loop())


if __name__ == "__main__":
    import sys
    import uvicorn

    # 支持指定端口：python main.py [port]，默认 8001（前端硬编码）
    # 多实例测试时可用 8002/8003…，避免和正在运行的后端抢端口
    port = int(os.getenv("PORT", sys.argv[1] if len(sys.argv) > 1 else "8001"))
    uvicorn.run(app, host="0.0.0.0", port=port)
