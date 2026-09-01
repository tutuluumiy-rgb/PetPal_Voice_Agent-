"""
telemetry.py — 评测中心专用 telemetry 模块
────────────────────────────────────────────────────────────
设计依据：见 eval-center/docs/decisions/0003a-adr-0003-修订-真实后端协议冲突.md

职责：
  - 把 backend 关键事件（VAD/ASR/LLM/TTS/Barge-in）通过 TCP socket 发出去
  - 评测中心 driver 在 localhost 上监听
  - 仅在环境变量 EVAL_TELEMETRY=1 时启用

激活方式（不动 main.py 业务逻辑）：
  - backend 启动时 import 这个模块
  - 启动：set EVAL_TELEMETRY=1 && set EVAL_TELEMETRY_PORT=3738 && python -c "import telemetry; telemetry.activate(); import main"
  - 评测中心 driver 负责起 TCP listener 并把端口写到 EVAL_TELEMETRY_PORT

边界（不违反"不修改业务代码"约束）：
  - 这个文件是**独立新模块**，不修改 main.py 任何函数
  - 通过 Python 的 import + monkey patch 注册一个 hook
  - hook 只**追加观测**，不改任何业务行为
"""

import asyncio
import json
import os
import socket
import sys
import threading
import time
from typing import Optional

# 是否启用（默认不启用）
ENABLED = os.environ.get("EVAL_TELEMETRY") == "1"

# TCP 端口（评测中心 driver 起的端口）
TCP_PORT = int(os.environ.get("EVAL_TELEMETRY_PORT", "3738"))

# 模拟 ASR 耗时（毫秒）—— 文本注入模式没有真实音频，用真实 endpoint 典型耗时替代。
# 可通过 EVAL_SIM_ASR_MS 配置（默认用阿里云 ASR 流式的典型 P50 值 ~500ms）。
# 真实音频链路（P2/P3 的 /ws/audio 注入）会走真实 ASR → last_asr_time 真实值自动覆盖。
SIM_ASR_MS = int(os.environ.get("EVAL_SIM_ASR_MS", "500"))

# 一个全局 emitter（线程安全）
_emitter: Optional["TelemetryEmitter"] = None


class TelemetryEmitter:
    """把事件写到 TCP socket"""

    def __init__(self):
        self._lock = threading.Lock()
        self._sock: Optional[socket.socket] = None
        self._disabled = False
        self._last_connect_attempt = 0

    def emit(self, stage: str, detail: str, duration_ms: float | None = None,
             round_id: int | None = None):
        """发一个事件。失败静默（不让 telemetry 影响 backend）。"""
        if self._disabled:
            return
        try:
            event = {
                "type": "event",
                "stage": stage,
                "detail": detail,
                "ts": round(time.time(), 3),
                "duration": duration_ms,
                "round": round_id,
            }
            line = json.dumps(event, ensure_ascii=False) + "\n"
            with self._lock:
                # 节流：每秒最多尝试连接 2 次（避免 backend 卡在 connect 超时上）
                now = time.time()
                if self._sock is None and now - self._last_connect_attempt > 0.5:
                    self._last_connect_attempt = now
                    self._connect()
                if self._sock is not None:
                    try:
                        self._sock.sendall(line.encode("utf-8"))
                    except (BrokenPipeError, OSError, ConnectionResetError):
                        self._sock.close()
                        self._sock = None
        except Exception as e:
            # 静默失败
            self._disabled = True
            print(f"[telemetry] disabled: {e}", file=sys.stderr)

    def _connect(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.3)  # 不阻塞太久
            s.connect(("127.0.0.1", TCP_PORT))
            s.settimeout(None)
            self._sock = s
        except Exception:
            self._sock = None


