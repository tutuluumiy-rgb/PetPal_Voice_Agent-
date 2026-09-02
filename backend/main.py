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
from fastapi.responses import JSONResponse

load_dotenv()  # 读取 backend/.env

from providers import get_asr, get_llm, get_llm_for_mode, get_tts
from providers.llm import strip_emotion_tags
from vad_engine import SileroVAD
from smart_turn import SmartTurnJudge
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

# ── 安全加固：Origin 白名单 + 不变量（F1 审计修复）──────────────
# 本地应用默认只服务本机：绑 127.0.0.1（HOST env 可显式开局域网）；
# 对 HTTP/WebSocket 请求做 Origin 校验，阻断跨站 WS（CSWSH/DNS-rebinding）与
# 恶意网页跨源调用；非浏览器客户端（主进程/测试，无 Origin）放行。
import re as _re_mod

_ALLOW_ORIGIN_RE = _re_mod.compile(r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$")
_VALID_ID_RE = _re_mod.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _origin_allowed(origin: str | None) -> bool:
    if not origin:
        return True  # 非浏览器客户端（主进程 fetch/ws、测试）
    o = origin.strip()
    if o == "null" or o == "file://":
        return True  # file:// 页面（Electron 渲染进程）
    return bool(_ALLOW_ORIGIN_RE.match(o))


def _safe_uid(uid: str | None) -> bool:
    """user_id / sessionId 白名单校验（防路径穿越，F2 审计修复）"""
    return bool(uid) and bool(_VALID_ID_RE.match(uid))


async def _run_in_session_mode(session, coro):
    """F4 隔离：在【本会话模式】下运行协程（handle_user_speech 等），结束后恢复全局模式。

    下游（providers/llm、run_tool_loop、prompt_loader）按全局 ModeState 读模式；
    本函数在工作前临时把全局切到会话模式、finally 还原，实现"每会话独立模式"
    而无需改动 provider 接口。会话 mode 为 None 时不切换（跟随全局）。
    """
    prior = mode_state.get_mode()
    session_mode = getattr(session, "mode", None)
    if session_mode and session_mode != prior:
        mode_state.switch(session_mode)
    try:
        return await coro
    finally:
        if mode_state.get_mode() != prior:
            mode_state.switch(prior)


class _OriginGuard:
    """ASGI 中间件：HTTP/WS 都校验 Origin；拒绝时 HTTP=403 / WS=1008。"""

    def __init__(self, app, **kwargs):
        # Starlette 要求中间件构造首参命名 app（build_middleware_stack 用
        # cls(app=app, ...) 传参）；其余关键字参数（如 add_middleware 透传）
        # 一律忽略，避免签名不匹配。
        self.inner = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            origin = None
            for k, v in (scope.get("headers") or []):
                if k == b"origin":
                    origin = v.decode("latin1")
                    break
            if origin is not None and not _origin_allowed(origin):
                if scope["type"] == "http":
                    body = b'{"detail":"origin not allowed"}'
                    await send({
                        "type": "http.response.start",
                        "status": 403,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                        ],
                    })
                    await send({"type": "http.response.body", "body": body})
                    return
                await send({"type": "websocket.close", "code": 1008})
                return
        await self.inner(scope, receive, send)


app.add_middleware(_OriginGuard)

# 管理端点 /ws/mgmt：控制面板对接真实后端（契约 MOCK_CONTRACT.md §6）
from mgmt_api import register_mgmt
register_mgmt(app)

# ══════════════════════════════════════════════════════════
# 🎛 后端双层打断参数区 —— 集中调这里！
# 架构：前端 Silero VAD（体感层，敏感）→ 后端 Silero VAD（业务层，准确）
# ══════════════════════════════════════════════════════════

# 【采样率】前端录音采样率，前后端保持一致
SAMPLE_RATE = 16000

# 【说话后静默保护期】西西说完话后多少帧内跳过处理（等混响尾音衰减）
# 单位：帧，1帧 = 30ms（后端音频分帧）
# 调大 → 更安全防回声；调小 → 用户接话响应快
POST_SPEECH_GUARD_FRAMES = 10

# ── 后端 Silero VAD 二次确认参数 ──────────────────────
# 【人声概率阈值】后端判定一帧为「人声」的 Silero 概率阈值
# 前端AEC会消掉大部分插话声，后端缓存里的人声被弱化，所以阈值不能太严
# 调大 → 更严格（只认真人声）；调小 → 更敏感
# 实测（test_diag_weak.py）：开口瞬间的弱音段 Silero 概率仅 0.2~0.47，
# 0.45 阈值会把正常插话误判为噪声（「误报」 根因之一），降到 0.35 兼顾敏感度与回声鲁棒
# ⚠️ 修复打断丢字：当前 0.6 阈值比实测推荐(0.35)高太多，真人声开口弱段被误判噪声
#    → barge_reject → 用户话丢失。降到 0.4（偏松，靠能量跃升+人声占比兜底误报）。
# 2025-02 实测再放宽到 0.3：「我刚刚在想…」轻声慢热开头，0.4 下人声占比=0.00 被拒
#   （前端同款 0.4 却能 onSpeechStart——前端还有 192ms 连续帧约束兜底，后端只算占比）。
# ── 与前端对齐（用户确认）：前端起提阈值已改 0.6/0.4 滞回 → 后端人声帧判定同口径 0.6，
#    占比 = "前端认定标准下的人声帧"占比（更严但口径一致；近窗静音场景由
#    CONFIRM_TRUST_FRONTEND_SILENT_RMS 分支接管，不依赖此阈值）。
BACKEND_VAD_THRESHOLD = 0.6

# 【二次确认人声帧占比】缓存音频里，人声帧占总帧数的比例阈值
# 真人声「大部分帧都是人声」，噪声/回声则零星几帧
# 前端AEC弱化后，人声占比会偏低（实测0.07~0.33波动），阈值要够低
# 调大 → 更严格；调小 → 更敏感（实测轻声插话占比可低至 0.0x，0.03 兜住）
CONFIRM_SPEECH_RATIO = 0.03

# 【短缓存人声占比（更严）】cache 不足 2 个观察窗口时（能量基线不可用，无法做
# 能量跃升辅助判断），用此更严的人声占比阈值，防止「人声类误报」（清嗓子/说话声/
# 耳机漏音）在 cache 短时被 15ms 快速误断。真插话占比通常 0.5+，0.3 仍能通过。
CONFIRM_SPEECH_RATIO_SHORT = 0.3

# 【二次确认最小缓存时长】缓存音频至少多少毫秒才做二次确认
# 缓存太短（<200ms）无法可靠判断，直接拒绝（宁可不打断，不误打断）
CONFIRM_MIN_AUDIO_MS = 200

# 【二次确认取音频窗口】只取缓存里「最近」多少毫秒做二次确认
# 原因：西西说话期间，缓存里大部分是西西回声（AEC消过但能量低），
#       你的插话声只在最后。取整个缓存会导致人声占比被回声稀释（如3%）。
#       所以只取「最近」这一段（含你的插话声）。
# 注意：与前端预卷回退量(PRE_ROLL_MS=256)对齐，窗口太宽会混入回声稀释人声占比
# 实测：256ms 窗口（8帧）太短，恰好取到开口弱音段时占比≈0（误报）；
#       加大到 512ms（16帧）覆盖语音主体，显著降低弱音段误拒；
#       768ms（24帧）：插话控制消息可能早于音频帧到达，宽窗给"开口爬坡+lags"余量。
# ── 回落 320ms（用户确认）：近窗应聚焦"开口段"，宽窗会把回声/环境噪声帧框进来
#    稀释人声占比（近窗≈preRoll 256ms + 64ms 余量）。消息滞后由 CONFIRM_RETRY_MS 兜底。
# 调大 → 覆盖更多语音，弱音段不易误拒；调小 → 更聚焦你的插话，但可能截断
CONFIRM_WINDOW_MS = 320

# 【二次确认能量跃升阈值】最近窗口 vs 前一窗口（西西回声基线）的能量比值
# 用户插话 = 能量从回声基线显著跃升；平稳噪音/持续回声 = 前后能量相近
# Silero VAD 无法区分「西西回声」和「用户插话」（都是人声特征），
# 实测纯回声占比 0.5+ 会被误判 → 用能量跃升辅助判别：
#   比值低于阈值 且 前窗口确有声音 → 判定平稳噪音/回声，拒绝打断
# 1.3→2.0→3.0：西西说话中语音本身能量有起伏（字间波动 jump 常见 1.3~2，强音字可更高），
#           原阈值把西西自身波动误判成"插话"→ 后端判定过松（前端都 misfire 了后端还 confirm）；
#           提到 3.0 后，只有明显的能量突变（真插话）才确认，后端判定更准确
# 调大 → 更严格（只认明显能量突变）；调小 → 更宽松（弱插话也能打断）
CONFIRM_ENERGY_JUMP = 3.0

# 【二次确认"静音窗"重试】首验失败后，延迟此毫秒用【新缓存】再验一次。
# 起因（实测）：speech_start 控制消息可能先于对应音频帧到后端；复核时刻近窗
#   几乎静音（RMS≈0、人声占比 0.00）→ 真插话被误拒 → 球球继续播、用户话走窗口
#   （样例：近窗 RMS=22/峰值=258/占比=0.00，但 ASR 后来识别出整句）。
# 重试只做一次且仅当有延时（120ms 内音频已送达）；0 = 关闭（旧行为）。
CONFIRM_RETRY_MS = 120

# 【基线静音时的近窗能量下限】基线 RMS≤30（mic 几乎听不到球球→环境分离度好）时，
# 人声占比闸会系统性误拒轻声插话（实测：近窗 RMS=137/峰值=1891 但 Silero 占比=0.00，
# ASR 却识别出整句）。此分支改为：近窗 RMS ≥ 该值（确实有能量）→ 直接判定用户插话；
# 低于该值仍走占比闸兜底（防微弱噪声误断）。0 = 关闭此分支（退回纯占比判定）。
CONFIRM_RECENT_MIN_RMS = 50

# 【基线静音→信任前端】首验+重验都失败时，若【缓存头部（球球回声区）RMS】低于此值
# （麦克风几乎采不到球球回声 → 无"回声误触"风险），物理复核材料不可信/判不了 →
# 信任前端 VAD（同一 Silero 0.6 起提 × 连续 6 帧 192ms 才发 speech_start，门槛已高）；
# 误断有挂起+语义裁决兜底（成本低），拒真打断体验成本高。
# 注意：判断条件用【头部基线】而非近窗——近窗 RMS 稍高（几十~百，弱开口）但基线静音时
# 同样应信任前端（实测：基线 RMS=0、近窗 RMS=44/峰值374、占比 0.00 仍拒断的案例）。
# 0 = 关闭该分支（严格物理复核）。
CONFIRM_TRUST_FRONTEND_BASELINE_RMS = 30

# 【能量闸启用/占比分档门槛（基于 speaking_audio_cache 自身长度，不含 preRoll）】
# 改造（改造清单#2）：物理复核素材 = 说话期缓存（preRoll 退出物理判据，只做补字）。
#   - len(cache) ≥ 此值：能量闸可用（基线=缓存头部512ms=球球开播段，保证纯回声；
#     近窗=缓存尾部512ms=插话段），占比用松阈值 CONFIRM_SPEECH_RATIO。
#   - len(cache) < 此值：短缓存，能量闸跳过（无基线），占比用严阈值 CONFIRM_SPEECH_RATIO_SHORT。
# 值的选择：要保证"头部512ms ≦ 用户开口前"，要求打断发生在球球开播 ≥(门槛-前端判定窗口) 之后；
#   前端判定窗口 = 6帧×32ms = 192ms（前端已显式 frameSamples=512，与后端 FRAME_SIZE 对齐）。
#   实际下限 = 512 + 192 = 704ms；1200 留大余量（隐含假设：用户插话至少发生在球球开播 ~1008ms
#   之后，头部 512ms 必然是纯球球回声）。可实测打断时刻分布后微调。
CONFIRM_ENERGY_CACHE_MS = 1200

