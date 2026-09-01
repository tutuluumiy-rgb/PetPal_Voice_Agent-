"""SmartTurn 端点检测（Endpoint Detection）—— 用户"是否说完话"的话轮完结判定

位置（架构澄清，改造清单#7）：
- 这是【端点检测】内容，不是 barge-in：决定"这句话说完了吗 → 何时提交给 STT/LLM"，所有链路（含打断后收话、正常收话）统一经过。
- Barge-in（打断）是另一个环节（前端 VAD + 后端物理复核 → barge_confirm/reject），不受本模块影响。

模型：pipecat-ai/smart-turn-v3 ONNX（本仓库放置 v3.2-cpu，int8 量化 8.7MB）。
加载 `SMART_TURN_MODEL_PATH`；
- available=False（模型缺失/加载失败）时 judge() 返回 None → 调用方按 fallback 策略处理（默认 direct 提交，等价旧行为）。
- 输入：一段"该说话段"的 16kHz mono 16bit PCM（调用方从事件里提供）；
  内部按上游协议处理：截断/补零为 8 秒（保留尾部）→ Whisper 式 log-mel（80×800，见 whisper_mel.py）。
- 输出：p = 模型对"话轮已完结"的概率（模型输出已是 sigmoid 概率，0~1）。
  p > SMART_TURN_THRESHOLD → 已说完；否则可能未完。

约定：与 Silero VAD 互补——
- Silero VAD：判"语音活动是否暂停"（是否还有人声）；
- SmartTurn ：判"话轮是否完结"（韵律/语义级：尾音、停顿结构、是否像要继续说）。
"""

from __future__ import annotations

import os

try:
    import numpy as _np
    import onnxruntime as _ort
except Exception:  # pragma: no cover - 依赖缺失时整体降级
    _np = None
    _ort = None

from whisper_mel import compute_whisper_log_mel_features


class SmartTurnJudge:
    """SmartTurn-v3 端点判定封装（无模型/加载失败 → available=False，judge 返回 None）"""

    _MODEL_SR = 16000

    def __init__(self, model_path: str, cpu_count: int = 1):
        self.model_path = model_path
        self.available = False
        self.session = None
        self.threshold = 0.5  # 默认阈值；调用方可用 SMART_TURN_THRESHOLD 覆盖配置
        if not model_path or not os.path.exists(model_path):
            return
        if _ort is None:
            return
        try:
            so = _ort.SessionOptions()
            so.execution_mode = _ort.ExecutionMode.ORT_SEQUENTIAL
            so.inter_op_num_threads = 1
            so.intra_op_num_threads = cpu_count
            so.graph_optimization_level = _ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            self.session = _ort.InferenceSession(
                model_path, sess_options=so, providers=["CPUExecutionProvider"],
            )
            self._input_name = self.session.get_inputs()[0].name
            self._output_name = self.session.get_outputs()[0].name
            self.available = True
        except Exception as e:
            print(f"[smart_turn] 模型加载失败（降级为不可用）: {e}")

    def judge(self, pcm: bytes | None) -> float | None:
        """输入 16k mono 16bit PCM → 返回"话轮已完结"概率 p（0~1）；不可用/输入异常返回 None。

        None 语义 = 无法判定（模型缺失 / 无音频 / 推理失败）→ 调用方走 fallback。
        提特征与推理按上游 pipecat smart-turn v3 流程复刻。
        """
        if not self.available or not pcm:
            return None
        if _np is None:
            return None
        try:
            x = _np.frombuffer(pcm, dtype=_np.int16).astype(_np.float32) / 32768.0
            if x.size == 0:
                return None
            log_mel = compute_whisper_log_mel_features(x, do_normalize=True)
            input_features = _np.expand_dims(log_mel, axis=0)  # (1, 80, 800)
            out = self.session.run(None, {self._input_name: input_features})
            v = float(_np.asarray(out[0]).reshape(-1)[0])
            # 上游：ONNX 直接输出 sigmoid 概率；防御性兜底——若明显是 logits 再 sigmoid。
            if v < 0.0 or v > 1.0:
                v = 1.0 / (1.0 + _np.exp(-v))
            return float(_np.clip(v, 0.0, 1.0))
        except Exception as e:
            print(f"[smart_turn] 推理异常（返回 None，降级）: {e}")
            return None

    def say_finished(self, p: float | None, fallback: str = "direct") -> bool:
        """p → 是否判定"已说完"。
        p None（无法判定）→ 按 fallback：direct=认为说完（直接提交）；window=认为未完（开窗等待）。
        """
        if p is None:
            return fallback == "direct"
        return p > self.threshold


class UnavailableJudge(SmartTurnJudge):
    """测试替身：固定输出概率 p（None = 不可用）"""

    def __init__(self, p_fixed: float | None = None):
        super().__init__("")
        self._p = p_fixed

    def judge(self, pcm: bytes | None):
        return self._p

    def say_finished(self, p, fallback="direct"):
        if p is None:
            return fallback == "direct"
        return p > self.threshold