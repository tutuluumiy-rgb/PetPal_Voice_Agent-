"""端到端 E2E 测试（Aliyun Qwen3-TTS-Instruct-Flash-Realtime）：
直接调 handle_user_speech 跑 3 轮，统计真实 E2E / ASR / LLM首句 / TTS首包

跳过 ASR（给定文本），跳过 preheat（AliyunTTS 无此能力，属正常）。
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ".")

os.environ["TTS_PROVIDER"] = "ali"
os.environ["TTS_MODEL"] = "qwen3-tts-instruct-flash-realtime"

from main import (  # noqa: E402
    handle_user_speech,
    ConversationSession,
    tts,
    emotion_state,
)

CASES = [
    "我今天真的不想上班",
    "用大白话解释下什么是API？",
    "聊聊人工智能呗",
]


class MockWs:
    def __init__(self):
        self.messages = []
        self.audio_first_ts = None
        self.audio_bytes = 0

    async def send_json(self, obj):
        self.messages.append((time.time(), obj.get("type", "?"), obj))

    async def send_bytes(self, data):
        self.audio_bytes += len(data)
        if self.audio_first_ts is None:
            self.audio_first_ts = time.time()


async def run_one(text):
    ws = MockWs()
    session = ConversationSession()
    session.last_asr_time = 0.5  # mock ASR
    emotion_state.current = "平静"
    t0 = time.time()
    await handle_user_speech(ws, session, text)
    td = time.time() - t0

    timing_msgs = [d for _, m, d in ws.messages if m == "timing"]
    if not timing_msgs:
        print(f"  [!] 无 timing（LLM 拒答？）", flush=True)
        return None
    cur = timing_msgs[-1].get("current", {})
    avg = timing_msgs[-1].get("avg", {})
    real_e2e = (ws.audio_first_ts - t0) * 1000 if ws.audio_first_ts else None
    print(
        f"  整轮={td:.2f}s | 真实E2E(物理)={real_e2e:.0f}ms | "
        f"服务端E2E={cur.get('e2e',0)*1000:.0f}ms | ASR={cur.get('asr',0)*1000:.0f}ms | "
        f"LLM首句={cur.get('llm_first_sentence',0)*1000:.0f}ms | TTS首包={cur.get('tts_first_packet',0)*1000:.0f}ms | "
        f"音频={ws.audio_bytes/1024:.0f}KB",
        flush=True,
    )
    return {"cur": cur, "real_e2e": real_e2e}


async def main():
    print(f"TTS={type(tts).__name__}", flush=True)
    results = []
    for i, text in enumerate(CASES):
        print(f"--- 轮{i+1}: {text} ---", flush=True)
        r = await run_one(text)
        if r:
            results.append(r)
        await asyncio.sleep(0.3)

    if results:
        n = len(results)
        avg_real = sum(r["real_e2e"] for r in results if r["real_e2e"]) / n
        avg_tts = sum(r["cur"].get("tts_first_packet", 0) * 1000 for r in results) / n
        avg_llm = sum(r["cur"].get("llm_first_sentence", 0) * 1000 for r in results) / n
        print(f"\n=== 平均 ===", flush=True)
        print(f"真实E2E(物理 说话结束→首帧) ≈ {avg_real:.0f}ms | 服务端E2E avg ≈ {sum(r['cur'].get('e2e',0)*1000 for r in results)/n:.0f}ms | TTS首包 ≈ {avg_tts:.0f}ms | LLM首句 ≈ {avg_llm:.0f}ms", flush=True)
    print("\n[done]", flush=True)


if __name__ == "__main__":
    asyncio.run(main())