# 【打断后补充说明窗口（改造清单#6）】
# 打断确认后不立即提交用户轮：进入 BARGE_IN_PENDING；语音暂断(SOFT_ENDED)后开此窗口，
# 窗口内用户继续说话(有效语音≥192ms，前端 VAD 档) → 视为同一 turn 的补充（turn_id 复用、
# revision+1、音频并入同一 ASR 会话、旧 LLM/TTS 结果失效）；窗口结束无新语音 → 才正式提交
# 该 turn（finalize → 语义判定 → LLM）。流式 partial 语义判定保持即时，不受窗口阻塞。
SUPPLEMENT_WINDOW_MS = 1200

# 【SmartTurn 端点检测（改造清单#7）——所有链路的"用户是否说完"判定，非 barge-in】
# 端点检测职责：一句话结束后判定"话轮完结与否" → 决定直接提交 or 开补充窗口等续说。
# 模型：pipecat-ai/smart-turn-v3 系 ONNX（放入 SMART_TURN_MODEL_PATH 即自动启用）。
# 无模型/无音频 → judge 返回 None → fallback（direct=直接提交，等价旧行为；window=统一开窗）。
SMART_TURN_MODEL_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "models", "smart_turn_v3.onnx",
)
# 阈值默认 0.5（实测标定，scripts/smart_turn_calib.py，31 样本：说完15/未完16）：
# θ=0.5 → 81% 一致（未完误判完说 5/16、完说误判未完 1/15）；θ=0.3 → 74%（未完误判 7/16，更差）。
# 职责边界（用户确认）：SmartTurn 只判"用户话是否完整"；"是否说完话"由前端 VAD 静音时长
# 判定（VoicePipeline.ts redemptionFrames，见参数清单），时长闸方案已移除。
SMART_TURN_THRESHOLD = 0.5   # p > 阈值 → 已说完；否则可能未完 → 补充窗口
SMART_TURN_FALLBACK = "direct"  # 无法判定时的策略：direct | window
# 尾静音重判（模型驱动的提前提交）：首判 p≤阈值开窗后，收集真实尾静音
# SMART_TURN_REJUDGE_MS 毫秒，用 [整段话+真实尾静音] 再判一次；p>阈值 → 提前提交
# （把"死等整个补充窗口"变为模型驱动，多数场景 ~600ms 内提交而非 1200ms）。
# 0 = 关闭（等同旧的定时窗口提交）。实测：突兀收尾的完整句首判 0.26 → 补 300ms
# 真实静音后 0.88——模型需要"静音证据"。
SMART_TURN_REJUDGE_MS = 600
# 调试：置 1 时把每次端点判定的音频段（含重判扩展段）dump 成 wav 到 smart_turn_audio_log/
# （供真实语音标定阈值/诊断误判；pipecat 同款能力）
SMART_TURN_LOG_AUDIO = os.getenv("SMART_TURN_LOG_AUDIO", "") == "1"

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

# 【speaking 播放超时】作为「说话卡住」的兜底，不是整轮回复的时间预算：
#   正常回复逐句完成后会滑动重 arm（见 handle_user_speech），整轮发送完毕即复位 listening
#   （前端会在播放真正完成后发 client_playback_done，见收尾逻辑）；超时只在真正卡死（某句 TTS/生成停滞）
#   时才兜底复位，避免长回复 / 长工具被误掐断。
# 45s ≈ LLM 单次生成超时（LLM_TIMEOUT_S），给慢句留余量，同时仍是「最后防线」。
SPEAKING_PLAYBACK_TIMEOUT_S = 45

# ── 全局组件 ──────────────────────────────────────────
backend_vad = SileroVAD(SILERO_MODEL_PATH)  # 后端 Silero VAD（业务层二次确认）
# 端点检测（话轮完结判定，改造清单#7）：smart-turn-v3；无模型自动降级不可用
smart_turn = SmartTurnJudge(SMART_TURN_MODEL_PATH)
smart_turn.threshold = SMART_TURN_THRESHOLD
# 通过工厂获取云接口（可插拔：换 ASR/TTS/LLM 只改 .env 的 *_PROVIDER 配置）
asr = get_asr()
llm = get_llm()
worker_llm = get_llm_for_mode("work")  # 后台任务 Worker 固定按工作模式选模型（默认 DeepSeek）
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
    if not getattr(resp, "choices", None):
        return ""
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


async def _preheat_tts(ws, session):
    """唤醒后异步预热 TTS 长连接（仅 MiniMax WS 模式有效）。
    - 把「建连 + task_start」从首句合成里挪到唤醒时间窗内跑完
    - 异常不传播（fire-and-forget，预热失败首句会正常建连）
    """
    import time as _time  # 局部 import，main.py 顶层没引 time
    try:
        if not hasattr(tts, "preheat"):
            return
        t0 = _time.time()
        await tts.preheat()
        cost = round(_time.time() - t0, 2)
        print(f"[TTS预热] WS 长连接就绪（{cost}s）")
        await session.emit_event(ws, "TTS", f"WS 预热就绪（{cost}s，首句免建连）")
    except Exception as e:
        print(f"[TTS预热] 失败（首句会正常建连）: {type(e).__name__}: {e}")
        try:
            await session.emit_event(ws, "TTS", f"WS 预热失败（首句会正常建连）: {type(e).__name__}")
        except Exception:
            pass


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
        self.frames_since_speech = 0  # 西西说话后的静默保护计数（从0递增）
        self.is_user_speaking = False  # 当前是否已确认用户正在说话
        self.mode = None  # 会话级模式（F4 隔离：None=跟随全局默认；语音 set_mode 只改本会话）
        self.temperature = None  # 温度覆盖（评测中心 Step4 注入；None = 后端默认 0.7）
        self.last_audio_recv_ts = 0.0  # 最近一次收到前端音频的时间戳（用于收音超时“仍在说话则续期”）
        self.speech_timeout_grace = 0  # 收音超时续期次数（避免无限续）
        self.is_barge_in_speaking = False  # 打断场景：是否已确认用户在插话
        self.barge_energy_baseline = None  # 西西说话时的回声能量基线（用于能量尖峰检测）
        self.barge_consecutive_speech = 0  # 打断场景：连续人声帧数（能量触发后的二次确认）
        self.speaking_start_time = None  # 西西本次开始说话的时间戳（用于算打断延迟）
        self.abort_speaking = False  # 打断标志：置 True 时，LLM/TTS 流水线循环退出
        self.speaking_audio_cache = bytearray()  # 西西说话期间缓存的音频（打断时喂ASR，防窗口吞字）
        self.MAX_SPEAKING_CACHE = SAMPLE_RATE * 2 * 2  # 最多缓存2秒（16000Hz * 2字节 * 2秒）
        # 当前轮次的事件流（一轮对话 = 用户说完到西西回复完）
        self.round_id = 0  # 轮次编号
        self.event_start_time = None  # 本轮第一个事件的时间戳（用于相对计时）
        # 累计统计（用于计算平均值）
        # timing_count = 总轮次（含打断）—— 用于前端展示「轮次计数」
        # avg_count    = 计入 avg 的完整轮次（排除打断轮的数据污染；打断轮部分指标无值会拉低平均）
        # timing_sum   = 完整轮次的累加和
        self.timing_count = 0
        self.avg_count = 0
        self.timing_sum = {"asr": 0.0, "llm_first_token": 0.0, "llm_first_sentence": 0.0, "tts_first_packet": 0.0, "e2e": 0.0, "barge_in": 0.0}
        self.barge_count = 0  # 打断次数（单独计数，因为不是每轮都打断）
        # 真实端到端延迟（前端测量：VAD onSpeechEnd → 第一帧音频出声），由前端 client_real_e2e 上报
        self.last_real_e2e_ms = None  # 最近一轮的真实 E2E（毫秒）
        # ── 打断挂起上下文（改造清单#4）：打断后不立即丢弃内容，等语义裁决再决定恢复/丢弃 ──
        # pending_reply_text：本轮已生成/已下发的回复文本（逐句追加，打断时可快照）
        self.pending_reply_text = ""
        # suspended_reply：被打断轮挂起的内容 {"text": str, "emotion": str}；
        # ASR 语义裁决有效 → 真正丢弃；无效（语气词/噪声）→ 恢复（重播音）
        self.suspended_reply = None
        # is_effective_interrupt：流式 partial 语义判定标记（改造：不等 speech_end/finalize）。
        # ASR 流式 partial 一旦出现"非语气词内容" → True（提前判有效指令，立即丢弃挂起）。
        self.is_effective_interrupt = False
        # ── 补充说明窗口（改造清单#6）：打断后的用户 turn 暂不立即提交 ──
        # supplement_state: None(未启用) | "pending"(打断后等补充/继续收话) | "soft_ended"(暂断,窗口计时中)
        self.supplement_state = None
        self.turn_revision = 0        # 同一用户 turn 内的补充次数（合并音频/revision+1）
        self.turn_generation = 0      # 防泄漏：每次"提交"或"再次打断"递增，旧输出校验不匹配则放弃
        self._supplement_task = None  # 补充窗口定时任务
        # ── 尾静音重判（改造清单#7 实测优化）：首判未完开窗后，收集真实尾静音再判一次 ──
        self.smart_turn_segment = None   # 本次端点判定的整段话 PCM（bytes|None）
        self.smart_turn_tail = None      # soft_ended 窗口期收集的真实尾静音（bytearray|None=未收集）
        self._rejudge_task = None        # 尾静音重判定时任务

    def reset_episode(self):
        """新一轮对话开始，清空 VAD 状态"""
        self.silence_frames = 0
        self.speech_frames = 0
        self.pending_user_text = ""
        self.is_user_speaking = False
        self.is_barge_in_speaking = False

    def reset_speech_guard(self):
        """西西开始说话/被打断后，重置静默保护计数（从0重新开始保护）"""
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
    if user_id is not None and not _safe_uid(user_id):
        return JSONResponse({"detail": "invalid user_id"}, status_code=400)  # F2 审计修复：防路径穿越
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
    if user_id is not None and not _safe_uid(user_id):
        return JSONResponse({"detail": "invalid user_id"}, status_code=400)  # F2 审计修复：防路径穿越
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
    session.mode = mode_state.get_mode()  # F4：会话级模式快照（后续语音切换只改本会话）
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

    # 音频帧大小上限（F1 审计修复：防恶意大帧打满缓存/队列；真实帧 2~4KB）
    if len(pcm) > 65536:
        return

    # 尾静音收集（改造清单#7）：补充窗口（soft_ended）期间持续到达的帧 = 真实尾静音，
    # 供 SMART_TURN_REJUDGE_MS 到期后的"尾静音重判"（模型需要静音证据判断话轮已完）。
    # 放在分支之前：无论 is_user_speaking/state 如何都收（上限 ~2s，防异常堆积）。
    if session.supplement_state == "soft_ended" and session.smart_turn_tail is not None:
        session.smart_turn_tail.extend(pcm)
        _tail_cap = int(SAMPLE_RATE * 2 * 2)  # 最多 2 秒
        if len(session.smart_turn_tail) > _tail_cap:
            session.smart_turn_tail = session.smart_turn_tail[-_tail_cap:]

    # 用户正在说话（speech_start 已确认）→ 优先喂 ASR 实时识别
    # 注意：必须放在 state 缓存分支【前面】——打断 reject 后 state 仍是 speaking，
    #       但 is_user_speaking=True 的用户实时话必须进 ASR（否则进 cache 永远不喂 → 丢字）
    if session.is_user_speaking:
        if session.state in ("speaking", "thinking", "pending_play"):
            # reject 后球球仍在播：先用 preRoll/cache_tail 补用户开头，实时音频继续喂 ASR
            # （不缓存：用户话实时进 ASR，打断确认路径已主动喂 preRoll+cache_tail）
            pass
        # 西西说完后的静默保护期（POST_SPEECH_GUARD_FRAMES 帧内跳过喂 ASR，防尾音回声误识别）
        # 修复：保护期之前只赋值从未检查，实际未生效。
        # 注意：用户确认开口（speech_start 到达）时 handle_speech_start 会立即结束保护，
        #       所以这里不会吞掉用户首字（开口前的首字由前端预卷 256ms 补上）。
        if session.frames_since_speech < POST_SPEECH_GUARD_FRAMES:
            session.frames_since_speech += 1
            return
        asr.feed(session.session_id, pcm)
        return

    if session.state in ("speaking", "thinking", "pending_play"):
        # 西西说话/思考/待播期间：缓存音频（打断信号到达后回放给 ASR）
        session.speaking_audio_cache.extend(pcm)
        if len(session.speaking_audio_cache) > session.MAX_SPEAKING_CACHE:
            session.speaking_audio_cache = session.speaking_audio_cache[-session.MAX_SPEAKING_CACHE:]
        return