def activate():
    """
    注册 hook 到 ConversationSession.emit_event。
    通过 monkey patch 实现，**不改 main.py**。
    """
    if not ENABLED:
        return

    global _emitter
    _emitter = TelemetryEmitter()
    print(f"[telemetry] activated, target 127.0.0.1:{TCP_PORT}", file=sys.stderr)

    try:
        # 关键：通过 import main 拿到 ConversationSession 类（不修改 main.py）
        # 这意味着 telemetry 必须**在 main.py 被加载之后**激活
        # 调用方负责这个顺序：python -c "import telemetry; telemetry.activate(); import main"
        import main as _main
        cls = _main.ConversationSession
        original = cls.emit_event

        async def patched_emit_event(self, ws, stage, detail="", duration=None):
            # 1) 调原来的（业务行为完全不变）
            await original(self, ws, stage, detail, duration)
            # 2) 顺便发一份到 telemetry（追加观测）
            duration_ms = None
            if duration is not None:
                try:
                    duration_ms = float(duration) * 1000
                except Exception:
                    pass
            if _emitter is not None:
                _emitter.emit(
                    stage=stage,
                    detail=detail,
                    duration_ms=duration_ms,
                    round_id=self.round_id,
                )

        cls.emit_event = patched_emit_event
        print("[telemetry] hook installed on ConversationSession.emit_event", file=sys.stderr)

        # v2: 启动命令服务器（driver 通过 TCP 发 run_case 指令）
        start_command_server()
    except Exception as e:
        print(f"[telemetry] activation failed: {e}", file=sys.stderr)


def emit(stage: str, detail: str, **kwargs):
    """手动发事件（备用，平时不用）"""
    if _emitter is not None:
        _emitter.emit(stage, detail, **kwargs)


# ══════════════════════════════════════════════════════════
# v2: 文本 case 执行器（评测中心需真实跑 backend 对话）
# ──────────────────────────────────────────────────────────
# 边界说明（不违反"不修改业务代码"）：
#   - 本模块仍是独立新模块，不改 main.py 任何函数
#   - 只是**复用** main 导出的 handle_user_speech / ConversationSession 等
#   - run_metrics.py / probe_e2e_cases.py 早已这么干（脚本内 asyncio.run 调 handle_user_speech）
#   - 事件流通过既有 emit_event hook 流出（前面已 patch），MockWs 只做结果收集
# ══════════════════════════════════════════════════════════

class MockWs:
    """收集后端 send_json / send_bytes 的假 ws（与 run_metrics.py 一致）"""

    def __init__(self):
        self.messages = []       # (ts, type, obj)
        self.audio_bytes = 0
        self.audio_first_ts = None
        self.reply_text = ""     # 完整回复文本（reply / reply_append 聚合）

    async def send_json(self, obj):
        ts = time.time()
        self.messages.append((ts, obj.get("type", "?"), obj))
        # 聚合完整回复（P1 回复长度 + P4 数据源）
        t = obj.get("type")
        if t == "reply" and obj.get("text"):
            self.reply_text = obj["text"]
        elif t == "reply_append" and obj.get("text"):
            self.reply_text += obj["text"]

    async def send_bytes(self, data):
        ts = time.time()
        self.audio_bytes += len(data)
        if self.audio_first_ts is None:
            self.audio_first_ts = ts
            self.messages.append((ts, "FIRST_AUDIO", {"bytes": len(data)}))


