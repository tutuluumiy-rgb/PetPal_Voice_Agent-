"""
Step 1: 上传待克隆音频到 MiniMax /v1/files/upload

官方文档对应章节："1. 上传复刻音频"
https://platform.minimaxi.com/docs/guides/speech-voice-clone

用途：
    把本地一段长音频（mp3/m4a/wav，10s~5min，<=20MB）上传到 MiniMax，
    返回一个 file_id，作为后续 /v1/voice_clone 接口的输入。

运行：
    # 方式 A：命令行传参
    python upload_clone_audio.py --audio ./audios/clone_input.mp3

    # 方式 B：环境变量传参（见 .env.example）
    CLONE_AUDIO_PATH=./audios/clone_input.mp3 python upload_clone_audio.py

环境变量：
    MINIMAX_API_KEY    MiniMax 开放平台 API Key（必填）
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import requests

UPLOAD_URL = "https://api.minimaxi.com/v1/files/upload"
ALLOWED_EXTS = {".mp3", ".m4a", ".wav"}
MAX_FILE_BYTES = 20 * 1024 * 1024  # 20MB
MIN_DURATION_SEC = 10
MAX_DURATION_SEC = 5 * 60  # 5 分钟


def _ensure_api_key() -> str:
    api_key = os.getenv("MINIMAX_API_KEY")
    if not api_key:
        sys.stderr.write(
            "[ERROR] 缺少环境变量 MINIMAX_API_KEY。\n"
            "        在控制台 https://platform.minimaxi.com 获取后写入 .env 或 export。\n"
        )
        sys.exit(2)
    return api_key


def _validate_audio(audio_path: Path) -> None:
    if not audio_path.exists():
        sys.stderr.write(f"[ERROR] 文件不存在：{audio_path}\n")
        sys.exit(2)
    if audio_path.suffix.lower() not in ALLOWED_EXTS:
        sys.stderr.write(
            f"[ERROR] 音频格式 {audio_path.suffix} 不支持，仅接受 {sorted(ALLOWED_EXTS)}\n"
        )
        sys.exit(2)
    size = audio_path.stat().st_size
    if size > MAX_FILE_BYTES:
        sys.stderr.write(
            f"[ERROR] 文件大小 {size / 1024 / 1024:.2f}MB 超过 {MAX_FILE_BYTES // 1024 // 1024}MB 上限。\n"
        )
        sys.exit(2)


def upload_clone_audio(audio_path: str | os.PathLike[str]) -> str:
    """上传待克隆音频，返回 file_id。

    参数：
        audio_path: 本地音频文件路径（mp3 / m4a / wav，10s~5min，<=20MB）

    返回：
        file_id 字符串，调用 /v1/voice_clone 时作为 file_id 字段使用。

    抛出：
        requests.HTTPError：上传失败
        RuntimeError：响应缺少 file_id
    """
    path = Path(audio_path)
    _validate_audio(path)

    api_key = _ensure_api_key()

    headers = {"Authorization": f"Bearer {api_key}"}
    data = {"purpose": "voice_clone"}

    with path.open("rb") as fp:
        files = {"file": (path.name, fp)}
        response = requests.post(
            UPLOAD_URL,
            headers=headers,
            data=data,
            files=files,
            timeout=60,
        )

    response.raise_for_status()
    payload = response.json()
    file_id = payload.get("file", {}).get("file_id")
    if not file_id:
        raise RuntimeError(f"上传成功但响应缺少 file_id：{payload}")

    return file_id


def main() -> None:
    parser = argparse.ArgumentParser(description="上传待克隆音频到 MiniMax，返回 file_id")
    parser.add_argument(
        "--audio",
        default=os.getenv("CLONE_AUDIO_PATH"),
        help="本地音频路径（默认读环境变量 CLONE_AUDIO_PATH）",
    )
    args = parser.parse_args()

    if not args.audio:
        parser.error("请通过 --audio 或环境变量 CLONE_AUDIO_PATH 指定音频文件路径")

    print(f"[INFO] 上传音频：{args.audio}")
    file_id = upload_clone_audio(args.audio)
    print(f"[OK]   file_id = {file_id}")
    print("[HINT] 请把 file_id 传给 voice_clone.py 或在 clone_voice.py 中使用。")


if __name__ == "__main__":
    main()