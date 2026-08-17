"""架构修复验证：打断后立即再说话（防重叠）+ 连续打断

场景：
A. 正常说话 → 球球回复中插话打断 → barge_confirm + 插话识别
B. 打断识别刚完成，【立即】再说话（不等球球第二轮回复）→ 验证防重叠逻辑
C. 第二轮球球回复中再次打断 → 连续打断稳定性

用法（先启动后端）：
  cd backend
  python test_ws_concurrent.py
"""

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
    data = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    n = int(SAMPLE_RATE * frame_ms / 1000)
    for i in range(0, len(data) - n, n):
        if float(np.sqrt(np.mean(data[i : i + n] ** 2))) > thr:
            return i * 2
    return 0


def make_pre_roll(pcm: bytes, voice_start: int) -> bytes:
    pre_roll_len = int(SAMPLE_RATE * 0.256 * 2)
    pre = pcm[max(0, voice_start - pre_roll_len) : voice_start]
    if len(pre) < pre_roll_len:
        pre = pre + bytes(bytearray(pre_roll_len - len(pre)))
    return pre


async def main():
    from tts_engine import TTSEngine

    tts = TTSEngine()

    # 素材：插话B="从二数到十"，打断后立即说 B2="从三数到十"，再插话 C="从四数到十"
    mats = {}
    for name, text in [("A", "从五数到十"), ("B", "从二数到十"), ("B2", "从三数到十"), ("C", "从四数到十")]:
        pcm = await synth_16k(tts, text, 0.3)
        mats[name] = (pcm, find_voice_start(pcm))
        print(f"[素材{name}] {text} 语音起点 {mats[name][1]/2/SAMPLE_RATE*1000:.0f}ms")

    print("=" * 70)

    async with websockets.connect(WS_URL) as ws:
        ready = json.loads(await ws.recv())
        print(f"[连接] session={ready['session_id']}")

        async def send_audio(pcm: bytes, chunk: int = 2048):
            for i in range(0, len(pcm), chunk):
                await ws.send(pcm[i : i + chunk])

        async def normal_speech(name: str):
            """正常说话：speech_start(无预卷) → 音频 → speech_end"""
            pcm, vs = mats[name]
            lead_in = pcm[max(0, vs - int(SAMPLE_RATE * 0.3 * 2)) : vs]
            voice_head = pcm[vs : vs + int(SAMPLE_RATE * 0.4 * 2)]
            await ws.send(json.dumps({"type": "speech_start", "preRollBase64": None}))
            await send_audio(lead_in + voice_head + pcm[vs + int(SAMPLE_RATE * 0.4 * 2):])
            await asyncio.sleep(0.15)
            await ws.send(json.dumps({"type": "speech_end"}))

        async def barge_speech(name: str):
            """播放中插话：先发开口前+语音进缓存 → speech_start(带预卷) → 剩余语音 → speech_end"""
            pcm, vs = mats[name]
            lead_in = pcm[max(0, vs - int(SAMPLE_RATE * 0.3 * 2)) : vs]
            voice_head = pcm[vs : vs + int(SAMPLE_RATE * 0.4 * 2)]
            pre_roll = make_pre_roll(pcm, vs)
            await ws.send(json.dumps({"type": "client_play_start"}))
            await asyncio.sleep(0.3)
            await send_audio(lead_in + voice_head)
            await ws.send(json.dumps({
                "type": "speech_start",
                "preRollBase64": base64.b64encode(pre_roll).decode(),
            }))
            await send_audio(pcm[vs + int(SAMPLE_RATE * 0.4 * 2):])
            await asyncio.sleep(0.6)
            await ws.send(json.dumps({"type": "speech_end"}))

        # ── 状态机 ──
        phase = "normal_A"          # normal_A → wait_barge1 → barge1_done → fast_B2 → wait_barge2 → barge2_done
        results = {}
        barge_ok = []

        print("[1] 用户说话 A：从五数到十")
        await normal_speech("A")

        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=35)
            except asyncio.TimeoutError:
                print("[!] 35s 超时，退出")
                break
            if isinstance(raw, bytes):
                continue
            msg = json.loads(raw)
            mtype = msg.get("type")

            if mtype == "asr_final":
                text = msg.get("text", "")
                if phase == "normal_A":
                    results["A"] = text
                    print(f"    [识别A] {text!r} → 等待球球回复")
                    phase = "wait_barge1"
                elif phase == "barge1_done":
                    results["B"] = text
                    print(f"    [插话B识别] {text!r} → 【立即】再说 B2（防重叠测试）")
                    phase = "fast_B2"
                    await normal_speech("B2")
                elif phase == "fast_B2":
                    results["B2"] = text
                    print(f"    [B2识别] {text!r} → 等待球球第三轮回复")
                    phase = "wait_barge2"
                elif phase == "barge2_done":
                    results["C"] = text
                    print(f"    [插话C识别] {text!r}")
                    phase = "done"
            elif mtype == "tts_start":
                if phase == "wait_barge1":
                    print("[1b] 球球播放中 → 插话 B（从二数到十）")
                    phase = "wait_barge1_confirm"
                    await barge_speech("B")
                elif phase == "wait_barge2":
                    print("[2b] 球球第二轮回复中 → 再次插话 C（从四数到十，连续打断）")
                    phase = "wait_barge2_confirm"
                    await barge_speech("C")
            elif mtype == "barge_confirm":
                if phase == "wait_barge1_confirm":
                    print(f"    ★ 插话B barge_confirm（后端 {msg.get('backend_ms')}ms）")
                    barge_ok.append("B")
                    phase = "barge1_done"
                elif phase == "wait_barge2_confirm":
                    print(f"    ★ 插话C barge_confirm（后端 {msg.get('backend_ms')}ms）→ 连续打断稳定")
                    barge_ok.append("C")
                    phase = "barge2_done"
            elif mtype == "barge_reject":
                if phase == "wait_barge1_confirm":
                    print("    ★ 插话B barge_reject（误报！）")
                    barge_ok.append("B_REJECT")
                    phase = "barge1_done"
                elif phase == "wait_barge2_confirm":
                    print("    ★ 插话C barge_reject（误报！）")
                    barge_ok.append("C_REJECT")
                    phase = "barge2_done"
            elif mtype == "asr_partial":
                pass
            elif mtype == "reply" or mtype == "reply_append":
                pass
            elif mtype == "reply_end":
                pass

            if phase == "done":
                break

        print("=" * 70)
        print("[结果]")
        print(f"  第一轮识别: {results.get('A')!r}")
        print(f"  插话B打断: {barge_ok[0] if barge_ok else '未触发'}")
        print(f"  插话B识别: {results.get('B')!r}")
        print(f"  打断后立即再说话识别: {results.get('B2')!r}")
        print(f"  连续打断C: {barge_ok[1] if len(barge_ok)>1 else '未触发'}")
        print(f"  插话C识别: {results.get('C')!r}")
        ok_b = bool(barge_ok) and barge_ok[0] == "B" and "从二数到十" in (results.get("B") or "")
        ok_b2 = "从三数到十" in (results.get("B2") or "")
        ok_c = len(barge_ok) >= 2 and barge_ok[1] == "C" and "从四数到十" in (results.get("C") or "")
        print(f"  防重叠验证: {'通过' if ok_b2 else '未通过'}")
        print(f"  连续打断验证: {'通过' if ok_c else '未触发'}")
        print(f"  综合: {'全部通过 ✓' if (ok_b and ok_b2 and ok_c) else '部分完成（参考后端日志）'}")


if __name__ == "__main__":
    asyncio.run(main())