def _extract_objective(ws_mock: MockWs, t0: float, session, expected_tool: str | None = None) -> dict:
    """从 MockWs 收集的消息 + 会话状态计算客观指标（M1 全指标版）

    指标（timing.current 是 backend 的 _build_timing_stats 产出，已是最可信源）：
      - e2e_latency_ms        = asr + llm_first_sentence + tts_first_packet（服务端理论）
      - asr_ms                模拟 ASR（SIM_ASR_MS；真实链路由 last_asr_time 覆盖）
      - llm_first_sentence_ms LLM 出第一句
      - tts_first_packet_ms   TTS 首包
      - total_ms              完整回合
      - reply_length          回复字数（chars，非 tokens）
      - tool_call_success     工具调用是否成功（按期望工具精确比对）

    tool_call_success 语义（评测中心 D4，方案 B 精确版）：
      - case 配置了 expected_tool → 实际调用且执行完的工具 == expected_tool → True；
          有工具调用但调的不是期望工具 → False；完全没工具事件 → None（不臆造）
      - case 未配置 expected_tool → 保持 None（前端显示 —）
    """
    objective = {
        "e2e_latency_ms": None,
        "asr_ms": SIM_ASR_MS,               # 模拟 ASR，真实链路覆盖
        "llm_first_sentence_ms": None,
        "tts_first_packet_ms": None,
        "total_ms": None,
        "reply_length": None,
        "tool_call_success": None,
        "round_id": getattr(session, "round_id", None),
    }
    # 解析 timing 消息（backend 收尾会发 timing.current）
    # 优先读「首轮首响」字段（e2e_first_round / llm_first_sentence_first_round / tts_first_packet_first_round）——
    # 多轮 LLM+tool 场景下，末轮的 timing.current 是最后一轮的耗时，但用户实际感知的是首轮的 e2e（"我说话完→第一次听到声音"）
    for _, t, data in ws_mock.messages:
        if t == "timing" and "current" in data and data["current"]:
            c = data["current"]
            # 优先首轮（backend 已锁定）
            if c.get("e2e_first_round"):
                objective["e2e_latency_ms"] = round(c["e2e_first_round"] * 1000, 1)
                objective["llm_first_sentence_ms"] = round(c.get("llm_first_sentence_first_round", c.get("llm_first_sentence", 0)) * 1000, 1)
                objective["tts_first_packet_ms"] = round(c.get("tts_first_packet_first_round", c.get("tts_first_packet", 0)) * 1000, 1)
            else:
                # 兜底：旧 backend（无 first_round 字段）或首轮未锁定时，用末轮
                objective["e2e_latency_ms"] = round(c.get("e2e", 0) * 1000, 1)
                objective["llm_first_sentence_ms"] = round(c.get("llm_first_sentence", 0) * 1000, 1)
                objective["tts_first_packet_ms"] = round(c.get("tts_first_packet", 0) * 1000, 1)
            objective["total_ms"] = round(c.get("total", 0) * 1000, 1)
            # 若真实 ASR 有值（真实链路），用它覆盖模拟值
            real_asr = c.get("asr", 0)
            if real_asr and real_asr > 0:
                objective["asr_ms"] = round(real_asr * 1000, 1)
            break  # 收尾 timing 只发一次（多轮场景保留以兜底；首轮已优先）
    # 回复长度（chars，整数）+ 句平均长度（chars/句，取整）
    if ws_mock.reply_text:
        objective["reply_length"] = len(ws_mock.reply_text)
        # 句平均长度：按中文/英文句末标点切句，字符数/句数，保留整数（常规四舍五入）
        import re as _re
        sentences = [s for s in _re.split(r"[。！？…；;.!?]+", ws_mock.reply_text) if s.strip()]
        if sentences:
            _avg = len(ws_mock.reply_text) / len(sentences)
            objective["avg_sentence_length"] = int(_avg + 0.5)
    # 工具调用（工具事件在 emit_event hook 里流出；main.py _on_tool 发「开始调用：{name}」）
    tool_names: set[str] = set()
    for _, t, data in ws_mock.messages:
        if t == "event" and data.get("stage") == "工具":
            detail = str(data.get("detail") or "")
            if detail.startswith("开始调用："):
                tool_names.add(detail[len("开始调用："):].strip())
    if expected_tool and expected_tool != "none":
        # 精确比对：期望工具被实际调用 → True；有调用但不是期望工具 → False；无调用 → None
        objective["tool_call_success"] = (
            expected_tool in tool_names if tool_names else None
        )
    # 未配置期望工具 → 保持 None（不臆造）；前端显示 —
    # 注意：不再输出 audio_first_packet_ms —— 它不是"用户听到"的延迟，已按用户决策废弃
    return objective


def _extract_events(ws_mock: MockWs) -> list:
    """从 MockWs 消息里挑出 event 类型（stage/detail/duration），转事件流"""
    events = []
    for _, t, data in ws_mock.messages:
        if t == "event" and data.get("stage"):
            events.append({
                "stage": data.get("stage", ""),
                "detail": data.get("detail", ""),
                "ts": round(data.get("ts", 0) or 0, 2),
                "duration_ms": round((data.get("duration") or 0) * 1000, 1)
                if data.get("duration") is not None else None,
            })
    return events