def _on_semantic_partial(ws, session, delta_text: str):
    """流式 ASR partial 语义判定（改造：不等 speech_end / finalize 全文）。

    partial（stash 全量修订）一旦出现"非语气词内容" → 提前判"有效指令"：
    - 立即丢弃挂起（被打断的播报确定不再恢复）；
    - 等用户说完 finalize 后直接走新回复（无需等待 speech_end 事件完成语义判定）。
    对"纯语气词/空"的最终确认仍需等说完（只有结束才知道是否为纯语气词），此路径只做正向提前。
    """
    t = (delta_text or "").strip()
    if not session.is_effective_interrupt and t and not _is_filler_word(t):
        session.is_effective_interrupt = True
        if session.suspended_reply:
            print(f"[语义-流式] partial 判定有效: {t[:20]!r} → 提前丢弃挂起（不恢复，不等 speech_end）")
            session.suspended_reply = None
            session.pending_reply_text = ""


def _cancel_supplement_window(session):
    """取消补充窗口定时器（回继续收话 / 提交 / 会话断开时调用，改造清单#6）。

    修复：跳过「当前正在运行的任务」——窗口/重判任务自身在 finish_user_speech 内
    提交时，会调用本函数，若取消自己 → 下一次 await 抛 CancelledError → 提交静默死亡
    （实测：重判 p=0.89 提前提交，日志停在 finalize 前，链路无回复）。
    """
    cur = asyncio.current_task()
    if session._supplement_task and session._supplement_task is not cur and not session._supplement_task.done():
        session._supplement_task.cancel()
    session._supplement_task = None


def _arm_supplement_window(ws, session):
    """SOFT_ENDED 后开启补充窗口（改造清单#6）：

    - 窗口内收到新 speech_start（soft_ended 态）→ 回 pending 继续收话（合并音频）；
    - 窗口结束仍无新语音 → 正式提交该用户 turn（finalize → 语义判定 → LLM）。
    注意：流式 partial 语义判定（_on_semantic_partial）保持即时，不受本窗口阻塞。
    """
    _cancel_supplement_window(session)

    async def _fire():
        try:
            await asyncio.sleep(SUPPLEMENT_WINDOW_MS / 1000)
        except asyncio.CancelledError:
            return
        if session.supplement_state != "soft_ended":
            return  # 已回 pending（用户补充）或已提交
        print(f"[补充窗口] {SUPPLEMENT_WINDOW_MS}ms 无新语音，正式提交用户 turn（revision={session.turn_revision}）")
        session.supplement_state = None
        session.turn_generation += 1  # 提交：generation 前移（旧输出据此失效）
        session.speech_timeout.disarm()
        try:
            await finish_user_speech(ws, session)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[补充窗口] 提交异常: {e}")

    session._supplement_task = asyncio.create_task(_fire())


def _cancel_endpoint_rejudge(session):
    """取消尾静音重判任务（提交/合并/断开时调用）。

    修复：跳过「当前正在运行的任务」——重判任务自身在 _fire 内提交（finish_user_speech）
    时会调用本函数，取消自己会让提交在下一个 await（生产者 ASR finalize 内）被
    CancelledError 打断 → 提前提交链路死亡、无回复（实测复现）。
    """
    cur = asyncio.current_task()
    if session._rejudge_task and session._rejudge_task is not cur and not session._rejudge_task.done():
        session._rejudge_task.cancel()
    session._rejudge_task = None


def _arm_endpoint_rejudge(ws: WebSocket, session: ConversationSession):
    """尾静音重判（端点检测，改造清单#7 实测优化）：

    smart-turn 首判 p≤阈值（开了补充窗口）后，收集真实尾静音 SMART_TURN_REJUDGE_MS
    毫秒（由 handle_audio_frame 在 soft_ended 窗口期写入 session.smart_turn_tail），
    用 [整段话 + 真实尾静音] 再次判定：
    - p > 阈值 → 提前提交（不等整个补充窗口，多数场景 ~600ms 内提交而非 1200ms）；
    - 仍 p ≤ 阈值（或判不了）→ 维持补充窗口，按原 SUPPLEMENT_WINDOW_MS 超时提交。

    实测依据：突兀收尾的完整短句首判低（0.26），补 300ms 真实静音后升至 0.88——
    模型需要"静音证据"判断话轮已完。用真实静音而非合成补零（诚实）：若用户真要续说，
    其韵律仍会被模型判为未完，且 speech_start（soft_ended 分支）会取消本重判走合并。
    """
    _cancel_endpoint_rejudge(session)
    if SMART_TURN_REJUDGE_MS <= 0:
        return

    async def _fire():
        try:
            await asyncio.sleep(SMART_TURN_REJUDGE_MS / 1000)
        except asyncio.CancelledError:
            return
        if session.supplement_state != "soft_ended":
            return  # 已回 pending（用户补充）或已提交
        segment = session.smart_turn_segment
        tail = bytes(session.smart_turn_tail) if session.smart_turn_tail else b""
        extended = (segment or b"") + tail if tail else segment
        _maybe_dump_smart_turn_audio(extended, tag=f"rejudge_tail_{len(tail)}B")
        p2 = smart_turn.judge(extended)
        if p2 is None or not (p2 > SMART_TURN_THRESHOLD):
            print(f"[端点-重判] 补真实尾静音 {len(tail)}B 后 p={p2}，仍判未完 → 维持补充窗口")
            return
        print(f"[端点-重判] 补真实尾静音 {len(tail)}B 后 p={p2:.3f} > 阈值 → 提前提交（不等窗口结束）")
        _cancel_supplement_window(session)
        session.supplement_state = None
        session.turn_generation += 1  # 提交：generation 前移（旧输出据此失效）
        session.speech_timeout.disarm()
        try:
            await finish_user_speech(ws, session)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[端点-重判] 提交异常: {e}")

    session._rejudge_task = asyncio.create_task(_fire())


def _maybe_dump_smart_turn_audio(pcm, tag: str):
    """调试：SMART_TURN_LOG_AUDIO=1 时把判定的音频段 dump 成 16k mono wav（真实语音标定用）"""
    if not SMART_TURN_LOG_AUDIO or not pcm:
        return
    try:
        import os as _os
        import time as _t
        import wave
        _os.makedirs("smart_turn_audio_log", exist_ok=True)
        path = f"smart_turn_audio_log/{_t.strftime('%Y%m%d_%H%M%S')}_{tag}.wav"
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(bytes(pcm))
        print(f"[端点-调试] 已 dump 判定音频 {len(pcm)}B → {path}")
    except Exception as e:
        print(f"[端点-调试] dump 失败: {e}")


def _snapshot_suspended_reply(session):
    """打断发生时快照本轮已生成/已下发但未播完的回复内容（改造清单#4）。

    - 仅在"有内容且尚未快照"时保存（多次打断只保留第一份被打断内容）；
    - 语义裁决（finish_user_speech）后决策：有效 → 真正丢弃；无效 → 恢复重播音。
    """
    if session.pending_reply_text and session.suspended_reply is None:
        session.suspended_reply = {
            "text": session.pending_reply_text,
            "emotion": emotion_state.current,
        }
        print(f"[挂起] 打断快照已完成（{len(session.pending_reply_text)}字），等待语义裁决决定恢复/丢弃")


async def _resume_suspended_reply(ws: WebSocket, session: ConversationSession, rep: dict):
    """语义裁决判"无效打断"时的恢复路径：重播被打断的回复（改造清单#4）。

    说明：当前实现采用"重播整条被打断回复"（重新 TTS 合成下发）；
    TTS 音频以句子粒度重新合成，前端走正常 reply/tts 通道播放（播放器已销毁也可重建）。
    挂起资源的"真正丢弃"发生在语义有效分支（finish_user_speech 启动新流水线前清空）。
    """
    try:
        await ws.send_json({"type": "reply", "text": rep["text"], "emotion": rep["emotion"]})
    except Exception:
        pass
    await session.emit_event(ws, "TTS", f"恢复播报（重播）: {rep['text'][:20]}", duration=0)
    # 恢复播报 = 重新进入播报态：打开打断窗口 + 播放超时兜底
    session.state = "speaking"
    session.speaking_playback_timeout.disarm()
    session.speaking_playback_timeout.arm(lambda: _auto_reset_speaking(ws, session))
    try:
        params = emotion_state.get_tts_params()
        await tts.speak_and_send(ws, rep["text"], session.session_id, params)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print(f"[恢复] 恢复重播 TTS 异常: {e}")
    finally:
        # 整段重发完成即视会话已处理；剩余播放状态由前端 client_playback_done 收尾
        session.pending_reply_text = ""


def _recent_window_rms(confirm_audio) -> float:
    """近窗（最近 CONFIRM_WINDOW_MS 毫秒，与 _confirm_real_speech 同口径）RMS，int16 尺度"""
    try:
        import numpy as _np
        window_bytes = int(SAMPLE_RATE * CONFIRM_WINDOW_MS / 1000 * 2)
        recent = confirm_audio[-window_bytes:] if len(confirm_audio) > window_bytes else confirm_audio
        if not recent:
            return 0.0
        x = _np.frombuffer(bytes(recent), dtype=_np.int16).astype(_np.float32)
        return float(_np.sqrt(_np.mean(x ** 2)))
    except Exception:
        return 0.0


def _head_window_rms(cache_audio) -> float:
    """缓存头部（最近窗口对齐的球球回声区，与 _confirm_real_speech 基线同口径）RMS，int16 尺度"""
    try:
        import numpy as _np
        window_bytes = int(SAMPLE_RATE * CONFIRM_WINDOW_MS / 1000 * 2)
        head = cache_audio[0:window_bytes] if len(cache_audio) >= window_bytes else cache_audio
        if not head:
            return 0.0
        x = _np.frombuffer(bytes(head), dtype=_np.int16).astype(_np.float32)
        return float(_np.sqrt(_np.mean(x ** 2)))
    except Exception:
        return 0.0


