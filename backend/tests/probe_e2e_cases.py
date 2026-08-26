"""端到端 E2E 测试：跑 10 条用例，统计 LLM→TTS 链路时延

注意：
- 跳过 ASR（直接给定文本，避免依赖真人录音）
- 跳过 WebSocket 协议层（用 Mock 收集消息）
- 直接调 handle_user_speech + emit_event，拿到真实 timing/event 消息
- 模拟 ASR 耗时为典型值 0.5s（last_asr_time），不影响 LLM/TTS 真实测量

用法：
    cd backend
    python tests/probe_e2e_cases.py
"""
import asyncio
import base64
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ".")

# 强制 ws transport
os.environ["MINIMAX_TRANSPORT"] = "ws"

from main import (  # noqa: E402
    handle_user_speech,
    ConversationSession,
    tts,
    llm,
    emotion_state,
    _preheat_tts,
)
from providers.minimax_tts import MiniMaxTTS  # noqa: E402


CASES = [
    "我今天真的不想上班",
    "你觉得我要不要点外卖",
    "中午了，吃啥啊？火锅还是烧烤，纠结死了都",
    "为什么我明明睡了很久还是觉得累",
    "你觉得做语音产品最难的是哪一块？",
    "人到底为什么要工作呢",
    "用大白话解释下什么是API？",
    "为什么手机会卡？",
    "你别讲术语，就跟我说为什么 Streaming 会让 TTS 不自然。",
    "大家为什么讨厌开会？",
    "聊聊人工智能呗。",
]


class MockWs:
    """收集后端 send_json / send_bytes 的 mock"""

    def __init__(self):
        self.messages = []  # [(timestamp, type_or_'binary', data)]
        self.audio_bytes = 0
        self.audio_first_ts = None  # 第一帧音频时间

    async def send_json(self, obj):
        ts = time.time()
        self.messages.append((ts, obj.get("type", "?"), obj))

    async def send_bytes(self, data):
        ts = time.time()
        self.audio_bytes += len(data)
        if self.audio_first_ts is None:
            self.audio_first_ts = ts
            self.messages.append((ts, "FIRST_AUDIO", {"bytes": len(data)}))


async def run_one(ws_mock: MockWs, session: ConversationSession, text: str, idx: int, total: int):
    """跑一轮对话，统计指标"""
    # 模拟 ASR 耗时（前端的真实 ASR 不通过 handle_user_speech）
    # 我们假设 ASR 是典型值 0.5s（Qwen ASR 实测 0.3~0.7s）
    t_asr = 0.5
    session.last_asr_time = t_asr

    # 重置情绪状态（每轮独立）
    emotion_state.current = "平静"

    t0 = time.time()
    await handle_user_speech(ws_mock, session, text)
    t_done = time.time()

    # 从 messages 中提取 timing 和 reply
    timing_msg = None
    reply_text = ""
    for ts, mtype, data in ws_mock.messages:
        if mtype == "timing":
            timing_msg = data
        elif mtype == "reply":
            reply_text += data.get("text", "")
        elif mtype == "reply_append":
            reply_text += data.get("text", "")

    return {
        "idx": idx + 1,
        "total": total,
        "text": text,
        "reply": reply_text[:60] + ("..." if len(reply_text) > 60 else ""),
        "duration_s": round(t_done - t0, 2),
        "timing": timing_msg,
        "asr_audio_bytes": ws_mock.audio_bytes,
        "first_audio_latency": round(ws_mock.audio_first_ts - t0, 3) if ws_mock.audio_first_ts else None,
    }


