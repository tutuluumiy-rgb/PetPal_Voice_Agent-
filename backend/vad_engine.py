"""后端 Silero VAD 引擎（业务层二次确认）

用 onnxruntime 跑 Silero VAD，对前端上报的人声做「二次确认」。
前后端阈值分离：
- 前端：更敏感（快速 ducking，宁可误报不漏报）
- 后端：更准确（确认真人声才打断，过滤噪声/回声）

Silero VAD 推理逻辑（参考官方）：
- 输入：16kHz float32 单声道音频帧
- 模型状态：h、c 隐藏状态（RNN），需要跨帧传递
- 输出：is_speech 概率

API Key 无，纯本地推理。
"""

import os

import numpy as np
import onnxruntime as ort

SAMPLE_RATE = 16000
FRAME_SIZE = 512  # Silero 推荐帧大小（16kHz 下 32ms）


class SileroVAD:
    """后端 Silero VAD，用于二次确认人声/噪声"""

    def __init__(self, model_path: str):
        if not os.path.exists(model_path):
            raise RuntimeError(f"Silero 模型不存在: {model_path}")

        # 加载 ONNX 模型
        self.session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
        self.reset_state()

    def reset_state(self):
        """重置 RNN 隐藏状态"""
        self._h = np.zeros((2, 1, 64), dtype=np.float32)
        self._c = np.zeros((2, 1, 64), dtype=np.float32)
        self._sr = np.array(16000, dtype=np.int64)

    def process(self, audio_frame: np.ndarray) -> float:
        """处理一帧音频，返回 is_speech 概率（0~1）

        audio_frame: float32 数组，长度 FRAME_SIZE（512）
        """
        if len(audio_frame) != FRAME_SIZE:
            raise ValueError(f"音频帧长度必须为 {FRAME_SIZE}，实际 {len(audio_frame)}")

        input_tensor = audio_frame.reshape(1, FRAME_SIZE).astype(np.float32)
        ort_inputs = {
            "input": input_tensor,
            "h": self._h,
            "c": self._c,
            "sr": self._sr,
        }
        outputs = self.session.run(None, ort_inputs)

        # 更新状态
        self._h = outputs[1]
        self._c = outputs[2]

        # 输出：output[0] 是 [is_speech_prob] 或 [not_speech, is_speech]
        out = outputs[0]
        if out.shape[-1] == 1:
            return float(out[0][0])
        else:
            return float(out[0][1])  # 取 is_speech 概率

    def is_speech(self, audio: bytes, threshold: float, ratio_threshold: float = 0.3) -> tuple[bool, float]:
        """对一段音频做 VAD 判断，返回 (是否人声, 人声帧占比)

        audio: PCM 16bit 单声道 bytes
        threshold: 人声概率阈值，高于此值判定为 speech 帧
        ratio_threshold: 人声帧占比阈值，超过此比例才判定整段为「有人声」
        """
        # bytes → float32
        pcm = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0

        # 切帧
        self.reset_state()
        speech_frames = 0
        total_frames = 0

        for i in range(0, len(pcm) - FRAME_SIZE + 1, FRAME_SIZE):
            frame = pcm[i : i + FRAME_SIZE]
            if len(frame) < FRAME_SIZE:
                break
            prob = self.process(frame)
            total_frames += 1
            if prob >= threshold:
                speech_frames += 1

        if total_frames == 0:
            return False, 0.0

        ratio = speech_frames / total_frames
        return ratio >= ratio_threshold, ratio