def _confirm_real_speech(confirm_audio: bytearray, cache_audio: bytearray) -> bool:
    """后端 Silero VAD 二次确认：判断「首分片」是真人声还是噪声/回声

    改造（改造清单#2）：物理复核素材 = speaking_audio_cache（不含 preRoll）。
    preRoll 只用于打断后喂 ASR 补首字，不参与能量/占比判据。
    - confirm_audio：preRoll + cache 合并段，仅用于闸①材料长度检查（≥CONFIRM_MIN_AUDIO_MS）。
    - cache_audio：能量跃升 + 人声占比的判据素材。
        近窗  = 缓存尾部 CONFIRM_WINDOW_MS（插话段）
        基线  = 缓存头部 CONFIRM_WINDOW_MS（球球开播段，len(cache)≥门槛时保证纯回声）
    - 启用条件：len(cache_audio) ≥ CONFIRM_ENERGY_CACHE_MS 才走能量闸；
      否则短缓存 → 跳过能量闸，占比用更严的 CONFIRM_SPEECH_RATIO_SHORT。
    返回：True = 确认真人声，应该打断；False = 噪声/回声，拒绝打断
    """
    if len(confirm_audio) < int(SAMPLE_RATE * CONFIRM_MIN_AUDIO_MS / 1000 * 2):
        # 材料总长（含 preRoll）太短，无法可靠判断
        print(f"[二次确认] 材料太短（{len(confirm_audio)}字节），拒绝")
        return False

    # 只取「最近 CONFIRM_WINDOW_MS 毫秒」的音频（最大观察窗口，聚焦插话段）
    # ── 修复：近窗取自【confirm_audio = preRoll + 缓存】的尾部 ──
    # preRoll 是 VAD 触发窗口起点前的 256ms，包含开口最开头（首个字「那/对」等）。
    # 改前近窗只取缓存尾部：短缓存/消息滞后时，人声判定看不到首字（只见回声段）
    # → 占比 0 → 首字开口被误判"噪声"拒绝打断、恢复 ducking（实测：第一个字说的"那/对"）。
    # 改后：缓存≥窗口时 confirm 尾部 = 缓存尾部（与旧行为一致，无回归）；
    #       缓存<窗口时近窗 = preRoll + 部分缓存 → 首字人声进入判定输入。
    window_bytes = int(SAMPLE_RATE * CONFIRM_WINDOW_MS / 1000 * 2)
    recent_audio = bytes(confirm_audio[-window_bytes:]) if len(confirm_audio) > window_bytes else bytes(confirm_audio)

    # 能量跃升检测（防噪音/回声被误判为打断）：
    # Silero VAD 无法区分「西西回声」和「用户插话」（都是人声特征）。
    # 用户插话 = 能量从西西回声基线显著跃升；平稳噪音/持续回声 = 前后能量相近。
    # 基线取【缓存头部 512ms】（球球开播段，保证纯回声）；近窗取【缓存尾部 512ms】（插话段）。
    # 仅在缓存足够长（len(cache) ≥ CONFIRM_ENERGY_CACHE_MS）时启用；
    # 短缓存没有可信基线 → 跳过能量闸，靠更严的占比闸兜底。
    cache_ms = len(cache_audio) / (SAMPLE_RATE * 2) * 1000  # 字节→ms（16bit PCM 每样本 2 字节）
    if cache_ms >= CONFIRM_ENERGY_CACHE_MS:
        try:
            import numpy as _np2
            prev_audio = bytes(cache_audio[0:window_bytes])  # 基线 = 头部 512ms（球球开播段）
            if len(prev_audio) == window_bytes:
                prev_np = _np2.frombuffer(prev_audio, dtype=_np2.int16).astype(_np2.float32)
                prev_rms = float(_np2.sqrt(_np2.mean(prev_np ** 2))) if len(prev_np) > 0 else 0.0
                recent_np2 = _np2.frombuffer(recent_audio, dtype=_np2.int16).astype(_np2.float32)
                recent_rms2 = float(_np2.sqrt(_np2.mean(recent_np2 ** 2))) if len(recent_np2) > 0 else 0.0
                if prev_rms > 30:  # 头部基线确有声音（球球开播在响）
                    jump = recent_rms2 / prev_rms if prev_rms > 0 else 0.0
                    if jump < CONFIRM_ENERGY_JUMP:
                        print(f"[二次确认] 能量无跃升（jump={jump:.2f} < {CONFIRM_ENERGY_JUMP}，头部基线RMS={prev_rms:.0f}→近窗RMS={recent_rms2:.0f}），判定平稳噪音/回声，拒绝打断")
                        return False
                    print(f"[二次确认] 能量跃升（jump={jump:.2f} ≥ {CONFIRM_ENERGY_JUMP}），判定用户插话")
                else:
                    print(f"[二次确认] 头部基线RMS={prev_rms:.0f} ≤30（无可靠基线），跳过能量闸")
                    # 实测优化：基线静音 = mic 几乎听不到球球（环境分离度好）。
                    # 占比闸在"轻声插话"下系统性 0.00 误拒（Silero 对低音量语音不敏感），
                    # 改为"近窗 RMS ≥ 下限 → 有真实能量 → 直接确认"，低于下限仍走占比兜底。
                    if CONFIRM_RECENT_MIN_RMS > 0 and recent_rms2 >= CONFIRM_RECENT_MIN_RMS:
                        print(f"[二次确认] 基线静音但近窗有能量（RMS={recent_rms2:.0f} ≥ {CONFIRM_RECENT_MIN_RMS}）→ 判定用户插话")
                        return True
        except Exception as e:
            print(f"[二次确认] 能量跃升检测异常: {e}")

    # 调试：打印音频特征，确认数据正确
    total_bytes = len(confirm_audio)
    recent_bytes = len(recent_audio)
    recent_ms = recent_bytes / 2 / SAMPLE_RATE * 1000
    # 计算 RMS 能量（判断是否有实际声音）
    try:
        import numpy as _np
        recent_np = _np.frombuffer(recent_audio, dtype=_np.int16).astype(_np.float32)
        rms = float(_np.sqrt(_np.mean(recent_np ** 2))) if len(recent_np) > 0 else 0.0
        peak = float(_np.abs(recent_np).max()) if len(recent_np) > 0 else 0.0
        print(f"[二次确认DEBUG] 缓存总{total_bytes}字节({total_bytes/2/16000*1000:.0f}ms), 近窗{recent_bytes}字节({recent_ms:.0f}ms), RMS={rms:.0f}, 峰值={peak:.0f}")
    except Exception as e:
        print(f"[二次确认DEBUG] 能量计算失败: {e}")

    try:
        # 占比分档：长缓存（启用能量闸的同一门槛）用松阈值，短缓存用严阈值
        ratio_thr = CONFIRM_SPEECH_RATIO if cache_ms >= CONFIRM_ENERGY_CACHE_MS else CONFIRM_SPEECH_RATIO_SHORT
        is_speech, ratio = backend_vad.is_speech(
            recent_audio,
            BACKEND_VAD_THRESHOLD,
            ratio_threshold=ratio_thr,
        )
        print(f"[二次确认] cache={cache_ms:.0f}ms，取近窗{CONFIRM_WINDOW_MS}ms, 人声帧占比={ratio:.2f}, 阈值={ratio_thr}, 结果={'确认人声' if is_speech else '判定噪声'}")
        return is_speech
    except Exception as e:
        print(f"[二次确认] 异常: {e}")
        # 异常时保守处理：拒绝打断（避免误打断）
        return False


