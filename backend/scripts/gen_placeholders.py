# -*- coding: utf-8 -*-
"""预生成前端占位音频（Placeholder WAV）。

把这些「固定提示语」用后端现有 TTS 合成一次、落盘成 WAV，
前端在等待/失败/连接异常/唤醒等场景本地播放 —— 不依赖每次运行时云 TTS，
低延迟、可复用、前后端解耦。

输出目录：frontend/renderer/public/placeholders/*.wav（24kHz mono 16bit PCM）
  即前端 renderer 静态资源，前端 PlaceholderPlayer 用 `/placeholders/<key>.wav` 播放。

用法（在 backend 目录）：
    python scripts/gen_placeholders.py            # 全部合成
    python scripts/gen_placeholders.py place         # 只合成指定 key
"""
import asyncio
import os
import struct
import sys

# 让脚本可直接运行（backend/scripts/.. → backend/）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers import get_tts  # noqa: E402

SAMPLE_RATE = 24000
CHANNELS = 1
SAMPLE_WIDTH = 2  # 16-bit

# 输出目录：<仓库根>/frontend/renderer/public/placeholders（脚本在 backend/scripts/，往上 3 层到仓库根）
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FRONTEND_AUDIO_DIR = os.path.join(_REPO_ROOT, "frontend", "renderer", "public", "placeholders")

# ── 占位文案库（key → 文案）────────────────────────
# 前端 PlaceholderPlayer 用 key 索引；文案可在此增删，增改后重跑本脚本即可。
PLACEHOLDERS = {
    # 等待/处理中（工作模式长任务、工具执行）
    "wait_processing": "收到！我正在帮你查～",
    "wait_working": "正在执行任务，请稍等～",
    "wait_almost": "我还在处理，马上就好～",
    # 工具/任务早期反馈
    "tool_start": "好的，这就帮你去办～",
    # 任务失败
    "task_failed": "任务失败了，要不你再试试？",
    "task_retry": "刚刚遇到点小问题，我们再试一次好不好？",
    # 连接异常
    "conn_lost": "连接好像断了，稍等让我重新连一下～",
    "conn_timeout": "后台好像卡住了，我重新连一下～",
    # 唤醒响应（Electron 阶段用）
    "wake_here": "我在呢～",
    "wake_yes": "嗯？我在～",
}


def pcm_to_wav(pcm: bytes, sample_rate: int, channels: int, width: int) -> bytes:
    """PCM bytes → WAV bytes（RIFF）。"""
    data_size = len(pcm)
    byte_rate = sample_rate * channels * width
    block_align = channels * width
    # fmt chunk（16 字节 payload）：PCM(1) / channels / sample_rate / byte_rate / block_align / bits
    fmt = struct.pack(
        "<4sIHHIIHH",
        b"fmt ", 16, 1, channels, sample_rate, byte_rate, block_align, width * 8,
    )
    data = struct.pack("<4sI", b"data", data_size) + pcm
    header = struct.pack("<4sI4sI", b"RIFF", 36 + data_size, b"WAVE", len(fmt) + data_size + 8)
    return header + fmt + data


async def synth_one(tts, key: str, text: str) -> bytes:
    """合成单条占位 → PCM bytes。空结果抛异常。"""
    chunks = []
    # 占位用中性语气（平静），不叠加情绪参数
    params = {"emotion": "平静"}
    async for chunk in tts.synth_stream(text, params):
        chunks.append(chunk)
    pcm = b"".join(chunks)
    if not pcm:
        raise RuntimeError(f"占位「{key}」合成结果为空: {text}")
    return pcm


async def _main(keys: list[str] | None):
    os.makedirs(FRONTEND_AUDIO_DIR, exist_ok=True)
    tts = get_tts()
    selection = keys if keys else list(PLACEHOLDERS)
    for key in selection:
        if key not in PLACEHOLDERS:
            print(f"[skip] 未知 key: {key}")
            continue
        text = PLACEHOLDERS[key]
        dst = os.path.join(FRONTEND_AUDIO_DIR, f"{key}.wav")
        try:
            pcm = await synth_one(tts, key, text)
            wav = pcm_to_wav(pcm, SAMPLE_RATE, CHANNELS, SAMPLE_WIDTH)
            with open(dst, "wb") as f:
                f.write(wav)
            dur = len(pcm) / (SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH)
            print(f"[OK] {key:16s} {len(wav):7d}B  {dur:4.2f}s  <- {text}")
        except Exception as e:
            print(f"[FAIL] {key:16s} {e}")
    print(f"\n输出目录: {FRONTEND_AUDIO_DIR}")


if __name__ == "__main__":
    sel = sys.argv[1:] or None
    asyncio.run(_main(sel))