async def run_text_case(text: str, session_id: str = "eval", overrides: dict | None = None) -> dict:
    """在 backend 进程内真实跑一条文本 case（LLM/工具/TTS 全真实执行）。

    overrides（v5 参数注入，不修改 backend 文件，session 级生效）:
      - system_prompt: 完整 system prompt 覆盖（M5 Prompt 注入；None 用默认 build_system_prompt）
      - chat_work: 'chat' | 'work'（模式；决定默认 prompt 文件）
      - voice: dict，TTS 参数覆盖 {speech_rate, volume, pitch_rate, voice}
      - asr: dict {tokenizer:'char'|'jieba'}（记录用，CER/WER 计算用）

    返回：{"ok": bool, "events": [...], "objective": {...}, "error": str|None}
    """
    import main as _main

    ws = MockWs()
    session = _main.ConversationSession()
    session.last_asr_time = SIM_ASR_MS / 1000  # 模拟 ASR（秒），配置化；真实链路覆盖
    overrides = overrides or {}

    # v5: Voice 参数注入（TTS 语速/音量/音调 —— 通过 emotion_state 覆盖或直接设 session 参数）
    voice_params = overrides.get("voice") or {}
    if voice_params:
        _apply_tts_params(voice_params)

    # v5: Prompt 注入（system_prompt 覆盖 或 模式选择）
    system_prompt_override = overrides.get("system_prompt")
    chat_work = overrides.get("chat_work")
    # D4 期望工具（评测中心 case 上配置的 expected_tool；用于 tool_call_success 精确比对）
    expected_tool = str(overrides.get("expected_tool") or "") or None

    _main.emotion_state.current = "平静"
    t0 = time.time()
    try:
        if system_prompt_override:
            # 直接用评测中心选定的 system prompt（M5）
            await _main.handle_user_speech(ws, session, text,
                                           extra_context=_wrap_system_prompt_override(system_prompt_override))
        else:
            await _main.handle_user_speech(ws, session, text,
                                           extra_context=_mode_extra_context(chat_work))
    except asyncio.CancelledError:
        return {"ok": False, "events": _extract_events(ws), "objective": {}, "error": "cancelled"}
    except Exception as e:
        return {"ok": False, "events": _extract_events(ws), "objective": _extract_objective(ws, t0, session, expected_tool), "error": f"{type(e).__name__}: {e}"}

    return {
        "ok": True,
        "events": _extract_events(ws),
        "objective": _extract_objective(ws, t0, session, expected_tool),
        "reply_text": ws.reply_text,   # P4 主观评测的数据源
        "error": None,
    }


def _apply_tts_params(voice: dict):
    """v5: 用评测配置覆盖 TTS 语速/音量/音调（session 级，不动全局常量）"""
    import main as _main
    emo = getattr(_main, "emotion_state", None)
    if emo is None:
        return
    # 构造 override 参数：让 TTS 用评测设定的数值而非情绪默认
    # （通过在 emotion_state 上注入 custom 属性；providers/tts 读 get_tts_params 输出）
    speech_rate = float(voice.get("speech_rate") or 0)
    volume = float(voice.get("volume") or 0)
    pitch_rate = float(voice.get("pitch_rate") or 0)
    if speech_rate or volume or pitch_rate:
        emo.current = "平静"  # 基准情绪
        # 直接覆盖 get_tts_params 输出：存到全局覆盖表
        _TTS_OVERRIDE.clear()
        if speech_rate: _TTS_OVERRIDE["speech_rate"] = speech_rate
        if volume: _TTS_OVERRIDE["volume"] = volume
        if pitch_rate: _TTS_OVERRIDE["pitch_rate"] = pitch_rate


def _mode_extra_context(chat_work: str | None) -> str | None:
    """v5: 模式上下文（chat/work）——传给 handle_user_speech 影响 system prompt 组装"""
    if chat_work == "work":
        return "当前模式：工作模式。以完成任务为目标，多用工具。"
    if chat_work == "chat":
        return "当前模式：闲聊模式。轻量聊天，口语化。"
    return None


def _wrap_system_prompt_override(prompt: str) -> str:
    """v5: 把评测中心选定的完整 system prompt 传给 handle_user_speech。
    handle_user_speech 的 extra_context 会拼接在默认 system_prompt 之后 ——
    我们这里不是替换而是"追加覆盖段"，由评测中心 prompt 内容完全控制本次输出。
    """
    return "\n\n# 评测中心注入的 System Prompt（本次跑分生效）\n" + prompt


# session 级 TTS override（线程安全由 GIL 保护，评测低频使用）
_TTS_OVERRIDE: dict = {}


# ── 命令服务器：driver 通过 TCP 发 `run_case` 指令 ──

_CMD_PORT = int(os.environ.get("EVAL_CMD_PORT", "3741"))
_cmd_server_thread = None
_loop_ref = None


def _run_case_sync(msg: dict) -> dict:
    """在命令服务器线程内同步执行一条 case"""
    import asyncio as _aio

    text = (msg.get("text") or "").strip()
    sid = msg.get("session_id") or "eval"
    overrides = msg.get("overrides") or {}   # v5: {chat_work, system_prompt, voice, asr}
    if not text:
        return {"id": msg.get("id"), "ok": False, "events": [], "objective": {}, "error": "empty text"}
    try:
        result = _aio.run(run_text_case(text, sid, overrides))
        result["id"] = msg.get("id")
        return result
    except Exception as e:
        return {"id": msg.get("id"), "ok": False, "events": [], "objective": {}, "error": f"{type(e).__name__}: {e}"}