async def finish_user_speech(ws: WebSocket, session: ConversationSession):
    """用户说完了（前端 speech_end / 补充窗口结束）：跑 ASR 识别，过滤语气词噪声，交给 LLM/TTS"""
    import time
    # 补充窗口（清单#6）：本次提交就是用户轮的终结——关闭窗口定时器、复位状态，防重复提交
    _cancel_supplement_window(session)
    _cancel_endpoint_rejudge(session)  # 提交即重判终止（尾静音不再收集）
    session.supplement_state = None
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

    # ── ASR 语音判定结果 → 决策（改造：流式 partial 已提前判定 + finalize 兜底）──
    # effective = 流式阶段已确认"有效指令"（不等 speech_end / finalize 全文）
    # 判定规则（已取消"极短文本过滤"——不以长度作为恢复/无效判据）：
    #   有效  = 流式提前判有效 或 finalize 文本非空且非纯语气词 → 丢弃挂起，走新回复
    #   无效  = 空识别 / 纯语气词 → 恢复重播（有挂起）或 resume_playback
    effective = session.is_effective_interrupt
    session.is_effective_interrupt = False  # 复位（下一轮打断重新判定）
    if not effective and (not text or _is_filler_word(text)):
        await session.emit_event(ws, "ASR", f"识别为语气词『{text}』，已过滤", duration=round(t_asr, 2))
        session.reset_episode()
        # 语气词被过滤，恢复 listening 状态，让主循环继续端点检测
        session.state = "listening"
        session.reset_speech_guard()
        # ── 改造清单#4：语义裁决判"无效打断" → 决策依据挂起上下文 ──
        # 有被打断且未播完的回复 → 恢复（重播音）；否则 → resume_playback（清音量状态）
        if session.suspended_reply:
            rep = session.suspended_reply
            session.suspended_reply = None  # 挂起内容被消费（恢复后不再保留）
            print(f"[恢复] 语义裁决无效，恢复被打断的播报（重播 {len(rep['text'])} 字）")
            if session.user_speech_task and not session.user_speech_task.done():
                session.user_speech_task.cancel()  # 防并发：先停旧流水线
                try:
                    await session.user_speech_task
                except (asyncio.CancelledError, Exception):
                    pass
            session.user_speech_task = asyncio.create_task(_resume_suspended_reply(ws, session, rep))
        else:
            # 修复：若这次是「打断后无有效输入」（用户其实没真说话 / 误断），
            # 通知前端恢复之前被打断的播报（前端 barge_confirm 时只是静音等待，未销毁播放器）。
            # 正常说话被过滤时前端无 pendingResume，此消息无害。
            try:
                await ws.send_json({"type": "resume_playback"})
            except Exception:
                pass
        return

    await session.emit_event(ws, "ASR", f"识别结果：{text}", duration=round(t_asr, 2))
    # ASR 指标口径（用户确认）：t_asr = 本次（最后一次）finalize 耗时——
    # 补充说话/补充窗口场景下也只计"最后一次提交"的 commit 识别时长，
    # 不把整个 turn 的累积识别（多个碎片）算成一次 ASR 时间。
    session.last_asr_time = round(t_asr, 2)
    # 通知前端 ASR 完成，收尾流式展示（把 asr_partial 的状态转正为最终结果）
    await ws.send_json({"type": "asr_final", "text": text})

    # ── 语音指令：模式切换（打开工作模式/打开闲聊模式/切换模式，子串部分命中）──
    # 命中 → 切模式 + 发【文字】系统通知（不播报 TTS），然后把用户整句输入 + 切换状态
    # 上下文一起送进 LLM 生成第一轮回复（继续处理用户本句的实际任务）。
    matched, target = parse_mode_command(text)
    switch_ctx = None
    if matched:
        # F4 隔离：语音切换只改【本会话】模式（不再写全局单例，防跨会话影响）
        new_mode = target if target else "work"
        session.mode = new_mode
        print(f"[模式] 语音指令切换(会话级) → {new_mode}")
        await session.emit_event(ws, "模式", f"系统通知：已经切换到{'工作模式' if new_mode == 'work' else '闲聊模式'}")
        await ws.send_json({
            "type": "mode_changed",
            "mode": new_mode,
            "notice": f"已经切换到{'工作模式' if new_mode == 'work' else '闲聊模式'}，继续处理你的请求",
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
        _run_in_session_mode(session, handle_user_speech(ws, session, text, extra_context=switch_ctx))
    )
    # 改造清单#4：语义裁决判"有效" → 这轮打断的挂起内容真正丢弃（新回复接管）
    session.suspended_reply = None
    session.reset_episode()


async def handle_barge_in(ws: WebSocket, session: ConversationSession):
    """打断：立即停止 TTS，西西先应一声「嗯？」"""
    import time
    # 计算打断延迟 = 从西西开始说话到被打断的时间
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
    _snapshot_suspended_reply(session)  # 改造清单#4：打断先快照挂起内容
    session.abort_speaking = True
    await ws.send_json({"type": "barge_in"})
    # 发送打断延迟平均值到前端
    if avg_barge is not None:
        await ws.send_json({"type": "barge_avg", "avg": avg_barge, "count": session.barge_count})
    # 西西被打断后先回应「嗯？」（后台播放，不阻塞音频接收）
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


async def handle_speech_start(ws: WebSocket, session: ConversationSession, pre_roll_b64: str = None, is_playing: bool = False):
    """后端业务决策：前端检测到人声，基于当前状态决定是否打断

    双层架构：前端负责体感（已做 ducking + 上传预卷），后端负责业务决策。
    二次确认输入 = 前端预卷(256ms) + 后端最近音频，最大观察窗口 CONFIRM_WINDOW_MS。
    """
    import time as _t
    _t_recv = _t.time()
    import sys
    print(f"[状态机] speech_start 到达, 当前 session.state={session.state!r}, _last_notified={getattr(session, '_last_notified_state', '?')}, 到达时刻={_t_recv:.3f}", file=sys.stderr, flush=True)

    # ── 补充说明窗口（改造清单#6/#7）：端点判定的收话期收到新语音 → 同一 turn 的补充/续说 ──
    # 端点窗口（soft_ended，正在等补充）→ 回 pending 继续收话（ASR 会话不 finalize = 音频合并），
    # turn_revision+1；补充收话中（pending）再次开口（停顿后继续说）→ 保持收话，视为同一 turn。
    if session.supplement_state in ("pending", "soft_ended"):
        if session.supplement_state == "soft_ended":
            print(f"[补充窗口] 窗口内检测到新语音 → 回补充收话（revision {session.turn_revision}→{session.turn_revision + 1}）")
            session.turn_revision += 1
            session.supplement_state = "pending"
            _cancel_supplement_window(session)
            _cancel_endpoint_rejudge(session)  # 用户续说 → 旧段/尾静音作废，重判取消
            session.smart_turn_tail = None
        session.frames_since_speech = POST_SPEECH_GUARD_FRAMES
        session.silence_frames = 0
        session.speech_frames = 0
        return

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

    # 统一解码前端预卷（供二次确认 / 打断补首字 / pending_play 收话共用）
    pre_roll_pcm = None
    if pre_roll_b64:
        try:
            import base64 as _b64
            pre_roll_pcm = _b64.b64decode(pre_roll_b64)
        except Exception as e:
            print(f"[状态机] 预卷解码失败: {e}")

    if session.state == "pending_play":
        # 待播态：TTS已下发但喇叭未响，用户说话了 → 直接丢弃待播任务，切 listening
        # （快速打断，不走 ASR——与 speaking 分支一致的 16ms 级响应）
        import sys
        print(f"[状态机] state=pending_play → 丢弃待播任务，切 listening", file=sys.stderr, flush=True)
        # 取消当前 TTS 任务（还没播，直接取消）+ 整条流水线任务
        if session.tts_task and not session.tts_task.done():
            session.tts_task.cancel()
        _snapshot_suspended_reply(session)  # 改造清单#4：打断先快照挂起内容
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
            _on_semantic_partial(ws, session, delta_text)  # 流式语义判定（不等 speech_end）
        asr.start_streaming(session.session_id, _on_partial)
        session.speech_start_ts = _t.time()  # 记录本次 speech_start 时刻（会话过期兜底）
        # 把预卷 + 待播期间缓存的音频喂给 ASR（防窗口吞字）
        if pre_roll_pcm:
            asr.feed(session.session_id, pre_roll_pcm)
            print(f"[状态机] pending_play: 已喂入前端预卷 {len(pre_roll_pcm)}字节, cache_len={len(session.speaking_audio_cache)}", file=sys.stderr, flush=True)
        # ── 修复打断后丢字：pending_play 也只喂 cache 尾部 200ms（不喂整段，避免球球回声污染）──
        if len(session.speaking_audio_cache) > 0:
            tail_bytes = int(SAMPLE_RATE * 200 / 1000 * 2)  # 200ms
            cache_tail = session.speaking_audio_cache[-tail_bytes:]
            asr.feed(session.session_id, bytes(cache_tail))
            session.speaking_audio_cache = bytearray()
            import sys
            print(f"[状态机] pending_play 打断：已喂入待播缓存尾部 200ms (减少球球回声)", file=sys.stderr, flush=True)
        # 依赖前端的 preRoll（256ms）补用户首字
        session.is_user_speaking = True
        session.turn_generation += 1          # 打断：旧 generation 作废（防泄漏）
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
            "content": "(主人打断了西西的上一轮回复，请重新听主人接下来的话)",
        })
        return

    if session.state == "speaking":
        # 西西正在说话 → 需要后端 VAD 二次确认「这是真人声还是噪声/回声」
        import sys
        print(f"[状态机] state=speaking → 后端 VAD 二次确认", file=sys.stderr, flush=True)
        # 二次确认输入 = 前端预卷 + 后端最近音频 合并
        # 修复：预卷（开口前256ms）必须拼在【前面】。_confirm_real_speech 只取
        #       「最近 CONFIRM_WINDOW_MS=256ms」，如果预卷拼末尾，最近256ms 全是
        #       开口前的静音/回声 → RMS≈0 → 误判噪声 → 正常插话被误报拒绝（实测复现）
        if pre_roll_pcm:
            print(f"[状态机] 解码前端预卷 {len(pre_roll_pcm)} 字节")
        confirm_audio = bytearray(pre_roll_pcm or b"") + session.speaking_audio_cache

        # 改造（清单#2）：物理复核素材 = speaking_audio_cache（preRoll 退出物理判据）
        confirmed = _confirm_real_speech(confirm_audio, session.speaking_audio_cache)
        # ── 静音窗重试（实测优化）：speech_start 控制消息可能早于音频帧到达 →
        # 复核近窗此刻几乎静音（RMS≈0/占比 0.00）误拒真插话；延迟用新缓存重验一次，
        # 仍失败才定论拒断。重试期间 state=speaking、is_user_speaking=False，
        # 到达的音频照常进 speaking_audio_cache（下方缓存分支）。
        if not confirmed and CONFIRM_RETRY_MS > 0:
            import sys
            print(f"[二次确认] 首验未通过（近窗可能无音频到达），{CONFIRM_RETRY_MS}ms 后重验", file=sys.stderr, flush=True)
            await asyncio.sleep(CONFIRM_RETRY_MS / 1000)
            confirm_audio = bytearray(pre_roll_pcm or b"") + session.speaking_audio_cache
            confirmed = _confirm_real_speech(confirm_audio, session.speaking_audio_cache)

        # ── 基线静音→信任前端（实测常见环境）：首验+重验都失败，且【头部基线】近静音
        # （麦克风采不到球球回声=无回声误触风险；近窗可能有微弱开口能量 RMS 几十~百，
        #  0.6 口径下 Silero 仍全不过线 → 占比 0）→ 物理复核材料不可信 → 信任前端 VAD。
        # 误断有挂起+语义裁决兜底；拒不打断 = 抢话+等窗口，体验更差 → 确认打断。
        if not confirmed and CONFIRM_TRUST_FRONTEND_BASELINE_RMS > 0:
            head_rms = _head_window_rms(session.speaking_audio_cache)
            if head_rms < CONFIRM_TRUST_FRONTEND_BASELINE_RMS:
                import sys
                print(f"[二次确认] 头部基线RMS={head_rms:.0f} < {CONFIRM_TRUST_FRONTEND_BASELINE_RMS}（采不到球球回声，无回声误触风险）→ 信任前端 VAD，确认打断", file=sys.stderr, flush=True)
                confirmed = True

        if not confirmed:
            # 二次确认失败：可能是西西回声或噪声，拒绝打断
            import sys
            print(f"[状态机] 二次确认失败（噪声/回声），拒绝打断，恢复音量, cache_len={len(confirm_audio)}", file=sys.stderr, flush=True)
            await session.emit_event(ws, "后端VAD", "二次确认判定噪声 → 拒绝打断")
            await ws.send_json({"type": "barge_reject"})
            # ── 修复打断后丢字：reject 也启动 ASR 接收用户话 ──
            # 后端判定「不是打断（噪声/回声）」，但前端 VAD 已确认人声（preRoll 非空）。
            # 双层架构下 reject 只代表「不打断球球」，不代表用户没说话。
            # 若不启动 ASR：用户话一直进 speaking_audio_cache（不喂 ASR），且前端 VAD
            # 在 onSpeechStart 周期内不再重发 speech_start → 用户说完 speech_end 时
            # 没有 ASR 会话 → finalize 空 → 用户的话整段丢失（实测「打断后说话丢失」）。
            # 方案：仍然启动 ASR + 喂 preRoll/cache_tail，但 state 保持 speaking（球球继续播），
            # user 话被识别；speech_end 时 finish_user_speech 正常处理。
            if pre_roll_pcm or session.speaking_audio_cache:
                loop = asyncio.get_event_loop()
                partial_buffer = {"text": ""}
                def _on_partial(delta_text):
                    partial_buffer["text"] = delta_text  # 流式 partial 是全量修订(stash)，覆盖而非追加
                    async def _send():
                        await ws.send_json({"type": "asr_partial", "text": partial_buffer["text"]})
                    asyncio.run_coroutine_threadsafe(_send(), loop)
                    _on_semantic_partial(ws, session, delta_text)  # 流式语义判定（不等 speech_end）
                asr.start_streaming(session.session_id, _on_partial)
                session.speech_start_ts = _t.time()
                if pre_roll_pcm:
                    asr.feed(session.session_id, pre_roll_pcm)
                if len(session.speaking_audio_cache) > 0:
                    tail_bytes = int(SAMPLE_RATE * 200 / 1000 * 2)  # 200ms
                    cache_tail = session.speaking_audio_cache[-tail_bytes:]
                    asr.feed(session.session_id, bytes(cache_tail))
                    session.speaking_audio_cache = bytearray()
                session.is_user_speaking = True  # 让后续实时音频喂 ASR
                print(f"[状态机] reject 后仍启动 ASR 接收用户话（不打断球球），is_user_speaking=True", file=sys.stderr, flush=True)
            return

        # 二次确认通过：确认真打断（快速，~16ms，不走 ASR——ASR 确认太慢影响打断体验）
        _t_confirm = _t.time()
        _backend_ms = (_t_confirm - _t_recv) * 1000
        import sys
        print(f"[状态机] 二次确认通过 → 确认真打断, 后端处理耗时={_backend_ms:.0f}ms, cache_len={len(confirm_audio)}", file=sys.stderr, flush=True)
        await session.emit_event(ws, "后端VAD", "二次确认判定人声 → 确认真打断")
        print(f"[打断DEBUG] 打断前 tts_task 状态: done={session.tts_task.done() if session.tts_task else 'None'}")
        # 1. 取消 TTS 和 LLM 流水线（先停当前句 TTS，再取消整条流水线任务）
        _snapshot_suspended_reply(session)  # 改造清单#4：打断先快照挂起内容
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
            _on_semantic_partial(ws, session, delta_text)  # 流式语义判定（不等 speech_end）
        asr.start_streaming(session.session_id, _on_partial)
        session.speech_start_ts = _t.time()  # 记录本次 speech_start 时刻（会话过期兜底）

        # 4. 把用户插话相关音频喂给 ASR（防窗口吞字）：
        #    - 前端预卷（开口前256ms）：补 VAD 触发延迟丢的首字
        #    - 缓存尾部最近 1s：用户开口后的部分
        #    修复：之前喂整段缓存（最长2s，大部分是西西回声），
        #          ASR 先识别出西西说的话再识别用户话 → 识别错乱/丢字（实测「从无属」）
        if pre_roll_pcm:
            asr.feed(session.session_id, pre_roll_pcm)
            print(f"[状态机] 已喂入前端预卷 {len(pre_roll_pcm)} 字节")
        if len(session.speaking_audio_cache) > 0:
            # 修复打断后丢字：cache_tail_ms 从 1000ms 缩到 200ms
            # 1000ms 球球回声污染 ASR 段分页（用户实际话被覆盖），实测丢字严重
            # 200ms 平衡：覆盖 VAD 触发延迟（用户说 ~300-500ms 还没触发 VAD，前 300ms 在 cache），
            #          又不会引入太多球球回声污染
            cache_tail_ms = 200
            tail_bytes = int(SAMPLE_RATE * cache_tail_ms / 1000 * 2)
            cache_tail = session.speaking_audio_cache[-tail_bytes:]
            asr.feed(session.session_id, bytes(cache_tail))
            session.speaking_audio_cache = bytearray()
            import sys
            print(f"[状态机] 已喂入缓存尾部 {len(cache_tail)} 字节 (200ms, 减少球球回声污染)", file=sys.stderr, flush=True)

        # 5. 进入 listening，重置端点检测，让用户的话能被识别
        session.state = "listening"
        # 契约：确认真打断 → 复位 listening + 通知；取消播放超时，启动收音超时
        session.speaking_playback_timeout.disarm()
        await _sync_backend_state(ws, session, "barge_confirmed")
        session.speech_timeout.arm(lambda: _auto_exit_speech(ws, session))
        session.is_user_speaking = True
        session.turn_generation += 1          # 打断：旧 generation 作废（防泄漏）
        session.silence_frames = 0
        session.speech_frames = 0
        session.frames_since_speech = POST_SPEECH_GUARD_FRAMES

        # 6. 打断延迟已改为前端上报（barge_latency 消息），这里不再计算
        session.speaking_start_time = None

        # 7. 打断历史标记：让 LLM 不延续旧话题
        session.history.append({
            "role": "user",
            "content": "(主人刚才打断了西西的上一轮回复，请不要再继续上一个话题，重新听主人接下来的话)",
        })

    elif session.state == "thinking":
        # 西西在思考（LLM生成中）→ 用户开口，可能是插话，也可能只是咳嗽
        # 保守处理：先 ducking（前端已做），等 speech_end 确认是不是真说话
        print(f"[状态机] state=thinking → 暂不打断，等 speech_end 确认")

    elif session.state == "listening":
        # 西西没在说话 → 正常收话；但若前端此刻仍有球球语音在播（is_playing=True），
        # 说明 client_playback_done 兜底/竞态已提前关掉打断窗口，而球球还有旧音频在播：
        # 只启动 ASR 不掐断 → 球球旧音频（ducking 后还可能恢复音量）与用户新话穿插。
        # 处理：立即掐断前端（barge_confirm 语义，走与 pending_play/speaking 一致的打断），
        # 再进入正常收话。listening 态没有 speaking_audio_cache（音频已丢弃），
        # 二次确认无输入必然判噪声拒绝，故此路径跳过二次确认、直接信任前端在播标记。
        if is_playing:
            import sys
            print(f"[状态机] state=listening 但前端仍在播（is_playing=True）→ 立即掐断前端", file=sys.stderr, flush=True)
            await session.emit_event(ws, "后端VAD", "listening 但前端在播 → 立即掐断")
            # 取消 TTS 和 LLM 流水线（与 speaking 打断一致）
            _snapshot_suspended_reply(session)  # 改造清单#4：打断先快照挂起内容
            tts.cancel()
            if session.tts_task and not session.tts_task.done():
                session.tts_task.cancel()
            session.abort_speaking = True
            if session.user_speech_task and not session.user_speech_task.done():
                session.user_speech_task.cancel()
            # 通知前端销毁播放器（丢弃旧音频），前端 barge_confirm 分支会立即 stopStreamPlayback
            await ws.send_json({"type": "barge_confirm", "backend_ms": 0})
            session.turn_generation += 1          # 打断：旧 generation 作废（防泄漏）
            session.speaking_start_time = None
            # 历史标记：上一轮被掐断
            session.history.append({
                "role": "user",
                "content": "(主人打断了西西的上一轮回复，请重新听主人接下来的话)",
            })
        # 正常收话：启动 ASR 会话，开始累积用户音频
        import sys
        print(f"[状态机] state=listening → 正常说话，启动 ASR", file=sys.stderr, flush=True)
        loop = asyncio.get_event_loop()
        partial_buffer = {"text": ""}
        def _on_partial(delta_text):
            partial_buffer["text"] = delta_text  # 流式 partial 是全量修订(stash)，覆盖而非追加
            async def _send():
                await ws.send_json({"type": "asr_partial", "text": partial_buffer["text"]})
            asyncio.run_coroutine_threadsafe(_send(), loop)
            _on_semantic_partial(ws, session, delta_text)  # 流式语义判定（不等 speech_end）
        asr.start_streaming(session.session_id, _on_partial)
        session.speech_start_ts = _t.time()  # 记录本次 speech_start 时刻（会话过期兜底）
        # 喂 preRoll 补首字（listening 态无 cache 可喂，只有预卷）：
        # ── 修复：原来只有 is_playing 才喂——普通对话（非打断）is_playing=False 时
        #    不喂，而 listening 态开口前的 ~192ms 前导帧在 handle_audio_frame 无分支
        #    被丢弃 → ASR 首字缺失/错认（实测"那/对"类首字识别错误）。
        #    预卷 = VAD 触发前 256ms（含开口首字），无条件喂入补回。
        if pre_roll_pcm:
            asr.feed(session.session_id, pre_roll_pcm)
            print(f"[状态机] listening 收话：已喂入前端预卷 {len(pre_roll_pcm)} 字节", file=sys.stderr, flush=True)
        session.is_user_speaking = True
        session.silence_frames = 0
        session.speech_frames = 0
        # 用户已被前端 VAD 确认真开口 → 立即结束静默保护期，
        # 否则西西说完后用户马上开口，保护期内的实时音频会被跳过吞掉首字
        session.frames_since_speech = POST_SPEECH_GUARD_FRAMES
        # 契约：listening 收音超时兜底（speech_start 后若无 speech_end，自动退出收音）
        session.speech_timeout.arm(lambda: _auto_exit_speech(ws, session))


