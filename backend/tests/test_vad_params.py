"""后端二次确认参数扫描：对典型样本扫 (人声占比阈值 × 能量跃升阈值) 的判定结果

帮助调 CONFIRM_SPEECH_RATIO / CONFIRM_SPEECH_RATIO_SHORT / CONFIRM_ENERGY_JUMP：

样本：
  A 真插话（正常音量 x1.0）     —— 长缓存（回声基线+插话）应【确认】
  B 真插话（AEC削弱 x0.3）      —— 长缓存应【确认】
  C 纯球球回声（x0.15）         —— 长缓存应【拒绝】（能量平稳）
  D 短缓存真话（<1024ms）       —— 无能量基线，靠占比，应【确认】
  E 短缓存纯回声（<1024ms）     —— 无能量基线，靠占比，应【拒绝】

输出每个样本的 (人声占比, 能量jump)，以及不同阈值组合下的判定。

用法：
  cd backend
  python tests\test_vad_params.py
"""

import asyncio
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vad_engine import SileroVAD  # noqa: E402

SAMPLE_RATE = 16000
WINDOW_MS = 512
BACKEND_THRESHOLD = 0.35  # 后端 Silero 人声概率阈值

# 扫描的阈值组合
RATIO_THRS = [0.05, 0.1, 0.2, 0.3, 0.5]
JUMP_THRS = [2.0, 2.5, 3.0, 3.5, 4.0]


def downsample_24k_to_16k(pcm_bytes: bytes) -> bytes:
    data = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
    n_out = int(len(data) * 16000 / 24000)
    if n_out <= 0:
        return b""
    x_old = np.linspace(0, 1, len(data))
    x_new = np.linspace(0, 1, n_out)
    return np.interp(x_new, x_old, data).astype(np.int16).tobytes()


def amp_scale(pcm_bytes: bytes, scale: float) -> bytes:
    data = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
    return (data * scale).astype(np.int16).tobytes()


def make_noise(ms, amp=0.02):
    rng = np.random.default_rng(42)
    n = int(SAMPLE_RATE * ms / 1000)
    return (rng.standard_normal(n) * amp * 32767).astype(np.int16).tobytes()


def analyze(vad, audio_cache: bytes):
    """返回 (人声占比, 能量jump, 缓存ms)，模拟 _confirm_real_speech 的核心计算"""
    window_bytes = int(SAMPLE_RATE * WINDOW_MS / 1000 * 2)
    recent = audio_cache[-window_bytes:] if len(audio_cache) > window_bytes else audio_cache

    # 人声占比
    _, ratio = vad.is_speech(bytes(recent), BACKEND_THRESHOLD, ratio_threshold=0.0)
    # 能量 jump
    jump = None
    if len(audio_cache) >= 2 * window_bytes:
        prev = audio_cache[-2 * window_bytes:-window_bytes]
        p = np.frombuffer(prev, dtype=np.int16).astype(np.float32)
        r = np.frombuffer(recent, dtype=np.int16).astype(np.float32)
        prev_rms = float(np.sqrt(np.mean(p ** 2))) if len(p) else 0.0
        recent_rms = float(np.sqrt(np.mean(r ** 2))) if len(r) else 0.0
        if prev_rms > 30:
            jump = recent_rms / prev_rms if prev_rms > 0 else 0.0
    return ratio, jump, len(audio_cache) / 2 / SAMPLE_RATE * 1000


async def main():
    from providers.tts import AliyunTTS

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    vad = SileroVAD(os.path.join(root, "testboard", "vad", "silero_vad.onnx"))
    tts = AliyunTTS()

    chunks = []
    async for c in tts.synth_stream("从五数到十"):
        chunks.append(c)
    voice = downsample_24k_to_16k(b"".join(chunks))

    # 回声基线（模拟球球正在说话）
    echo = amp_scale(voice, 0.15)
    silence = bytes(bytearray(int(SAMPLE_RATE * 0.3 * 2)))

    # 模拟真实时序：speech_start 到达时 cache = 回声基线(前) + 用户开口后 576ms(后，VAD 确认延迟)
    # 近窗 512ms = 用户刚开口的语音开头（能量高）→ jump 应显著 >1
    open_576 = voice[: int(SAMPLE_RATE * 0.576 * 2)]            # 真话开头 576ms
    open_576_weak = amp_scale(open_576, 0.3)                    # AEC 削弱
    echo_576 = echo[: int(SAMPLE_RATE * 0.576 * 2)]             # 回声开头 576ms（平稳）

    samples = {
        "A 真插话x1.0 (长缓存)": bytes(echo + open_576),                    # 回声基线 + 真话开头
        "B 真插话x0.3 (长缓存)": bytes(echo + open_576_weak),               # 回声基线 + 弱插话开头
        "C 纯回声x0.15 (长缓存)": bytes(echo + echo_576),                   # 回声基线 + 回声（平稳）
        "D 真话x1.0 (短缓存)": bytes(silence + open_576),                  # 短缓存（静音+真话开头）
        "E 纯回声x0.15 (短缓存)": bytes(silence + echo_576),               # 短缓存（静音+回声）
        "F 白噪声 (长缓存)": bytes(echo + make_noise(2000)),
    }

    print("=" * 100)
    print("样本分析：人声占比 / 能量jump（后端阈值 0.35，窗口 512ms）")
    print("=" * 100)
    stats = {}
    for name, audio in samples.items():
        ratio, jump, ms = analyze(vad, audio)
        stats[name] = (ratio, jump, ms)
        print(f"  {name:<28} 缓存{ms:6.0f}ms  人声占比={ratio:5.2f}  能量jump={jump if jump is None else round(jump,2)}")

    print()
    print("=" * 100)
    print("判定矩阵：行=人声占比阈值(ratio_thr)，列=能量jump阈值(jump_thr)")
    print("单元格：✓确认 / ✗拒绝（同时满足 占比>=ratio_thr 和 能量jump>=jump_thr 才确认；短缓存只看占比）")
    print("=" * 100)
    for name, (ratio, jump, ms) in stats.items():
        short = ms < 2 * WINDOW_MS  # 短缓存：无能量判断，只看占比
        print(f"\n  {name}（{'短缓存·只看占比' if short else '长缓存·占比+能量'}）")
        header = "        " + "".join(f"jump≥{j:<5}" for j in JUMP_THRS)
        print(header)
        for rt in RATIO_THRS:
            row = f"  ratio≥{rt:<5} "
            for jt in JUMP_THRS:
                if short:
                    ok = ratio >= rt  # 短缓存：只有占比
                else:
                    ok = ratio >= rt and (jump is None or jump >= jt)
                row += f"{'✓' if ok else '✗':<9}"
            print(row)


if __name__ == "__main__":
    asyncio.run(main())
