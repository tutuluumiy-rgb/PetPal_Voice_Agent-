"""Whisper 风格 log-Mel 特征提取（纯 numpy，Smart Turn v3 专用）

协议来源：Smart Turn v3（pipecat-ai/smart-turn）的推理流程——
  16k mono float 音频（[-1,1]）→ 截断/补零为 8 秒（保留尾部）→
  Whisper 式 log-mel（80 频带 × 800 帧）→ 喂给 ONNX `input_features`。

本文件把 `transformers.WhisperFeatureExtractor(chunk_length=8)` 的数学
复刻为纯 numpy（与 pipecat 仓库 `_whisper_features.py` 的 vendored 实现
逐位对齐：Slaney mel 滤波器组、periodic Hann、反射填充、z-score 波形归一、
log10 动态范围压缩、(x+4)/4 缩放）。上游许可：BSD-2 / Apache-2.0。
目的：运行时不依赖 transformers/torch 的版本行为。

对外只暴露 `compute_whisper_log_mel_features(audio, do_normalize=True)`
（返回 float32，形状 (80, 800)）。
"""

from __future__ import annotations

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

_N_FFT = 400
_HOP_LENGTH = 160
_N_MELS = 80
_SAMPLING_RATE = 16000
_MEL_FLOOR = 1e-10
_NORM_VARIANCE_EPS = 1e-7


def _hertz_to_mel_slaney(freq: np.ndarray) -> np.ndarray:
    """Hz → mel（Slaney 刻度）"""
    min_log_hertz = 1000.0
    min_log_mel = 15.0
    logstep = 27.0 / np.log(6.4)
    freq = np.atleast_1d(np.asarray(freq, dtype=np.float64))
    mels = 3.0 * freq / 200.0
    log_region = freq >= min_log_hertz
    mels[log_region] = min_log_mel + np.log(freq[log_region] / min_log_hertz) * logstep
    return mels


def _mel_to_hertz_slaney(mels: np.ndarray) -> np.ndarray:
    """mel → Hz（Slaney 刻度）"""
    min_log_hertz = 1000.0
    min_log_mel = 15.0
    logstep = np.log(6.4) / 27.0
    mels = np.atleast_1d(np.asarray(mels, dtype=np.float64))
    freq = 200.0 * mels / 3.0
    log_region = mels >= min_log_mel
    freq[log_region] = min_log_hertz * np.exp(logstep * (mels[log_region] - min_log_mel))
    return freq


def _build_mel_filterbank(
    num_frequency_bins: int,
    num_mel_filters: int,
    min_frequency: float,
    max_frequency: float,
    sampling_rate: int,
) -> np.ndarray:
    """Slaney 归一化三角 mel 滤波器组（返回 (num_frequency_bins, num_mel_filters)）"""
    mel_min = float(_hertz_to_mel_slaney(np.array([min_frequency], dtype=np.float64))[0])
    mel_max = float(_hertz_to_mel_slaney(np.array([max_frequency], dtype=np.float64))[0])
    mel_freqs = np.linspace(mel_min, mel_max, num_mel_filters + 2)
    filter_freqs = _mel_to_hertz_slaney(mel_freqs)
    fft_freqs = np.linspace(0, sampling_rate // 2, num_frequency_bins)

    filter_diff = np.diff(filter_freqs)
    slopes = np.expand_dims(filter_freqs, 0) - np.expand_dims(fft_freqs, 1)
    down_slopes = -slopes[:, :-2] / filter_diff[:-1]
    up_slopes = slopes[:, 2:] / filter_diff[1:]
    mel_filters = np.maximum(np.zeros(1), np.minimum(down_slopes, up_slopes))

    # Slaney 面积归一
    enorm = 2.0 / (filter_freqs[2 : num_mel_filters + 2] - filter_freqs[:num_mel_filters])
    mel_filters *= np.expand_dims(enorm, 0)
    return mel_filters


def _periodic_hann_window(window_length: int) -> np.ndarray:
    """periodic Hann（与 torch.hann_window 一致）"""
    return np.hanning(window_length + 1)[:-1]


_HANN_WINDOW = _periodic_hann_window(_N_FFT)
_MEL_FILTERS = _build_mel_filterbank(
    num_frequency_bins=_N_FFT // 2 + 1,
    num_mel_filters=_N_MELS,
    min_frequency=0.0,
    max_frequency=_SAMPLING_RATE / 2.0,
    sampling_rate=_SAMPLING_RATE,
)


def _power_spectrogram(
    waveform: np.ndarray,
    window: np.ndarray,
    frame_length: int,
    hop_length: int,
) -> np.ndarray:
    """居中功率谱（反射填充、real-FFT、|·|²）。返回 (num_frequency_bins, num_frames)"""
    pad = frame_length // 2
    padded = np.pad(waveform.astype(np.float64), (pad, pad), mode="reflect")
    win = window.astype(np.float64)
    windows = sliding_window_view(padded, frame_length)[::hop_length]
    spec = np.fft.rfft(windows * win, axis=-1)
    return (np.abs(spec) ** 2).T


def _truncate_or_pad_8s(audio: np.ndarray) -> np.ndarray:
    """对齐上游 truncate_audio_to_last_n_seconds：超过 8s 保留尾部，不足 8s 前补零。"""
    n_samples = _SAMPLING_RATE * 8  # 128000
    if audio.size > n_samples:
        return audio[-n_samples:]
    if audio.size < n_samples:
        return np.pad(audio, (n_samples - audio.size, 0), mode="constant")
    return audio


def compute_whisper_log_mel_features(
    audio: np.ndarray,
    *,
    do_normalize: bool = True,
) -> np.ndarray:
    """16k mono float 音频（任意长度或精确 128000 样本）→ (80, 800) float32 log-mel。

    与 pipecat v3 流程一致：先按"保留尾部"补/截成 8 秒，再做
    Whisper 特征提取（do_normalize=True 时先对波形 z-score 归一）。
    """
    x = np.asarray(audio, dtype=np.float32)
    if x.ndim != 1:
        raise ValueError(f"Expected 1-D audio, got shape {x.shape}")
    x = _truncate_or_pad_8s(x)

    if do_normalize:
        x = (x - x.mean()) / np.sqrt(x.var() + _NORM_VARIANCE_EPS)

    magnitudes = _power_spectrogram(x, _HANN_WINDOW, _N_FFT, _HOP_LENGTH)
    mel_spec = np.maximum(_MEL_FLOOR, _MEL_FILTERS.T @ magnitudes)
    log_spec = np.log10(mel_spec)
    log_spec = log_spec[:, :-1]  # 去掉尾帧，对齐参考实现 → 800 帧
    log_spec = np.maximum(log_spec, log_spec.max() - 8.0)
    log_spec = (log_spec + 4.0) / 4.0
    return log_spec.astype(np.float32)