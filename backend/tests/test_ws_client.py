"""环节3：模拟前端 WS 客户端 —— 端到端复现打断链路（验证修复）

流程：
1. 连接后端，模拟正常说话（speech_start → 用户话音频 → speech_end）
2. 等西西开始回复（tts_start）
3. 西西说话期间模拟用户插话：
   a. 先发 300ms 插话音频（进后端 speaking_audio_cache，模拟 VAD 触发前的开口）
   b. 发 speech_start + preRoll（开口前 256ms 静音）
   c. 继续发插话音频
   d. 等 barge_confirm / barge_reject（验证「误报」修复）
   e. 发 speech_end
   f. 等 asr_final（验证「丢字/识别错误」修复）

用法（先启动后端）：
  cd backend
  python test_ws_client.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio
import base64
import json
import os

import numpy as np
import websockets

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


def find_voice_start(pcm: bytes, frame_ms: int = 20, thr: float = 200.0) -> int:
    """找到第一个能量超过阈值的帧（模拟用户真实开口点），返回字节偏移"""
    data = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    n = int(SAMPLE_RATE * frame_ms / 1000)
    for i in range(0, len(data) - n, n):
        frame = data[i : i + n]
        rms = float(np.sqrt(np.mean(frame**2)))
        if rms > thr:
            return i * 2
    return 0


async def main():
    from providers.tts import AliyunTTS

    tts = AliyunTTS()

    # 预合成素材
    user_audio = await synth_16k(tts, "从五数到十", 0.3)  # 用户插话（AEC 削弱）
    print(f"[素材] 用户话 {len(user_audio)/2/SAMPLE_RATE*1000:.0f}ms")
    # 找到语音起点（跳过 TTS 前导静音，模拟真实「开口瞬间」）
    voice_start = find_voice_start(user_audio)
    print(f"[素材] 语音起点 {voice_start/2/SAMPLE_RATE*1000:.0f}ms 处")
    pre_roll_start = max(0, voice_start - int(SAMPLE_RATE * 0.256 * 2))  # 开口前 256ms
    pre_roll = user_audio[pre_roll_start:voice_start]  # 开口前 256ms（静音/轻声）
    if len(pre_roll) < int(SAMPLE_RATE * 0.256 * 2):
        pre_roll = pre_roll + bytes(bytearray(int(SAMPLE_RATE * 0.256 * 2) - len(pre_roll)))
    print(f"[素材] 预卷 {len(pre_roll)/2/SAMPLE_RATE*1000:.0f}ms")
    print("=" * 70)

    async with websockets.connect(WS_URL) as ws:
        # 收 ready
        ready = json.loads(await ws.recv())
        print(f"[1] ready: {ready['session_id']}")

        async def send_audio(pcm: bytes, chunk: int = 2048):
            for i in range(0, len(pcm), chunk):
                await ws.send(pcm[i : i + chunk])

        # ── 阶段1：正常说话 ──
        print("[2] 模拟用户正常说话：从五数到十")
        await ws.send(json.dumps({"type": "speech_start", "preRollBase64": None}))
        await send_audio(user_audio)
        await asyncio.sleep(0.15)
        await ws.send(json.dumps({"type": "speech_end"}))

        # ── 收消息循环：等西西开始播放后触发插话 ──
        barge_triggered = False
        barge_result = None
        asr_final_text = None      # 正常说话的识别结果
        barge_asr_text = None      # 插话的识别结果
        reply_text = ""
        timing_count = 0           # timing 消息计数（验证打断轮次也补发统计）

        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=30)
            except asyncio.TimeoutError:
                print("[!] 30s 无消息，超时退出")
                break

            if isinstance(raw, bytes):
                continue  # 西西 TTS 音频，忽略

            msg = json.loads(raw)
            mtype = msg.get("type")

            if mtype == "asr_partial":
                print(f"    [ASR增量] {msg['text']}")
            elif mtype == "asr_final":
                text = msg.get("text", "")
                if barge_triggered and barge_asr_text is None:
                    barge_asr_text = text
                    print(f"    [插话ASR最终] {text!r}")
                else:
                    asr_final_text = text
                    print(f"    [ASR最终] {text!r}")
            elif mtype == "reply_start":
                print("[3] 西西开始回复")
            elif mtype == "reply":
                reply_text += msg.get("text", "")
                print(f"    [西西] {msg.get('text','')}")
            elif mtype == "reply_append":
                reply_text += msg.get("text", "")
                print(f"    [西西+] {msg.get('text','')}")
            elif mtype == "tts_start":
                if not barge_triggered:
                    barge_triggered = True
                    print("[4] 西西开始播放 → 上报 client_play_start（模拟喇叭发声），后端进入 speaking")
                    await ws.send(json.dumps({"type": "client_play_start"}))
                    await asyncio.sleep(0.05)  # 立即插话（在西西第一句 TTS 合成中打断，触发流水线取消）
                    print("[4b] 模拟用户插话：先发「开口前静音+开口后400ms语音」进缓存（VAD触发延迟），再报 speech_start")
                    # a. 开口前 300ms 静音 + 开口后 400ms 语音（进后端缓存，模拟 VAD 触发前用户已开口）
                    lead_in = user_audio[max(0, voice_start - int(SAMPLE_RATE * 0.3 * 2)):voice_start]
                    voice_head = user_audio[voice_start : voice_start + int(SAMPLE_RATE * 0.4 * 2)]
                    await send_audio(lead_in + voice_head)
                    # b. 报 speech_start + preRoll（开口前 256ms）
                    await ws.send(json.dumps({
                        "type": "speech_start",
                        "preRollBase64": base64.b64encode(pre_roll).decode(),
                    }))
                    # c. 发剩余语音（用户插话内容）
                    await send_audio(user_audio[voice_start + int(SAMPLE_RATE * 0.4 * 2):])
                    print("[5] 插话音频已发完，等待后端二次确认…")
                    await asyncio.sleep(0.3)
                    # e. 用户说完
                    await ws.send(json.dumps({"type": "speech_end"}))
                    print("[6] 已发 speech_end，等待识别结果…")
            elif mtype == "barge_confirm":
                barge_result = "barge_confirm"
                print(f"    ★ barge_confirm（确认真打断，后端耗时 {msg.get('backend_ms')}ms）→ 误报修复验证: 通过")
            elif mtype == "barge_reject":
                barge_result = "barge_reject"
                print("    ★ barge_reject（判噪声拒绝）→ 误报修复验证: 未通过！")
            elif mtype == "reply_end":
                # 打断流程完成（barge_confirm + 插话识别结果都拿到）且收到补发 timing 后退出
                if barge_result is not None and barge_asr_text is not None and timing_count >= 2:
                    break
            elif mtype == "tts_end":
                pass
            elif mtype == "event":
                pass
            elif mtype == "timing":
                timing_count += 1
                print(f"    [timing #{msg.get('count')}] interrupted={msg.get('current', {}).get('interrupted', False)}")

        print("=" * 70)
        print("[结果]")
        print(f"  打断判定: {barge_result}")
        print(f"  正常说话识别: {asr_final_text!r}")
        print(f"  插话识别: {barge_asr_text!r}")
        print(f"  timing 消息数: {timing_count}（期望 ≥2：正常轮 1 + 打断轮补发 1）")
        if barge_result == "barge_confirm":
            print(f"  误报修复验证: 通过（正常插话被确认真打断）")
        elif barge_result == "barge_reject":
            print(f"  误报修复验证: 未通过（正常插话被判噪声）")
        else:
            print(f"  误报修复验证: 未执行（未收到 barge_confirm/reject）")
        if barge_asr_text is not None:
            ok = "从五数到十" in barge_asr_text or barge_asr_text.replace("。", "").replace("？", "").strip() == "从五数到十"
            print(f"  丢字/错字修复验证: {'通过' if ok else '未通过（请检查打断喂音频策略）'}")
        else:
            print(f"  丢字/错字修复验证: 未执行（未收到插话识别结果）")

if __name__ == "__main__":
    asyncio.run(main())
