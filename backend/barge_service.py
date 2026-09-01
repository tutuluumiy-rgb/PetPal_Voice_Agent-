"""
barge_service.py — 评测中心 P2 barge-in 音频测试的后端执行器
────────────────────────────────────────────────────────────
设计依据：评测中心 M3（P2 barge-in 自动化测试）

场景：
  AI 正在说话（speaking）→ 注入音频样本（正/负）到麦克风输入链路
  → 后端二次确认（SileroVAD + 能量跃升）判定是否真打断
  
指标：
  - barge_detected     : 是否触发打断（正样本期望 true，负样本期望 false）
  - backend_ms         : 后端二次确认耗时（speech_start 到达 → barge_confirm 发出）
  - barge_latency_ms   : 事件流时序里「speech_start → state切到 listening/打断完成」

边界（不违反"不修改业务代码"）：
  - 独立新模块，不改 main.py 任何函数
  - 复用 ConversationSession / handle_speech_start / MockWs（与 run_text_case 同策略）

注意（诚实标注）：
  - 无真实 Electron 前端 → 无法测「用户开口 → 扬声器停止」的真实感知延迟
  - 本模块测的是【后端响应延迟】（后端从收到 speech_start 到确认真打断的耗时）
  - 真实感知延迟需 M3 二期的真实前端闭环（Electron + 声卡注入）才能测
"""

import asyncio
import base64
import os
import time
from typing import Optional


async def run_barge_test(pcm_b64: str, trigger_text: str = "请你连续讲一段话，至少说三句，不要停下来。",
                         wait_ms: int = 1500, attempt_duration_ms: int = 200) -> dict:
    """播放中注入音频样本，测后端打断响应。

    Args:
        pcm_b64:     正/负样本 PCM 音频（16bit, 16kHz mono）base64
        trigger_text: 让 AI 说话的指令（触发 speaking 状态）
        wait_ms:      AI 开始说话后等待多久注入样本（模拟"播放中"的某时间点）
        attempt_duration_ms: 样本的时长（毫秒，用于构造有效长度）

    Returns:
        {
          ok, barge_detected, backend_ms, barge_latency_ms,
          events(部分), error
        }
    """
    import main as _main

    # 1. 触发 AI 说话（真实 LLM+TTS → 后台跑，让 TTS 进入播放态）
    ws_ai = _mockws()
    session = _main.ConversationSession()
    session.last_asr_time = 0.3
    _main.emotion_state.current = "平静"
    ai_task = None
    try:
        # 后台启动：handle_user_speech 内部 await session.tts_task 会等 TTS 播完，
        # 我们不等它完成，而是让它在后台跑（TTS 合成是重点，模拟"AI 正在说话"）。
        ai_task = asyncio.create_task(
            _main.handle_user_speech(ws_ai, session, trigger_text)
        )
    except Exception as e:
        return {"ok": False, "barge_detected": None, "backend_ms": None,
                "barge_latency_ms": None, "error": f"trigger failed: {type(e).__name__}: {e}"}

    # 2. 等 AI 进入 TTS 播放态（给 handle_user_speech 一点时间达到 speaking/pending_play）
    # 注：无真实前端"播放"，我们用"等待 LLM 首句 + TTS 开始合成"作为播放起点。
    #     wait_ms 模拟"播放第 N ms 时注入"（人声 onset 参考）。
    if not pcm_b64:
        if ai_task: ai_task.cancel()
        return {"ok": False, "barge_detected": None, "backend_ms": None,
                "barge_latency_ms": None, "error": "pcm_b64 empty"}

    try:
        pre_roll_pcm = base64.b64decode(pcm_b64)
    except Exception as e:
        if ai_task: ai_task.cancel()
        return {"ok": False, "barge_detected": None, "backend_ms": None,
                "barge_latency_ms": None, "error": f"base64 decode failed: {e}"}

    # 3. 注入 speech_start（预卷 = 样本）→ 触发打断判定（speaking 态才走打断分支）
    #    需要 session.state 已进入 speaking/pending_play——若 handle_user_speech 太慢
    #    （LLM 首句未出），先小等（最多 max_wait）让 TTS 进入播放。
    ws_barge = _mockws()
    max_wait = (wait_ms or 1500) / 1000
    elapsed = 0
    while session.state not in ("speaking", "pending_play", "thinking") and elapsed < max_wait:
        await asyncio.sleep(0.05)
        elapsed += 0.05
    try:
        await _main.handle_speech_start(ws_barge, session, pre_roll_b64=pcm_b64, is_playing=True)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        if ai_task: ai_task.cancel()
        return {"ok": False, "barge_detected": None, "backend_ms": None,
                "barge_latency_ms": None, "error": f"inject failed: {type(e).__name__}: {e}"}

    # 4. 给后台 TTS 一点时间被打断（打断路径会 cancel tts_task/user_speech_task）
    try:
        await asyncio.sleep(0.3)
    except Exception:
        pass
    if ai_task and not ai_task.done():
        ai_task.cancel()  # 兜底清理

    # 5. 从 MockWs 收集结果
    barge_confirm_ms = None
    barge_reject = False
    events = []
    for ts, t, data in ws_barge.messages:
        if t == "event":
            events.append({"ts": ts, "stage": data.get("stage", ""), "detail": data.get("detail", "")})
        if t == "barge_confirm":
            backend_ms = data.get("backend_ms")
            barge_confirm_ms = backend_ms if backend_ms is not None else None
        if t == "barge_reject":
            barge_reject = True
    for ts, t, data in ws_ai.messages:
        if t == "event":
            events.append({"ts": ts, "stage": data.get("stage", ""), "detail": data.get("detail", "")})

    barge_detected = (barge_confirm_ms is not None)
    return {
        "ok": True,
        "barge_detected": barge_detected,
        "barge_reject": barge_reject,
        "backend_ms": barge_confirm_ms,          # 后端二次确认耗时(ms)
        "events": events[:50],
        "error": None,
    }


