"""首字丢失复现（真实 AliyunASR API，允许联网调用）

模拟非打断（listening 正常收话）时序：
  前端 VAD 判定需要 ~192ms 连续人声 → 这期间 listening 态的音频帧在后端被丢弃；
  speech_start 到达后 ASR 才开始收。preRoll 是补回这 192ms 的唯一素材。

对照三组真实识别：
  A 旧行为（不喂 preRoll）：只喂 [onset+192ms →] → 预期首字缺失/错认
  B 修复后（喂 preRoll）  ：先喂 preRoll(=onset 前 192ms) 再喂余下 → 预期完整
  C 重复喂（排查项）      ：preRoll 喂两次 → 观察是否出现 那那/对对 类重复错字

用法：cd backend && python scripts/repro_first_char.py
"""

import asyncio
import sys

import numpy as np

sys.path.insert(0, ".")
sys.path.insert(0, "providers")

SAMPLE = 16000


async def load_speech(text, out="probe_first_char.mp3"):
    import edge_tts
    import miniaudio
    await edge_tts.Communicate(text, "zh-CN-XiaoxiaoNeural", rate="-10%").save(out)
    with open(out, "rb") as f:
        d = miniaudio.decode(f.read(), output_format=miniaudio.SampleFormat.SIGNED16,
                             nchannels=1, sample_rate=SAMPLE)
    return np.frombuffer(bytes(d.samples), dtype=np.int16).astype(np.float32) / 32768.0


def pcm16(x: np.ndarray) -> bytes:
    return (np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes()


async def transcribe(feeds: list[bytes], tag: str) -> str:
    """真实 AliyunASR：建立会话 → 依次 feed → finalize。feeds = 分段音频字节。"""
    from providers.asr import AliyunASR
    asr = AliyunASR()
    sid = f"repro-{tag}"
    partials = []
    asr.start_streaming(sid, lambda t: partials.append(t))
    for f in feeds:
        # 按 100ms 帧喂（模拟实时）
        chunk = int(SAMPLE * 0.1)
        for i in range(0, len(f), chunk):
            asr.feed(sid, f[i:i + chunk])
            await asyncio.sleep(0.01)  # 让后台线程发送
    text = await asr.finalize(sid)
    return text


async def main():
    speech = await load_speech("那对我觉得挺好的")
    n_onset = int(SAMPLE * 0.192)          # 前端 VAD 判定窗口(~192ms)内丢失的音频
    onset = speech[:n_onset]                # = preRoll 应补回的首字段（近似）
    rest = speech[n_onset:]                 # = speech_start 之后 ASR 实际收到的
    print(f"整句 {len(speech)/SAMPLE:.2f}s | 首字段 {len(onset)/SAMPLE*1000:.0f}ms | 后续 {len(rest)/SAMPLE*1000:.0f}ms")

    print("\n[A] 旧行为：不喂 preRoll（只喂 192ms 之后）")
    ta = await transcribe([pcm16(rest)], "A_nopreroll")
    print(f"    识别 = {ta!r}")

    print("\n[B] 修复后：先喂 preRoll(首字段) 再喂余下")
    tb = await transcribe([pcm16(onset), pcm16(rest)], "B_withpreroll")
    print(f"    识别 = {tb!r}")

    print("\n[C] 排查：preRoll 重复喂两次（观察 那那/对对 类重复错字）")
    tc = await transcribe([pcm16(onset), pcm16(onset), pcm16(rest)], "C_duppreroll")
    print(f"    识别 = {tc!r}")

    print("\n════ 对照 ════")
    print(f"  A(不喂 preRoll): {ta!r}")
    print(f"  B(喂 preRoll)  : {tb!r}")
    print(f"  C(重复喂)      : {tc!r}")
    ok = "那" in tb and (ta == tb or "那" not in ta)
    print(f"  首字在 B 中出现、A 中缺失/错认: {'是' if ('那' in tb and '那' not in ta) else '见上述对照'}")


if __name__ == "__main__":
    asyncio.run(main())