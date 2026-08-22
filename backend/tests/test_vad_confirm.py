"""环节2：后端 Silero VAD 二次确认测试

构造多种音频样本，验证：
1. SileroVAD.is_speech 对真人声/噪声/回声的帧级判定（帧占比）
2. _confirm_real_speech 的最终决策（含 CONFIRM_MIN_AUDIO_MS / CONFIRM_WINDOW_MS 窗口逻辑）

重点排查实测问题：正常说话被后端判为「误报（噪声）」拒绝打断。

用法：
  cd backend
  python test_vad_confirm.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio

import numpy as np

SAMPLE_RATE = 16000


def make_noise(ms, amp=0.02):
    rng = np.random.default_rng(42)
    n = int(SAMPLE_RATE * ms / 1000)
    return (rng.standard_normal(n) * amp * 32767).astype(np.int16).tobytes()


def make_sine(ms, freq=440, amp=0.2):
    n = int(SAMPLE_RATE * ms / 1000)
    t = np.arange(n) / SAMPLE_RATE
    return (np.sin(2 * np.pi * freq * t) * amp * 32767).astype(np.int16).tobytes()


def downsample_24k_to_16k(pcm_bytes: bytes) -> bytes:
    data = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
    n_out = int(len(data) * 16000 / 24000)
    x_old = np.linspace(0, 1, len(data))
    x_new = np.linspace(0, 1, n_out)
    return np.interp(x_new, x_old, data).astype(np.int16).tobytes()


def amp_scale(pcm_bytes: bytes, scale: float) -> bytes:
    data = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
    return (data * scale).astype(np.int16).tobytes()


async def main():
    from vad_engine import SileroVAD

    from providers.tts import AliyunTTS

    vad = SileroVAD(r"../testboard/vad/silero_vad.onnx")
    tts = AliyunTTS()

    # 人声样本：用户说话（用 TTS 模拟真人声）
    chunks = []
    async for c in tts.synth_stream("从五数到十"):
        chunks.append(c)
    voice16 = downsample_24k_to_16k(b"".join(chunks))
    voice_weak = amp_scale(voice16, 0.3)  # 模拟 AEC 削弱后的插话
    voice_very_weak = amp_scale(voice16, 0.1)

    # 西西回声样本：西西说话声（另一段 TTS），经 AEC 后残留（弱化）
    chunks2 = []
    async for c in tts.synth_stream("一、二、三、四、五、六、七、八、九、十！数完啦！有奖励吗？"):
        chunks2.append(c)
    ball_echo16 = downsample_24k_to_16k(b"".join(chunks2))
    ball_echo_weak = amp_scale(ball_echo16, 0.15)

    noise = make_noise(1000)
    sine = make_sine(1000)

    samples = [
        ("真人声(正常音量)", voice16),
        ("真人声(AEC削弱x0.3)", voice_weak),
        ("真人声(严重削弱x0.1)", voice_very_weak),
        ("白噪声", noise),
        ("440Hz正弦波", sine),
        ("西西回声残留(x0.15)", ball_echo_weak),
        ("西西回声+用户插话混合", ball_echo_weak + voice_weak),
    ]

    print("=" * 72)
    print("环节2a：Silero VAD 帧级判定（is_speech, threshold=0.45, ratio=0.05）")
    print("=" * 72)
    for name, pcm in samples:
        is_sp, ratio = vad.is_speech(pcm, 0.45, ratio_threshold=0.05)
        rms = float(np.sqrt(np.mean(np.frombuffer(pcm, dtype=np.int16).astype(np.float32) ** 2)))
        print(f"  {name:<26} RMS={rms:8.1f}  帧占比={ratio:.3f}  判定={'人声' if is_sp else '噪声'}")

    print()
    print("=" * 72)
    print("环节2b：_confirm_real_speech 最终决策（100ms 最短 / 256ms 窗口 / 0.05 占比）")
    print("=" * 72)
    from main import _confirm_real_speech

    for name, pcm in samples:
        decision = _confirm_real_speech(bytearray(pcm))
        print(f"  {name:<26} -> {'确认人声(打断)' if decision else '判定噪声(拒绝)'}")


if __name__ == "__main__":
    asyncio.run(main())