async def handle_speech_end(ws: WebSocket, session: ConversationSession, audio_b64: str | None = None):
    """前端检测到人声结束 → 通用端点检测（改造清单#7：SmartTurn 完说完判定，非 barge-in）。

    所有链路（正常收话 / 打断后收话）统一走这里，判断"这句说完了吗"：
    - p ≤ SMART_TURN_THRESHOLD（或 fallback=window）→ 可能未完 → 开 SUPPLEMENT_WINDOW_MS 补充窗口，
      窗口内再说话则合并为同一 turn（revision+1、音频并入 ASR），超时无补充才正式提交；
    - p > SMART_TURN_THRESHOLD（或 fallback=direct / 无 audio）→ 已说完 → 立即提交（finalize → 语义 → LLM）。
    """
    print(f"[状态机] speech_end 到达, 当前 state={session.state}")

    if session.state == "thinking":
        # 思考期间用户开口又结束：可能是咳嗽/短促噪声，也可能是真的要说话
        # 这里简单处理：如果是 thinking 且用户说了话，等下一轮正常识别
        print(f"[状态机] state=thinking → speech_end，忽略（思考期不识别）")
        return

    if not session.is_user_speaking:
        return

    # ── 端点检测：smart-turn 判定"话轮是否完结" ──
    pcm = None
    if audio_b64:
        try:
            import base64 as _b64
            pcm = _b64.b64decode(audio_b64)
        except Exception as e:
            print(f"[端点] 音频段解码失败: {e}")
    p = smart_turn.judge(pcm)
    _maybe_dump_smart_turn_audio(pcm, tag="first_judge")
    finished = smart_turn.say_finished(p, fallback=SMART_TURN_FALLBACK)
    print(f"[端点] smart_turn p={p} → {'已说完，直接提交' if finished else '可能未完，开补充窗口'}")

    if finished:
        print(f"[状态机] 用户说完，触发 finalize")
        session.speech_timeout.disarm()  # 收音结束，取消超时兜底
        _cancel_endpoint_rejudge(session)  # 清理可能残留的重判（正常收话无此分支）
        await finish_user_speech(ws, session)
        return

    # 可能未完 → 开启补充说明窗口（等待续说；超时无补充才正式提交）
    # 记录本次判定的整段话 + 开始收集真实尾静音（给"尾静音重判"用，提前提交多数场景）
    print(f"[补充窗口] 端点判定可能未完（p={p}），开启 {SUPPLEMENT_WINDOW_MS}ms 补充窗口")
    session.supplement_state = "soft_ended"
    session.speech_timeout.disarm()
    session.smart_turn_segment = pcm
    session.smart_turn_tail = bytearray()
    _arm_supplement_window(ws, session)
    _arm_endpoint_rejudge(ws, session)


