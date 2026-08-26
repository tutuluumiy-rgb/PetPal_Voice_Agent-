"""诊断打断后 ASR 音频丢失的探针

模拟完整时序：
1. 说话 → 后端 ASR finalize → LLM → TTS
2. 球球播放中
3. 用户打断 → speech_start → 后端二次确认 → barge_confirm → ASR 启动
4. 用户连续说话（多段，中间停顿）
5. speech_end → ASR finalize
6. 用户又继续说（新 audio）

观察：
- ASR 会话何时被 pop
- cache_tail 是否喂入
- 用户后续 audio 流向
- preRoll 是否被处理
"""
import asyncio
import base64
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import websockets

# 复用 AliyunTTS 合成"用户音频"——ASR 一定能识别
sys.path.insert(0, ".")
os.environ["TTS_PROVIDER"] = "ali"
os.environ["TTS_MODEL"] = "qwen3-tts-instruct-flash-realtime"
from providers.tts import AliyunTTS  # noqa: E402

WS_URL = os.getenv("WS_URL", "ws://127.0.0.1:8001/ws/audio")
SAMPLE_RATE = 16000


def synth_silence_with_seed(duration_ms, seed, amp=0.05):
    """生成带可识别特征的合成音频（不同 seed 不同节奏），模拟用户说话"""
    rng = np.random.default_rng(seed)
    n = SAMPLE_RATE * duration_ms // 1000
    return (rng.normal(0, amp, n) * 32767).astype(np.int16).tobytes()


async def synth_user_audio(text: str) -> bytes:
    """用 AliyunTTS 合成用户语音（24k mono），重采样到 16k 给 ASR"""
    tts = AliyunTTS()
    chunks = []
    async for c in tts.synth_stream(text, {"instructions": "用平静温和、自然的语气说"}):
        chunks.append(c)
    pcm24 = b"".join(chunks)
    # 24k → 16k 重采样
    data = np.frombuffer(pcm24, dtype=np.int16).astype(np.float32)
    n_out = int(len(data) * 16000 / 24000)
    x_old = np.linspace(0, 1, len(data))
    x_new = np.linspace(0, 1, n_out)
    res = np.interp(x_new, x_old, data).astype(np.int16)
    return res.tobytes()


def synth_speech_like(duration_ms, seed, freq=440):
    """用正弦波 + 谐波模拟"语音"，比纯噪声更像人声（ASR 不会被 filter）"""
    rng = np.random.default_rng(seed)
    n = SAMPLE_RATE * duration_ms // 1000
    t = np.arange(n) / SAMPLE_RATE
    # 基频 + 谐波（模拟声带+共振峰）
    sig = (np.sin(2*np.pi*freq*t) * 0.3 +
           np.sin(2*np.pi*freq*2*t) * 0.15 +
           np.sin(2*np.pi*freq*3*t) * 0.08 +
           rng.normal(0, 0.05, n))
    # 调幅（模拟语速节奏）
    envelope = 0.5 + 0.5 * np.sin(2*np.pi*4*t + seed)
    sig = sig * envelope
    return (sig * 20000).astype(np.int16).tobytes()


