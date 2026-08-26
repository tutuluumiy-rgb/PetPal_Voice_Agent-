"""
Step 2 (可选): 上传示例音频 (prompt_audio) 到 MiniMax /v1/files/upload

官方文档对应章节："2. 上传参考音频"
https://platform.minimaxi.com/docs/guides/speech-voice-clone

用途：
    上传一段 < 8 秒的参考音频，与 prompt_text 一一对应，用于增强克隆效果。
    不传也可以克隆，只是音色相似度会下降。

运行：
    python upload_prompt_audio.py --audio ./audios/clone_prompt.mp3
    python upload_prompt_audio.py --audio ./audios/clone_prompt.mp3 --text "参考音频对应的文字"

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
MAX_DURATION_SEC = 8  # prompt_audio 必须 < 8 秒


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


def upload_prompt_audio(audio_path: str | os.PathLike[str]) -> str:
    """上传示例音频，返回 prompt_file_id。

    参数：
        audio_path: 本地音频路径（mp3 / m4a / wav，< 8s，<=20MB）

    返回：
        prompt_file_id 字符串，调用 /v1/voice_clone 时作为 clone_prompt.prompt_audio 字段使用。
    """
    path = Path(audio_path)
    _validate_audio(path)

    api_key = _ensure_api_key()

    headers = {"Authorization": f"Bearer {api_key}"}
    data = {"purpose": "prompt_audio"}

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
    parser = argparse.ArgumentParser(description="上传示例音频 (prompt_audio) 到 MiniMax，返回 file_id")
    parser.add_argument(
        "--audio",
        default=os.getenv("PROMPT_AUDIO_PATH"),
        help="本地参考音频路径（默认读环境变量 PROMPT_AUDIO_PATH）",
    )
    parser.add_argument(
        "--text",
        default=os.getenv("PROMPT_TEXT"),
        help="与音频对应的文字（仅打印提醒，不会随上传接口提交）",
    )
    args = parser.parse_args()

    if not args.audio:
        parser.error("请通过 --audio 或环境变量 PROMPT_AUDIO_PATH 指定音频文件路径")

    if args.text:
        print(f"[INFO] prompt_text：{args.text}")

    print(f"[INFO] 上传参考音频：{args.audio}")
    file_id = upload_prompt_audio(args.audio)
    print(f"[OK]   prompt_file_id = {file_id}")


if __name__ == "__main__":
    main()