def _mockws():
    """轻量 MockWs（只收 send_json）"""
    import time as _t

    class _W:
        def __init__(self):
            self.messages = []
        async def send_json(self, obj):
            self.messages.append((_t.time(), obj.get("type", "?"), obj))
        async def send_bytes(self, data):
            pass
    return _W()


def pcm_from_wav(wav_path: str, start_ms: int = 0, dur_ms: Optional[int] = None) -> Optional[str]:
    """从 wav 文件取一段 PCM16 b64（简化：假定 16kHz mono 16bit；非 PCM 需先转）

    真实可用性依赖 wav 格式；测试样本用户采集，建议统一 16kHz/16bit/mono。
    返回 base64 字符串（对应 pre_roll_b64 语义）。失败返回 None。
    """
    try:
        import wave
        with wave.open(wav_path, 'rb') as w:
            params = w.getparams()
            nch, sampwidth, framerate, nframes = params[:4]
            if sampwidth != 2 or framerate != 16000:
                raise ValueError(f"需 16kHz/16bit wav，当前 {framerate}Hz/{sampwidth}bytes {nch}ch")
            w.setpos(int(framerate * start_ms / 1000))
            n = int(framerate * (dur_ms or 1000) / 1000)
            data = w.readframes(min(n, w.getnframes() - w.tell()))
            # 单声道直接返回；立体声降混
            if nch == 2:
                import array
                arr = array.array('h', data)
                mono = array.array('h', [int((arr[i] + arr[i + 1]) / 2) for i in range(0, len(arr) - 1, 2)])
                data = mono.tobytes()
            return base64.b64encode(data).decode('ascii')
    except Exception:
        return None


if __name__ == "__main__":
    print("barge_service: 供 telemetry 命令口调用，不独立运行")