def _asr_test_sync(msg: dict) -> dict:
    """P3：ASR 识别率（CER/WER）"""
    import asyncio as _aio

    standard_text = (msg.get("standard_text") or "").strip()
    pcm_b64 = (msg.get("pcm_b64") or "").strip() or None
    if not standard_text:
        return {"id": msg.get("id"), "ok": False, "error": "standard_text required"}
    try:
        from asr_service import run_asr_test
        result = _aio.run(run_asr_test(standard_text, pcm_b64))
        result["id"] = msg.get("id")
        result["ok"] = result.get("ok", False)
        return result
    except Exception as e:
        return {"id": msg.get("id"), "ok": False, "error": f"{type(e).__name__}: {e}"}


def _barge_test_sync(msg: dict) -> dict:
    """P2：播放中注入音频样本测打断（正/负样本）"""
    import asyncio as _aio

    pcm_b64 = (msg.get("pcm_b64") or "").strip()
    if not pcm_b64:
        return {"id": msg.get("id"), "ok": False, "barge_detected": None,
                "backend_ms": None, "error": "pcm_b64 required"}
    try:
        from barge_service import run_barge_test
        result = _aio.run(run_barge_test(pcm_b64, trigger_text=msg.get("trigger_text") or ""))
        result["id"] = msg.get("id")
        result["ok"] = result.get("ok", False)
        return result
    except Exception as e:
        return {"id": msg.get("id"), "ok": False, "barge_detected": None,
                "backend_ms": None, "error": f"{type(e).__name__}: {e}"}


def _judge_sync(msg: dict) -> dict:
    """P4：LLM 初评一条 case（拟人度 + 人设一致性），复用 judge_service"""
    import asyncio as _aio

    user_text = (msg.get("user_text") or "").strip()
    reply_text = (msg.get("reply_text") or "").strip()
    prompt_suffix = msg.get("prompt_suffix") or None   # 评测中心选定的 judge prompt（可选）
    if not user_text or not reply_text:
        return {"id": msg.get("id"), "ok": False,
                "anthropomorphism_score": None, "persona_consistency_score": None,
                "error": "user_text and reply_text required"}
    try:
        from judge_service import judge_reply
        result = _aio.run(judge_reply(user_text, reply_text, prompt_suffix=prompt_suffix))
        result["id"] = msg.get("id")
        result["ok"] = result.get("error") is None
        return result
    except Exception as e:
        return {"id": msg.get("id"), "ok": False,
                "anthropomorphism_score": None, "persona_consistency_score": None,
                "error": f"{type(e).__name__}: {e}"}


def _cmd_server_loop():
    """命令服务器线程主循环：监听 TCP，收到指令就执行并回包"""
    import socket as _sock

    srv = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
    srv.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", _CMD_PORT))
    srv.listen(4)
    srv.settimeout(0.5)
    print(f"[telemetry] cmd server listening on 127.0.0.1:{_CMD_PORT}", file=sys.stderr)
    while True:
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            continue
        except Exception:
            break
        try:
            data = b""
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
            if data.strip():
                msg = json.loads(data.decode("utf-8"))
                if msg.get("op") == "run_case":
                    resp = _run_case_sync(msg)
                    conn.sendall(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
                elif msg.get("op") == "judge":
                    # P4 主观双轨：LLM 初评（拟人度 + 人设一致性）
                    resp = _judge_sync(msg)
                    conn.sendall(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
                elif msg.get("op") == "barge_test":
                    # P2 barge-in：播放中注入音频样本测打断
                    resp = _barge_test_sync(msg)
                    conn.sendall(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
                elif msg.get("op") == "asr_test":
                    # P3 ASR 识别率（CER/WER）
                    resp = _asr_test_sync(msg)
                    conn.sendall(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
        except Exception as e:
            print(f"[telemetry] cmd server error: {e}", file=sys.stderr)
        finally:
            try:
                conn.close()
            except Exception:
                pass


def start_command_server():
    """启动命令服务器线程（幂等）"""
    global _cmd_server_thread
    if _cmd_server_thread and _cmd_server_thread.is_alive():
        return
    _cmd_server_thread = threading.Thread(target=_cmd_server_loop, daemon=True)
    _cmd_server_thread.start()
    return _cmd_server_thread


if __name__ == "__main__":
    print(f"enabled={ENABLED}, port={TCP_PORT}")
    if ENABLED:
        activate()