def _build_timing_stats(session, t0, t_llm_first_sentence, t_llm, t_tts, include_in_avg: bool = True):
    """计算本轮耗时统计（current + avg），正常收尾与打断补发共用

    - 正常收尾：LLM 完整生成 + TTS 完整发送 → 计入 avg
    - 打断补发（include_in_avg=False）：t_tts=0（TTS 未完整发送），数据是部分值，
      会污染 avg（e2e 偏小因为没 TTS 首包），只发 current 不进 sum

    指标口径（用户确认）：
    - 用户体感 E2E = 前端 client_real_e2e（最后一次 VAD 判定结束 → 第一帧出声），
      服务端 current["e2e"] 只是服务端首响参考（asr + 首句 + 首包），不替代前者；
    - 所有服务端分段都从"最后一次说完"之后起算：ASR=最后一次 finalize 耗时、
      LLM 首句/完成从 t0（finalize 完成后流水线起点）起算；
    - 不提供 Total（旧的 asr+t_llm+t_tts 会重复计 TTS，已废弃）。

    同时维护首轮首响指标（first_e2e / first_llm_first_sentence / first_tts_first_packet）：
    多轮 LLM+tool 时，「用户第一次听到声音」就是首轮的 e2e；后续轮的 e2e/tts_first_packet
    会覆盖 current，首轮锁定避免被末轮污染（评测中心要按"首响延迟"算 E2E）。

    返回 (current, avg)：avg 在 include_in_avg=False 时保持旧值不变（用现有 avg_count 算）
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
    }
    # 首轮锁定：仅正常收尾轮（include_in_avg=True）且尚未记录时，锁定首轮首响指标
    # 后续轮的 e2e/tts_first_packet 会随 tool 调用和后续 LLM 回复被覆盖，污染"首响"语义
    if include_in_avg and not getattr(session, "first_round_locked", False):
        asr0 = current["asr"]
        llm_fs0 = current["llm_first_sentence"]
        tts_fp0 = current["tts_first_packet"]
        e2e0 = current["e2e"]
        # 仅在首轮数据齐全时记录（保证有真正的"首次出声"）
        if (asr0 or llm_fs0) and tts_fp0:
            session.first_e2e = e2e0
            session.first_llm_first_sentence = llm_fs0
            session.first_tts_first_packet = tts_fp0
            session.first_round_locked = True
    # 把首轮数据也放进 current 让前端可读
    if getattr(session, "first_round_locked", False):
        current["e2e_first_round"] = getattr(session, "first_e2e", 0)
        current["llm_first_sentence_first_round"] = getattr(session, "first_llm_first_sentence", 0)
        current["tts_first_packet_first_round"] = getattr(session, "first_tts_first_packet", 0)
    session.timing_count += 1  # 总轮次（含打断）—— 前端展示用
    if include_in_avg:
        session.avg_count += 1  # 仅完整轮计入 avg 分母
        for k, v in current.items():
            if k in ("e2e_first_round", "llm_first_sentence_first_round", "tts_first_packet_first_round", "interrupted"):
                continue  # 首轮锁定/打断标记字段不进 avg（avg 只统计常规指标）
            session.timing_sum[k] += v
    avg = {}
    if session.avg_count > 0:
        avg_keys = [k for k in current.keys()
                    if k not in ("e2e_first_round", "llm_first_sentence_first_round", "tts_first_packet_first_round", "interrupted")]
        avg = {k: round(session.timing_sum[k] / session.avg_count, 2) for k in avg_keys}
    return current, avg


async def _safe_send_timing(ws, current, avg, count, avg_count=0):
    """独立任务发送 timing（供打断补发使用——任务取消状态下不能直接 await 发送）

    avg_count: 计入avg的轮数（不含打断轮）——前端调试用，确认 avg 是不是真的有数据
    """
    try:
        await ws.send_json({"type": "timing", "current": current, "avg": avg, "count": count, "avg_count": avg_count})
    except Exception:
        pass


async def handle_user_speech(ws: WebSocket, session: ConversationSession, text: str,
                             extra_context: str | None = None,
                             temperature: float | None = None):
    """用户说完一句话：进入思考 → LLM 流式逐句 → TTS 逐句合成播放

    extra_context: 可选系统上下文（模式切换状态），一并送入 LLM 生成第一轮回复。
    temperature:   可选温度覆盖（评测中心 Step4 注入；None = 后端默认 0.7）
    """
    import time

    session.state = "thinking"
    # 评测中心注入的温度 → 持久化到 session，供 run_agent_loop 读取
    if temperature is not None:
        session.temperature = temperature
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
    session.pending_reply_text = ""  # 新一轮开始，重置"已下发回复"轨迹（供打断挂起快照）

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
    # 按会话模式选 LLM：工作模式默认 DeepSeek（工具调用更稳）；闲聊跟随 LLM_PROVIDER
    _llm = get_llm_for_mode(mode)
    if extra_context:
        system_prompt += "\n\n" + extra_context
    session.store.add("user", text, run_id=run_id, sub_turn=1)

    progress_task = None  # 工具开始前的进度播报任务
    progress_announced = False  # 整个 run 内是否已播过一次进度占位（多工具/多 sub_turn 只播一次）

    async def _on_tool(stage, name, call_id, args):
        """原生工具调用回调（agent_runtime 以 4 参调用：stage, name, call_id, args）。
        stage: "start"（工具开始执行）/ "end"（工具执行完）。
        后端打印「调用开始（工具名）/ 调用结束」+ 事件流展示。
        进度播报只在 start 且首轮时触发（_TOOL_PROGRESS 罐头），end 不播报。
        """
        import time as _tt
        import sys
        nonlocal progress_task, progress_announced
        if stage == "start":
            print(f"[工具调用] >>> 开始 {name}  # call_id={call_id}  t={_tt.strftime('%H:%M:%S')}", file=sys.stderr, flush=True)
            # 工具执行也算说话中的“活跃”：滑动重置播放超时，长工具调用不会被兜底误掐
            session.speaking_playback_timeout.arm(lambda: _auto_reset_speaking(ws, session))
            await session.emit_event(ws, "工具", f"开始调用：{name}")
            progress_text = _TOOL_PROGRESS.get(name)
            # 去重：单流式下 LLM 可能已用「前言」播报过第一轮（full_reply 非空）→
            # 罐头进度不再播，避免前言+进度双播；未播过前言（full_reply 仍空）才播罐头进度。
            if progress_text and not progress_announced and not full_reply:
                progress_announced = True
                progress_task = asyncio.create_task(
                    tts.speak_and_send(ws, progress_text, session.session_id, {"emotion": "平静"})
                )
        elif stage == "end":
            # agent_runtime 在 end 阶段传入的是工具执行结果 result（第 4 参）
            # 打印「工具调用 + 工具调用结果」，结果仅展示开头一段（截断，防日志爆炸）
            result_str = args if isinstance(args, str) else str(args)
            result_str = result_str.replace("\n", " ").strip()
            if len(result_str) > 80:
                result_str = result_str[:80] + "…"
            print(f"[工具调用] <<< 结束 {name}  # call_id={call_id}  t={_tt.strftime('%H:%M:%S')}", file=sys.stderr, flush=True)
            print(f"[工具调用]     结果: {result_str}", file=sys.stderr, flush=True)
            try:
                await session.emit_event(ws, "工具", f"调用结束：{name}")
            except Exception:
                pass

    async def _summarize(prompt_text: str) -> str:
        """独立无工具模型调用，生成/更新压缩检查点（超长对话才触发）。

        摘要长度按 COMPACT_SUMMARY_RATIO(0.1) 约束：约为被压缩历史 token 的 10%，
        设上下限 [200, 2000] 防止过短/过长。
        """
        from agent_config import COMPACT_SUMMARY_RATIO
        # 粗略估算被压缩历史 token（prompt 文本长度 / 4），乘 0.1 作为摘要预算
        hist_tokens = max(1, len(prompt_text) // 4)
        budget = max(200, min(2000, int(hist_tokens * COMPACT_SUMMARY_RATIO)))
        resp = await _llm.client.chat.completions.create(
            model=_llm.model,
            messages=[
                {"role": "system", "content": COMPACTION_SYSTEM_PROMPT},
                {"role": "user", "content": prompt_text},
            ],
            max_tokens=budget,
            stream=False,
        )
        if not getattr(resp, "choices", None):
            return ""
        return resp.choices[0].message.content or ""

    # 后台任务（改造计划最小闭环）：设置当前会话钩子，供 delegate_task 工具读取
    from task_service import TaskContext
    TaskContext.set_current(ws, session)
    try:
        # 新架构：原生 function calling 多 sub_turn agent 环（会话层/上下文层/压缩/工具并发都在环内）
        async for ev in run_agent_loop(
            _llm.client, _llm.model, mode, system_prompt, session.store,
            run_id=run_id,
            user_profile=load_user_profile(get_active_user_id()),
            compaction_state=session.agent_compaction,
            summarizer=_summarize,
            on_tool=_on_tool,
            memory_fs=memory_fs,
            timeout=getattr(_llm, 'timeout', None),
            temperature=getattr(session, 'temperature', None),   # 评测中心可注入（默认 None = 0.7）
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
            session.pending_reply_text += sentence  # 供打断挂起快照（改造清单#4）

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
                # 滑动重 arm：每句 TTS 完成即重置播放超时窗口。
                # 长回复（多句/多轮）不会因整轮超过 SPEAKING_PLAYBACK_TIMEOUT_S 被
                # _auto_reset_speaking 误掐（之前只在回复开始时 arm 一次，20s 必炸）。
                session.speaking_playback_timeout.arm(lambda: _auto_reset_speaking(ws, session))
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
        # 打断轮 include_in_avg=False：TTS 未完整，e2e/total 偏小会污染 avg
        current, avg = _build_timing_stats(session, t0, t_llm_first_sentence, t_llm, 0, include_in_avg=False)
        current["interrupted"] = True  # 标记：该轮被打断（部分数据）
        asyncio.create_task(_safe_send_timing(ws, current, avg, session.timing_count, session.avg_count))
        if session.tts_task and not session.tts_task.done():
            session.tts_task.cancel()
        raise
    except Exception as e:
        # LLM 流式异常（网络/限流/额度耗尽，如 403 FreeTierOnly）：
        # 给前端可见反馈 + 状态复位，避免「卡 speaking 无下文」。
        print(f"[流水线] LLM 流式异常: {e}")
        try:
            await session.emit_event(ws, "LLM", f"流式异常: {str(e)[:80]}", duration=0)
        except Exception:
            pass
        # 取消当前句 TTS、解除 speaking 播放超时、停止占位音频
        if session.tts_task and not session.tts_task.done():
            try:
                session.tts_task.cancel()
            except Exception:
                pass
        session.speaking_playback_timeout.disarm()
        try:
            tts.cancel()
        except Exception:
            pass
        try:
            await ws.send_json({"type": "stop_placeholder"})
        except Exception:
            pass
        # 给前端一条可见的道歉回复（无 TTS 音频；前端 reply → reply_end 自行回 listening）
        try:
            await ws.send_json({"type": "reply", "text": "哎呀，我的大脑暂时卡住啦… 稍等一下再找我说话吧？", "emotion": "委屈"})
            await ws.send_json({"type": "reply_end"})
        except Exception:
            pass
        # 状态机复位回 listening（force 确保通知发出，哪怕刚广播过 speaking）
        session.state = "listening"
        await _sync_backend_state(ws, session, "llm_error", force=True)
        return
    finally:
        # 任何退出路径都确保当前句 TTS 任务被清理
        if session.tts_task and not session.tts_task.done():
            session.tts_task.cancel()
        from task_service import TaskContext
        TaskContext.clear_current()  # 主 turn 结束，清除后台任务会话钩子

    # ── 空回复兜底：LLM 整轮未产出任何回复句（空流/截断/只调工具未生成文本）→
    #    补一条可见话术，避免后端一直 speaking、前端静默无回复 ──
    if not full_reply and not session.abort_speaking:
        print("[流水线] LLM 空回复，补发兜底话术")
        full_reply = "（我刚才有点卡壳，没接上你的话，能再问我一次吗？）"
        try:
            await ws.send_json({"type": "reply", "text": full_reply, "emotion": "委屈"})
        except Exception:
            pass

    t_llm = time.time() - t0
    t_tts = time.time() - t_tts_start

    # 记录对话历史（旧路径兼容）+ 会话层持久化（新架构：完整可追溯）
    session.history.append({"role": "user", "content": text})
    session.history.append({"role": "assistant", "content": full_reply})
    session.store.add("assistant", full_reply, run_id=run_id)

    await session.emit_event(
        ws, "LLM",
        f"首字 {getattr(_llm, 'first_token_time', 0)}s，首句 {round(t_llm_first_sentence or 0,2)}s，完成 {round(t_llm,2)}s，情绪[{emotion}]",
        duration=round(t_llm, 2),
    )

    # ── 统计本轮耗时 + 发 reply_end / timing（健壮化：任何统计或发送失败，
    #    reply_end 也必须发出，否则前端 ballIsPlaying 状态卡死）──
    try:
        # 打断走 break 后也会到这里：通过 abort_speaking 判断，这种「半完整」轮不进 avg
        include_in_avg = not session.abort_speaking
        current, avg = _build_timing_stats(session, t0, t_llm_first_sentence, t_llm, t_tts, include_in_avg=include_in_avg)
        # 修复：打断时任务取消被内层 except CancelledError 消费，break 后走正常收尾路径。
        # 这里通过 abort_speaking 标记「该轮被打断」，让看板能区分打断轮（部分数据）与完整轮。
        if session.abort_speaking:
            current["interrupted"] = True
            print(f"[流水线] 该轮被打断，timing 标记 interrupted=True")
        # ── 每轮指标后端打印（真实E2E / 打断时长 / ASR / LLM首句 / TTS首包）──
        # 打断时长取最近一次平均：timing_sum["barge_in"]/barge_count（没有打断则为 0）
        barge_ms = 0
        if session.barge_count > 0:
            barge_ms = round((session.timing_sum.get("barge_in", 0) / session.barge_count) * 1000, 0)
        # 真实 E2E：前端上报的「说话结束→第一帧出声」（用户感知）；没有则回退服务端首响并标注
        real_e2e_ms = session.last_real_e2e_ms
        if real_e2e_ms is not None:
            e2e_str = f"{real_e2e_ms:.0f}ms"
        else:
            e2e_str = f"{current.get('e2e', 0) * 1000:.0f}ms(服务端)"
        print(
            f"[指标] 轮#{session.round_id} "
            f"真实E2E={e2e_str} "
            f"打断={barge_ms:.0f}ms "
            f"ASR={current.get('asr', 0) * 1000:.0f}ms "
            f"LLM首句={current.get('llm_first_sentence', 0) * 1000:.0f}ms "
            f"TTS首包={current.get('tts_first_packet', 0) * 1000:.0f}ms"
            + (" [打断轮]" if current.get("interrupted") else ""),
            flush=True,
        )
        await ws.send_json({"type": "reply_end"})
        await ws.send_json({
            "type": "timing",
            "current": current,
            "avg": avg,
            "count": session.timing_count,
            "avg_count": session.avg_count,  # 计入 avg 的轮数（不含打断轮），调试用
        })
    except Exception as _e:
        # 收尾异常不应影响前端状态：诊断打印 + 尽力补发 reply_end
        print(f"[流水线] 收尾统计/发送异常: {type(_e).__name__}: {_e}", file=__import__("sys").stderr)
        try:
            await ws.send_json({"type": "reply_end"})
        except Exception:
            pass

    # 整轮回复已全部下发完毕（逐句 TTS 均完成发送），但**播放可能还没完成**。
    # 契约（MESSAGE_CONTRACT §1.1-3 / §2.1）：speaking 不因 TTS 发送完退出，
    # 必须等前端 `client_playback_done`（真实播放完毕）才切回 listening。
    # 原因：若 TTS 发完立即复位 listening，「播放未完成期间用户插话」会走正常
    # listening 分支（启动 ASR 收话）而不触发打断二次确认 → 打断被延迟到 asr 结束，
    # 前端 ducking 卡住（用户实测问题）。
    # 保持 speaking：播放中插话 → handle_speech_start 走 speaking 分支 → 二次确认 → barge_confirm。
    # 防卡死：speaking_playback_timeout 仍在 arm（滑动重臂在每句完成时刷新）；
    # 前端 VoicePipeline 正常发 client_playback_done（onended + 15s 兜底），不会挂死。
    session.state = "speaking"
    await _sync_backend_state(ws, session, "reply_done", force=False)


async def handle_control_message(ws: WebSocket, session: ConversationSession, text: str):
    """处理控制消息（JSON）"""
    try:
        msg = json.loads(text)
    except json.JSONDecodeError:
        return
    if not isinstance(msg, dict):
        return  # F1 审计修复：非 dict JSON 直接忽略（防属性访问崩溃）

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
        # F4 隔离：只改本会话模式（不再写全局单例）
        requested = msg.get("mode")
        base = getattr(session, "mode", None) or mode_state.get_mode()
        if requested == "toggle":
            new_mode = "work" if base == "chat" else "chat"
        elif requested in ("chat", "work"):
            new_mode = requested
        else:
            new_mode = base
        session.mode = new_mode
        await ws.send_json({"type": "mode_changed", "mode": new_mode})
        print(f"[模式] 手动切换(会话级) → {new_mode}")
    elif msg_type == "get_mode":
        # 查询当前模式（会话级优先）
        cur = getattr(session, "mode", None) or mode_state.get_mode()
        await ws.send_json({"type": "mode_changed", "mode": cur})
    elif msg_type == "speech_start":
        # 前端 VAD 检测到人声（纯事件上报 + 预卷上传，后端做业务决策）
        await session.emit_event(ws, "前端VAD", "检测到人声（speech_start）")
        await handle_speech_start(ws, session, msg.get("preRollBase64"), bool(msg.get("isPlaying")))
    elif msg_type == "vad_cancel":
        # 前端判定上次 speech_start 是误报（onVADMisfire）→ 撤销 ASR 会话 + 重置说话状态。
        # 修复：之前 misfire 只恢复音量，后端已启动的 ASR 会话和 is_user_speaking 会一直卡着，
        #      导致后续用户真实说话被防重入忽略 / 音频喂进噪声会话 → 识别错乱、LLM 被阻塞
        # ── 补充窗口期间不撤销 ASR 会话（改造清单#7 修复）──
        # 窗口期（soft_ended/pending）ASR 会话属于"未完成 turn"：若这里 reset 会话+
        # 复位 is_user_speaking，窗口结束时 finalize 拿到空会话 → 识别文本丢失 →
        # 走"语气词"分支不进 LLM（实测链路缺口：p=0.145 开窗后无回复）。
        # soft_ended：忽略（窗口已挂，照常提交）；pending（续说中）：疑似续说误报，
        # 回退到 soft_ended 并重挂窗口（既有音频保留、turn 保证提交），而不是杀会话。
        if session.supplement_state == "soft_ended":
            print(f"[状态机] vad_cancel 到达但补充窗口进行中（soft_ended），忽略（不撤销 ASR 会话）")
            return
        if session.supplement_state == "pending":
            print(f"[状态机] vad_cancel 到达但处于补充续说（pending），回退 soft_ended + 重挂窗口（保留 ASR 会话）")
            session.supplement_state = "soft_ended"
            _arm_supplement_window(ws, session)
            return
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
        # 前端 VAD 检测到人声结束（改造清单#7：携带该说话段音频供 smart-turn 端点判定）
        import time as _t_end
        session.last_speech_end_recv_ts = _t_end.time()  # 记录后端收到 speech_end 时刻（算网络延迟）
        await session.emit_event(ws, "前端VAD", "人声结束（speech_end）")
        audio_b64 = msg.get("audioB64")
        # F1 审计修复：audioB64 大小上限（8s×16k×2B → b64≈341KB；超限丢弃按 fallback 处理）
        if isinstance(audio_b64, str) and len(audio_b64) > 400_000:
            print(f"[端点] speech_end.audioB64 超限（{len(audio_b64)}B），按无音频处理")
            audio_b64 = None
        await handle_speech_end(ws, session, audio_b64)
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
    elif msg_type == "client_real_e2e":
        # 前端上报真实端到端延迟（VAD onSpeechEnd → 第一帧音频出声，毫秒）
        # 这是用户真正感知的「我说话结束 → 球球开口」，与服务端首响（e2e）语义不同
        ms = msg.get("ms")
        try:
            ms = float(ms)
        except (TypeError, ValueError):
            ms = None
        if ms is not None and ms > 0:
            session.last_real_e2e_ms = round(ms)
            print(f"[指标] 真实E2E（说话结束→首帧出声）={session.last_real_e2e_ms}ms", flush=True)
    elif msg_type == "tts_preheat":
        # 前端唤醒/进对话后立即发：让后端异步预热 TTS 长连接。
        # 把「建连 + task_start」的 5.5s 提前到唤醒时间窗内跑完，首句合成即可直接 task_continue（~2.7s）。
        asyncio.create_task(_preheat_tts(ws, session))
    elif msg_type == "client_barge_in":
        # 前端本地打断：取消 TTS 生成，进入 listening（音频继续流式接收，不丢字）
        print(f"[打断DEBUG] 收到 client_barge_in, state={session.state}, abort={session.abort_speaking}")
        # 打断响应延迟由前端测量（你开口 → 西西闭嘴），这里接收并统计
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
        _snapshot_suspended_reply(session)  # 改造清单#4：打断先快照挂起内容
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
            _on_semantic_partial(ws, session, delta_text)  # 流式语义判定（不等 speech_end）
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

        # 关键修复：把「西西说话期间缓存的音频」喂给 ASR
        # 这段音频是打断信号到达前、用户插话的那段窗口，之前被 continue 丢弃了
        if len(session.speaking_audio_cache) > 0:
            asr.feed(session.session_id, bytes(session.speaking_audio_cache))
            session.is_user_speaking = True
            print(f"[打断] 已喂入 speaking 缓存音频 {len(session.speaking_audio_cache)} 字节")
            session.speaking_audio_cache = bytearray()

        # 进入 listening，但不 reset_episode（保留已喂的 ASR 音频，让用户的话能被识别）
        session.state = "listening"
        session.turn_generation += 1          # 打断：旧 generation 作废（防泄漏）
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
        # 避免西西继续之前的话题（比如继续数数）
        session.history.append({
            "role": "user",
            "content": "(主人刚才打断了西西的上一轮回复，请不要再继续上一个话题，重新听主人接下来的话)",
        })


# ── 后台任务完成语音通知（改造计划最小闭环）────────────────────
# 由 task_service._announce 回调（创建任务时快照的 ws/session）。
_ANNOUNCE_RETRY_TIMES = 15       # 最多等 15×2s=30s
_ANNOUNCE_RETRY_WAIT_S = 2.0


async def _announce_task_done(ws, session, status: str, text: str):
    """任务完成/失败语音通知：不抢播——当前正在播报/生成/收话时不打扰，
    等当前 turn 安全回到 listening 再播；连接断开或超时则静默放弃。"""
    import sys as _sys
    if status == "succeeded":
        message = f"任务完成。{text}"
    elif status == "failed":
        message = "有个后台任务失败了，抱歉哦，你可以让我重新做一次。"
    else:
        return  # cancelled：静默
    print(f"[任务][通知] 等待安全播报时机（status={status}）…", file=_sys.stderr, flush=True)
    for _ in range(_ANNOUNCE_RETRY_TIMES):
        try:
            # 不抢播：listening + 用户未在说话 + 主回复的排队 TTS 已播完
            tt = getattr(session, "tts_task", None)
            tts_busy = tt is not None and not tt.done()
            if session.state == "listening" and not session.is_user_speaking and not tts_busy:
                print(f"[任务][通知] 播报: {message[:60]}", file=_sys.stderr, flush=True)
                await tts.speak_and_send(ws, message, session.session_id, {"emotion": "平静"})
                return
        except Exception:
            return  # WebSocket 已断开等：放弃播报
        await asyncio.sleep(_ANNOUNCE_RETRY_WAIT_S)
    print(f"[任务][通知] 等待超时，放弃播报（不影响任务结果）", file=_sys.stderr, flush=True)


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

    # 后台任务（改造计划最小闭环）：销毁会话级 Worker，取消活动任务并保留记录
    try:
        from task_service import _svc
        _svc().teardown_session(session.session_id)
    except Exception as e:
        print(f"[task] 会话清理异常: {e}")

    # 改造清单#4：会话断开清理挂起上下文
    session.suspended_reply = None
    session.pending_reply_text = ""
    session.is_effective_interrupt = False
    # 改造清单#6：会话断开清理补充窗口状态
    _cancel_supplement_window(session)
    session.supplement_state = None
    session.turn_revision = 0
    # 改造清单#7：会话断开清理尾静音重判
    _cancel_endpoint_rejudge(session)
    session.smart_turn_segment = None
    session.smart_turn_tail = None

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
    # F1 审计修复：默认只绑本机回环（局域网共享受限）；要局域网暴露显式设 HOST=0.0.0.0
    host = os.getenv("HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=port)