async def main():
    print(f"=== 探针：打断后 ASR 音频流 ===\n", flush=True)
    timings_seen = []
    asr_texts = []

    async with websockets.connect(WS_URL) as ws:
        ready = json.loads(await ws.recv())
        print(f"[ready] session_id={ready.get('session_id')}", flush=True)

        # 阶段1: 说话让球球回复（启动 TTS 播放）
        print("\n[阶段1] 用户先说一句让球球回复（让 TTS 进入 speaking）", flush=True)
        await ws.send(json.dumps({"type": "speech_start", "preRollBase64": None}))
        # 用 TTS 合成"用户音频"（ASR 一定能识别）
        user_tts_audio = await synth_user_audio("今天天气怎么样。")
        for off in range(0, len(user_tts_audio), 2048):
            await ws.send(user_tts_audio[off:off+2048])
        await asyncio.sleep(0.1)
        await ws.send(json.dumps({"type": "speech_end"}))

        # 收消息直到收到 tts_start（球球开始播放）或 timing
        t_tts_start = None
        t_ball_reply_done = None
        # 阶段1 收所有消息（不只等 tts_start），看后端到底跑了什么
        for i in range(200):
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=3)
            except asyncio.TimeoutError:
                break
            if isinstance(raw, bytes):
                continue
            msg = json.loads(raw)
            mt = msg.get("type")
            if i < 20:  # 前 20 条全打印，诊断用
                print(f"    [p1 msg #{i}] {mt}: {str(msg)[:120]}", flush=True)
            if mt == "tts_start":
                t_tts_start = time.time()
                print(f"  [tts_start] 球球开始播放，模拟用户此时打断", flush=True)
                break
            if mt == "timing":
                timings_seen.append(msg)
                print(f"  [timing] {msg.get('count')}", flush=True)

        if not t_tts_start:
            print("  [!] 没收到 tts_start，跳过打断测试", flush=True)
            return

        # 阶段2: 用户打断（球球播放中）
        # 等 1.5 秒让球球真正播放
        print("\n[阶段2] 等 1.5s 让球球播放", flush=True)
        await asyncio.sleep(1.5)
        # 用户正式打断
        t_barge_start = time.time()
        # 发 256ms preRoll（真实用户开头，Silero 能过 → confirm 路径）
        pre_roll = await synth_user_audio("测试预卷")
        if len(pre_roll) > 8192:
            pre_roll = pre_roll[:8192]
        import base64
        pre_roll_b64 = base64.b64encode(pre_roll).decode('ascii')
        await ws.send(json.dumps({"type": "speech_start", "preRollBase64": pre_roll_b64}))
        # 发 2.5s TTS 合成的"用户音频"（能过二次确认）
        user_audio_1 = await synth_user_audio("打断测试第一句话，今天天气真好啊。")
        for off in range(0, len(user_audio_1), 2048):
            await ws.send(user_audio_1[off:off+2048])
        print(f"  已发打断后第一段用户音频 2s（持续 feed）", flush=True)

        # 收消息，期望：barge_confirm + asr_partial
        got_barge_confirm = False
        for _ in range(50):
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=3)
            except asyncio.TimeoutError:
                break
            if isinstance(raw, bytes):
                continue
            msg = json.loads(raw)
            mt = msg.get("type")
            if mt == "barge_confirm":
                got_barge_confirm = True
                print(f"  [barge_confirm] 后端确认打断 (backend_ms={msg.get('backend_ms')})", flush=True)
            elif mt == "asr_partial":
                print(f"  [asr_partial] {msg.get('text', '')!r}", flush=True)
            elif mt == "asr_final":
                text = msg.get("text", "")
                asr_texts.append(("barge", text))
                print(f"  [asr_final] 打断 ASR 输出: {text!r}", flush=True)
            elif mt == "reply":
                print(f"  [reply] 球球回复: {msg.get('text', '')[:50]!r}...", flush=True)
            elif mt == "tts_start":
                print(f"  [tts_start] 球球开始回复（打断后）", flush=True)
            elif mt == "timing":
                timings_seen.append(msg)
            elif mt == "barge_avg":
                pass

        if not got_barge_confirm:
            print("  [!] 没收到 barge_confirm", flush=True)
            return

        # 阶段3: 用户继续说话（中间停顿后）—— 模拟"边想边说"
        print("\n[阶段3] 用户继续说话（停顿后又开口），看新 audio 流向", flush=True)
        # 等 200ms 后再发 audio（模拟停顿后继续）
        await asyncio.sleep(0.2)
        # 模拟用户继续说 1.5s（TTS 合成）
        user_audio_2 = await synth_user_audio("打断后继续说。")
        for off in range(0, len(user_audio_2), 2048):
            await ws.send(user_audio_2[off:off+2048])
        print(f"  已发继续说话音频 1.5s（无 speech_start，因为 VAD 还在认为上一段没结束）", flush=True)

        # 等消息
        for _ in range(30):
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2)
            except asyncio.TimeoutError:
                break
            if isinstance(raw, bytes):
                continue
            msg = json.loads(raw)
            mt = msg.get("type")
            if mt == "asr_partial":
                print(f"  [asr_partial] {msg.get('text', '')!r}", flush=True)
            elif mt == "asr_final":
                text = msg.get("text", "")
                asr_texts.append(("barge+continue", text))
                print(f"  [asr_final] 打断后继续说话 ASR 输出: {text!r}", flush=True)
            elif mt == "reply":
                print(f"  [reply] 球球回复: {msg.get('text', '')[:50]!r}...", flush=True)
            elif mt == "tts_start":
                pass
            elif mt == "timing":
                timings_seen.append(msg)

        # 阶段4: 用户发 speech_end
        print("\n[阶段4] 用户说完 speech_end", flush=True)
        await ws.send(json.dumps({"type": "speech_end"}))

        # 收最终消息
        for _ in range(30):
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=3)
            except asyncio.TimeoutError:
                break
            if isinstance(raw, bytes):
                continue
            msg = json.loads(raw)
            mt = msg.get("type")
            if mt == "asr_final":
                text = msg.get("text", "")
                asr_texts.append(("end", text))
                print(f"  [asr_final] speech_end ASR 输出: {text!r}", flush=True)
            elif mt == "reply":
                print(f"  [reply] 球球回复: {msg.get('text', '')[:50]!r}...", flush=True)
            elif mt == "timing":
                timings_seen.append(msg)

    print(f"\n=== 汇总 ===", flush=True)
    print(f"ASR 文本: {asr_texts}", flush=True)
    print(f"timing 消息数: {len(timings_seen)}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())