"""端到端 Agent 工具验证：语音说「查天气」→ 进度播报 → 工具调用 → 回复

验证消息流：
- tts_start（进度句播报）
- tts_start（最终回复）
- reply（文本显示）
- timing（统计）

用法（先启动后端）：
  cd backend
  python tests\test_agent_ws.py
"""

import asyncio
import json
import os
import sys

import numpy as np
import websockets

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

WS_URL = os.getenv("WS_URL", "ws://127.0.0.1:8001/ws/audio")
SAMPLE_RATE = 16000


async def synth_16k(tts, text: str, scale: float = 1.0) -> bytes:
    chunks = []
    async for c in tts.synth_stream(text):
        chunks.append(c)
    pcm24 = b"".join(chunks)
    data = np.frombuffer(pcm24, dtype=np.int16).astype(np.float32)
    n_out = int(len(data) * 16000 / 24000)
    x_old = np.linspace(0, 1, len(data))
    x_new = np.linspace(0, 1, n_out)
    res = np.interp(x_new, x_old, data).astype(np.int16)
    if scale != 1.0:
        res = (res.astype(np.float32) * scale).astype(np.int16)
    return res.tobytes()


async def main():
    from providers.tts import AliyunTTS

    tts = AliyunTTS()
    user_audio = await synth_16k(tts, "帮我查一下北京明天的天气")
    print(f"[素材] 用户话 {len(user_audio)/2/SAMPLE_RATE*1000:.0f}ms")
    print("=" * 70)

    async with websockets.connect(WS_URL) as ws:
        ready = json.loads(await ws.recv())
        print(f"[连接] session={ready['session_id']}")

        # 正常说话
        await ws.send(json.dumps({"type": "speech_start", "preRollBase64": None}))
        for i in range(0, len(user_audio), 2048):
            await ws.send(user_audio[i : i + 2048])
        await asyncio.sleep(0.15)
        await ws.send(json.dumps({"type": "speech_end"}))
        print("[1] 用户说：帮我查一下北京明天的天气")

        tts_starts = 0   # TTS 播报次数（进度 + 回复）
        reply_text = ""
        timing_count = 0
        progress_seen = {"v": False}  # 是否有进度播报（第一个 tts_start 文本是"好的…"）
        stop_playback_seen = {"v": False}  # 是否收到 stop_playback（最终回复打断进度播报）

        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=30)
            except asyncio.TimeoutError:
                print("[!] 30s 超时")
                break
            if isinstance(raw, bytes):
                continue
            msg = json.loads(raw)
            mtype = msg.get("type")
            if mtype == "stop_playback":
                stop_playback_seen["v"] = True
                print("    [stop_playback] 进度播报被打断 → 转最终回复")
            elif mtype == "tts_start":
                tts_starts += 1
                text = msg.get("text", "")
                if tts_starts == 1 and ("好" in text or "查" in text):
                    progress_seen["v"] = True
                print(f"    [TTS播报#{tts_starts}] {text[:30]}")
            elif mtype == "reply":
                reply_text += msg.get("text", "")
                print(f"    [回复] {msg.get('text','')[:40]}")
            elif mtype == "reply_append":
                reply_text += msg.get("text", "")
            elif mtype == "asr_final":
                print(f"    [ASR] {msg.get('text')}")
            elif mtype == "event":
                if "工具" in msg.get("stage", ""):
                    print(f"    [事件] {msg.get('stage')}: {msg.get('detail')}")
            elif mtype == "timing":
                timing_count += 1
                if timing_count >= 1:
                    print(f"    [timing #{msg.get('count')}] 完成，结束观察")
                    break

        print("=" * 70)
        print("[结果]")
        print(f"  TTS 播报次数: {tts_starts}（期望 ≥2：进度句 + 回复；中间轮不播）")
        print(f"  进度播报: {'✓' if progress_seen['v'] else '✗'}")
        print(f"  stop_playback: {'✓ 出现（进度未播完被最终回复打断）' if stop_playback_seen['v'] else '未触发（进度自然播完，也正确）'}")
        print(f"  回复: {reply_text[:60]!r}")
        print(f"  timing: {'✓' if timing_count >= 1 else '✗'}")
        ok = tts_starts >= 2 and progress_seen["v"] and reply_text and timing_count >= 1
        print(f"  端到端 Agent 工具: {'通过 ✓' if ok else '未通过 ✗'}")


if __name__ == "__main__":
    asyncio.run(main())
