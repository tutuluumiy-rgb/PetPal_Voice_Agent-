"""验证 MiniMax WS 预热机制：预热建连耗时 + 预热后首句是否免建连

对比：
  A. 不预热：首句 = 建连(5.5s?) + task_continue
  B. 预热后：首句 = task_continue（连接已 ready）
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ".")

os.environ["MINIMAX_TRANSPORT"] = "ws"

from providers.minimax_tts import MiniMaxTTS  # noqa: E402

SENTENCES = [
    "比如你打字，它能变成人声读出来。",
    "像音箱、导航、有声书都在用。",
    "解说AI和语音技术的原理。",
]


async def synth_one(t, text, label):
    t.first_audio_time = None
    t0 = time.time()
    n = 0
    async for c in t.synth_stream(text, {}):
        n += len(c)
    dt = time.time() - t0
    fa = t.first_audio_time
    print(f"  [{label}] 首包={fa}s 整句={dt:.2f}s 音频={n}B", flush=True)
    return dt


async def main():
    t = MiniMaxTTS()
    print(f"transport={t.transport}  ws_ready={t._ws_ready}", flush=True)

    # ── 场景A：不预热，直接首句（模拟后端刚启动没预热）──
    print("\n[A] 不预热：第一句直接合成", flush=True)
    await synth_one(t, SENTENCES[0], "A-句1")
    print("  ws_ready=", t._ws_ready, " ws 复用=", t._ws is not None, flush=True)

    # ── 场景B：预热后首句 ──
    print("\n[B] 清空连接，重新建 → 预热 → 首句", flush=True)
    await t._close_ws()
    tp = time.time()
    await t.preheat()
    print(f"  [预热] 建连+task_started 耗时={time.time()-tp:.2f}s", flush=True)
    await synth_one(t, SENTENCES[1], "B-句1(预热后)")
    await synth_one(t, SENTENCES[2], "B-句2(复用)")

    await t._close_ws()
    print("\n[done]", flush=True)


if __name__ == "__main__":
    asyncio.run(main())