async def main():
    # 检查 transport
    if not isinstance(tts, MiniMaxTTS):
        print(f"[!] 当前 TTS 是 {type(tts).__name__}，不是 MiniMaxTTS，请设置 TTS_PROVIDER=minimax", flush=True)
        return

    print(f"[初始化] TTS={type(tts).__name__}, transport={tts.transport}", flush=True)

    # 1. 预热（模拟前端唤醒命中后发 tts_preheat）
    print("\n[预热] 调用 tts.preheat() ...", flush=True)
    ws_preheat = MockWs()
    session_preheat = ConversationSession()
    t_pre = time.time()
    try:
        await _preheat_tts(ws_preheat, session_preheat)
        print(f"[预热] 完成（{time.time()-t_pre:.2f}s）", flush=True)
    except Exception as e:
        print(f"[预热] 失败：{e}", flush=True)

    # 2. 跑 10 条用例
    results = []
    for i, text in enumerate(CASES):
        ws_mock = MockWs()
        session = ConversationSession()
        try:
            r = await run_one(ws_mock, session, text, i, len(CASES))
            results.append(r)
            # 简明输出
            t = r["timing"]
            if t and "current" in t:
                c = t["current"]
                e2e = c.get("e2e", 0)
                asr = c.get("asr", 0)
                llm_ft = c.get("llm_first_token", 0)
                llm_fs = c.get("llm_first_sentence", 0)
                tts_fp = c.get("tts_first_packet", 0)
                total = c.get("total", 0)
                print(f"\n[{i+1}/{len(CASES)}] {text[:30]}", flush=True)
                print(f"  → 回复: {r['reply']}", flush=True)
                # 关键指标显示精度：ms 整数 + 0~50ms 的小数（避免 round 到 0 误导）
                print(f"  → 整轮 {r['duration_s']}s | ASR={asr*1000:.0f}ms LLM首句={llm_fs*1000:.0f}ms TTS首包={tts_fp*1000:.0f}ms E2E(服务端)={e2e*1000:.0f}ms Total={total*1000:.0f}ms | 音频 {r['asr_audio_bytes']/1024:.1f}KB | 首音频 {r['first_audio_latency']}s", flush=True)
            else:
                print(f"[{i+1}] 无 timing 数据（可能 LLM 拒答）", flush=True)
        except Exception as e:
            print(f"[{i+1}] 异常: {type(e).__name__}: {e}", flush=True)

    # 3. 汇总
    print("\n" + "=" * 80, flush=True)
    print("汇总统计（仅服务端 E2E 链路：LLM→TTS）", flush=True)
    print("=" * 80, flush=True)
    valid = [r for r in results if r["timing"] and "current" in r["timing"]]
    if not valid:
        print("无有效数据", flush=True)
        return
    n = len(valid)
    keys = ["asr", "llm_first_token", "llm_first_sentence", "tts_first_packet", "e2e", "total"]
    labels = {
        "asr": "ASR(mock)",
        "llm_first_token": "LLM首字",
        "llm_first_sentence": "LLM首句",
        "tts_first_packet": "TTS首包",
        "e2e": "E2E(服务端)",
        "total": "Total",
    }
    print(f"\n有效用例: {n}/{len(results)}\n")
    print(f"{'指标':<16} {'平均(ms)':>10} {'最小(ms)':>10} {'最大(ms)':>10} {'中位(ms)':>10}", flush=True)
    print("-" * 60, flush=True)
    for k in keys:
        vals_ms = [r["timing"]["current"].get(k, 0) * 1000 for r in valid]
        avg = sum(vals_ms) / n
        mn = min(vals_ms)
        mx = max(vals_ms)
        md = sorted(vals_ms)[n // 2]
        print(f"{labels[k]:<16} {avg:>10.0f} {mn:>10.0f} {mx:>10.0f} {md:>10.0f}", flush=True)

    # 真实 E2E（用户说完→第一帧）—— 这个是 probe 里 from t0 到 first_audio 的时间
    real_e2e_vals = [r["first_audio_latency"] for r in valid if r["first_audio_latency"] is not None]
    if real_e2e_vals:
        avg = sum(real_e2e_vals) / len(real_e2e_vals) * 1000
        print(f"\n真实E2E(mock+LLM+TTS 首帧): {avg:.0f}ms (min {min(real_e2e_vals)*1000:.0f} / max {max(real_e2e_vals)*1000:.0f})", flush=True)
        print("  (含 mock ASR 0.5s + LLM首句 + TTS首包 + WS推送延迟)", flush=True)

    # 4. 清理
    if isinstance(tts, MiniMaxTTS):
        try:
            await tts._close_ws()
        except Exception:
            pass
    print("\n[done]", flush=True)


if __name__ == "__main__":
    asyncio.run(main())