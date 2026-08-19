"""前端 Silero VAD 行为模拟测试（同模型、同参数，验证噪音误触发）

前端 vad-web 参数（index.html 当前值）：
  VAD_POSITIVE_THRESHOLD = 0.5   # 人声判定阈值
  VAD_NEGATIVE_THRESHOLD = 0.4   # 静音判定阈值
  VAD_MIN_SPEECH_FRAMES  = 4     # 连续多少帧触发 onSpeechStart
  VAD_REDEMPTION_FRAMES  = 8     # 静音多少帧触发 onSpeechEnd
  frameSamples = 1536 (~96ms @16kHz，vad-web 默认)

模拟逻辑：
  - 音频切 96ms 帧，逐帧算 Silero 概率
  - 连续 >= positive 帧数达到 min_frames → 判定「会触发 onSpeechStart」
  - 静音段连续 >= negative 帧数达到 redemption → 判定「会触发 onSpeechEnd」

用法：
  cd backend
  python tests\test_vad_front_sim.py
"""

import os
import sys

import numpy as np
import onnxruntime as ort

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FRAME_SIZE = 1536  # 96ms @16kHz（前端 vad-web 默认）
SAMPLE_RATE = 16000
POSITIVE = 0.5
NEGATIVE = 0.4
MIN_FRAMES = 4
REDEMPTION = 8


class FrontVadSim:
    """直接用 ONNX 跑前端同款帧长（1536）的 Silero VAD，模拟 vad-web 行为"""

    def __init__(self, model_path: str):
        self.session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
        self.reset()

    def reset(self):
        self._h = np.zeros((2, 1, 64), dtype=np.float32)
        self._c = np.zeros((2, 1, 64), dtype=np.float32)
        self._sr = np.array(16000, dtype=np.int64)

    def process(self, frame: np.ndarray) -> float:
        outputs = self.session.run(None, {
            "input": frame.reshape(1, FRAME_SIZE).astype(np.float32),
            "h": self._h,
            "c": self._c,
            "sr": self._sr,
        })
        self._h = outputs[1]
        self._c = outputs[2]
        out = outputs[0]
        if out.shape[-1] == 1:
            return float(out[0][0])
        return float(out[0][1])


def make_noise(ms, kind="white", amp=0.05):
    """生成噪声样本"""
    n = int(SAMPLE_RATE * ms / 1000)
    rng = np.random.default_rng(42)
    if kind == "white":
        return (rng.standard_normal(n) * amp * 32767).astype(np.int16).tobytes()
    if kind == "pink":
        # 粉噪：粗略近似（累积随机游走）
        white = rng.standard_normal(n)
        pink = np.cumsum(white)
        pink = pink / (np.max(np.abs(pink)) + 1e-9)
        return (pink * amp * 32767).astype(np.int16).tobytes()
    return b""


def make_pulse(ms, pulse_ms=10, interval_ms=200, amp=0.3):
    """脉冲噪声（模拟键盘敲击/咳嗽）"""
    n = int(SAMPLE_RATE * ms / 1000)
    buf = np.zeros(n, dtype=np.float32)
    pulse_n = int(SAMPLE_RATE * pulse_ms / 1000)
    interval_n = int(SAMPLE_RATE * interval_ms / 1000)
    for start in range(0, n - pulse_n, interval_n):
        # 衰减脉冲
        t = np.arange(pulse_n)
        buf[start:start + pulse_n] = amp * np.exp(-t / (pulse_n / 4))
    return (buf * 32767).astype(np.int16).tobytes()


def make_tone(ms, freq=440, amp=0.2):
    n = int(SAMPLE_RATE * ms / 1000)
    t = np.arange(n) / SAMPLE_RATE
    return (np.sin(2 * np.pi * freq * t) * amp * 32767).astype(np.int16).tobytes()


def make_silence(ms):
    return bytes(bytearray(int(SAMPLE_RATE * ms / 1000 * 2)))


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


def sim_frontend_vad(vad: FrontVadSim, audio: bytes):
    """模拟前端 vad-web 触发逻辑，返回 (是否触发onSpeechStart, 是否触发onSpeechEnd, 帧概率列表)"""
    pcm = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0
    vad.reset()
    probs = []
    for i in range(0, len(pcm) - FRAME_SIZE + 1, FRAME_SIZE):
        probs.append(vad.process(pcm[i:i + FRAME_SIZE]))

    # onSpeechStart：从静音→人声，连续 >= POSITIVE 帧数达到 MIN_FRAMES
    speaking = False
    speech_start = False
    speech_end = False
    streak = 0
    silent_streak = 0
    for p in probs:
        if p >= POSITIVE:
            streak += 1
            silent_streak = 0
            if not speaking and streak >= MIN_FRAMES:
                speaking = True
                speech_start = True
        else:
            streak = 0
            if speaking:
                silent_streak += 1
                if p < NEGATIVE and silent_streak >= REDEMPTION:
                    speaking = False
                    speech_end = True
    return speech_start, speech_end, probs


async def main():
    from providers.tts import AliyunTTS

    # 模型路径：项目根/testboard/vad/silero_vad.onnx（脚本在 backend/tests/，上三级是项目根）
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    vad = FrontVadSim(os.path.join(root, "testboard", "vad", "silero_vad.onnx"))
    tts = AliyunTTS()

    # 合成真人声样本（TTS 模拟说话）
    chunks = []
    async for c in tts.synth_stream("今天天气真不错呀"):
        chunks.append(c)
    voice16 = downsample_24k_to_16k(b"".join(chunks))

    samples = [
        ("白噪声(低能量 x0.02)", make_noise(3000, "white", 0.02)),
        ("白噪声(高能量 x0.08)", make_noise(3000, "white", 0.08)),
        ("粉噪声 x0.05", make_noise(3000, "pink", 0.05)),
        ("440Hz 正弦波", make_tone(3000)),
        ("脉冲/敲击声(键盘模拟)", make_pulse(3000)),
        ("纯静音", make_silence(2000)),
        ("真人声(TTS 正常音量)", voice16),
        ("真人声(AEC削弱 x0.3)", amp_scale(voice16, 0.3)),
        ("球球回声残留(x0.15)", amp_scale(voice16, 0.15)),
    ]

    print("=" * 76)
    print("前端 Silero VAD 模拟（阈值 0.5 / 4帧确认 / 静音阈值 0.4 / 8帧结束，帧 96ms）")
    print("=" * 76)
    for name, audio in samples:
        start, end, probs = sim_frontend_vad(vad, audio)
        over = sum(1 for p in probs if p >= POSITIVE)
        peak = max(probs) if probs else 0
        mean = sum(probs) / len(probs) if probs else 0
        flag = "★ 会误触发 onSpeechStart" if start else "不触发"
        print(f"  {name:<22} 帧数={len(probs):3d} 峰值={peak:.2f} 均值={mean:.2f} >=0.5帧={over:3d}  → {flag}")
        if start:
            print(f"    ⚠️ 此样本会被前端判定为「人声」→ 触发 ducking + 上报 speech_start")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
