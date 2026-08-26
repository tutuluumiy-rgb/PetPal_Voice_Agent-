"""诊断 testboard 看板：直接复用 main.py 的 LLM/TTS，跑 5 轮，
抓 timing 消息原文，看 avg 是否累加 + interrupted 标记是否正确。

不通过 WS（避免 ASR 噪声问题），直接调 handle_user_speech 的内部路径。

关键：每次跑前重置 ConversationSession（独立测试）。
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ".")

# 强制 ws transport（测试真实路径）
os.environ["MINIMAX_TRANSPORT"] = "ws"

from main import (  # noqa: E402
    handle_user_speech,
    ConversationSession,
    tts,
    llm,
    emotion_state,
    _preheat_tts,
)


CASES = [
    "我今天真的不想上班",
    "中午了，吃啥啊",
    "为什么手机会卡",
    "用大白话解释API",
    "聊聊人工智能呗",
]


class MockWs:
    """捕获后端所有消息 + 第一帧音频时间"""
    def __init__(self):
        self.messages = []
        self.audio_bytes = 0
        self.audio_first_ts = None

    async def send_json(self, obj):
        ts = time.time()
        self.messages.append((ts, obj.get("type", "?"), obj))

    async def send_bytes(self, data):
        ts = time.time()
        self.audio_bytes += len(data)
        if self.audio_first_ts is None:
            self.audio_first_ts = ts





async def main():
    if not isinstance(tts, type(tts)):  # 兼容
        pass

    print(f"TTS={ type(tts).__name__ }, transport={tts.transport}", flush=True)

    # 预热
    print("\n[预热] 调用 tts.preheat() ...", flush=True)
    try:
        await tts.preheat()
        print(f"  预热完成", flush=True)
    except Exception as e:
        print(f"  预热失败: {e}", flush=True)

    # 跑 5 轮 —— 复用同一个 session（模拟真实 WS 连接的同一会话）
    # 注意：每次跑完一轮需要等 handle_user_speech 完成 + 后端状态复位回 listening
    # 如果 ASR 噪音过滤掉 text 会跳过 handle_user_speech（这里不会，因为 mock text）
    print("\n[注意] 复用同一个 session.timing_count / timing_sum —— 与真实 WS 行为一致", flush=True)
    ws_mock = MockWs()
    session = ConversationSession()
    session.last_asr_time = 0.5
    results = []
    for i, text in enumerate(CASES):
        print(f"\n=== 轮 {i+1}/5：{text} ===", flush=True)
        t0 = time.time()
        await handle_user_speech(ws_mock, session, text)
        t_done = time.time()

        # 找本轮 timing（每次 run_one 都会发一条）
        timing_msgs = [(ts, mtype, data) for ts, mtype, data in ws_mock.messages if mtype == "timing"]
        if not timing_msgs:
            print(f"  [!] 没收到 timing", flush=True)
            continue
        last_data = timing_msgs[-1][2]
        cur = last_data.get("current", {})
        avg = last_data.get("avg", {})
        cnt = last_data.get("count", 0)
        print(f"  count={cnt}", flush=True)
        print(f"  current.e2e={cur.get('e2e')} avg.e2e={avg.get('e2e')}", flush=True)
        results.append({"count": cnt, "current": cur, "avg": avg})
        await asyncio.sleep(0.3)

    # 汇总
    print("\n=== 汇总 ===", flush=True)
    print(f"总轮次: {len(results)}", flush=True)
    if results:
        last = results[-1]
        print(f"\n最后一条 timing：", flush=True)
        print(f"  count={last['count']}", flush=True)
        print(f"  current.e2e={last['current'].get('e2e')}", flush=True)
        print(f"  avg.e2e={last['avg'].get('e2e')}", flush=True)
        # 期望：count=5，avg.e2e 是 5 轮均值（不是单轮值）
        if last["count"] == 5:
            print(f"\n[OK] count 正确累加到 5", flush=True)
        else:
            print(f"\n[FAIL] count 应为 5，实际 {last['count']}", flush=True)

    if isinstance(tts, type(tts)):
        try:
            await tts._close_ws()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())