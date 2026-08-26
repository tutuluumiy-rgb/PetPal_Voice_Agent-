"""
一键脚本：上传音频 + 调用 MiniMax /v1/voice_clone 克隆音色。

对应官方文档的"完整示例"：
https://platform.minimaxi.com/docs/guides/speech-voice-clone

相对官方示例，本脚本修正 / 补齐：
  * 修复了 "headers / clone_payload" 变量名混用 bug
  * 补齐每次 requests 后的 raise_for_status
  * 自动从响应里下载试听音频到 ./audios/cloned_preview.mp3
  * 通过 CLI 参数 或 .env 配置所有可选项
  * 抽离出可被外部 import 的函数 clone_voice()

运行：
    python clone_voice.py \\
        --clone-audio ./audios/clone_input.mp3 \\
        --prompt-audio ./audios/clone_prompt.mp3 \\
        --prompt-text "后来认为啊，是有人抓这鸡，可是抓鸡的地方呢没人听过鸡叫。" \\
        --voice-id my_voice_01 \\
        --text "大兄弟，听您口音不是本地人吧..." \\
        --model speech-2.8-hd

环境变量（可选，缺省走命令行默认值）：
    MINIMAX_API_KEY    MiniMax 开放平台 API Key（必填）
    CLONE_AUDIO_PATH   默认复刻音频路径
    PROMPT_AUDIO_PATH  默认参考音频路径
    PROMPT_TEXT        默认参考音频对应文字
    PREVIEW_TEXT       默认试听文本
    VOICE_ID           默认 voice_id
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import requests

UPLOAD_URL = "https://api.minimaxi.com/v1/files/upload"
CLONE_URL = "https://api.minimaxi.com/v1/voice_clone"
DEFAULT_MODEL = "speech-2.8-hd"

DEFAULT_PROMPT_TEXT = "后来认为啊，是有人抓这鸡，可是抓鸡的地方呢没人听过鸡叫。"
DEFAULT_PREVIEW_TEXT = (
    "大兄弟，听您口音不是本地人吧，头回来天津卫，啊，"
    "待会您可甭跟着导航走，那玩意儿净给您往大马路上绕。"
)
DEFAULT_VOICE_ID = "my_custom_voice_001"

ALLOWED_EXTS = {".mp3", ".m4a", ".wav"}
MAX_FILE_BYTES = 20 * 1024 * 1024  # 20MB


def _ensure_api_key() -> str:
    api_key = os.getenv("MINIMAX_API_KEY")
    if not api_key:
        sys.stderr.write(
            "[ERROR] 缺少环境变量 MINIMAX_API_KEY。\n"
            "        在控制台 https://platform.minimaxi.com 获取后写入 .env 或 export。\n"
        )
        sys.exit(2)
    return api_key


def _validate_audio(path: Path, *, kind: str) -> None:
    """kind ∈ {'clone', 'prompt'}，分别校验时长约束。"""
    if not path.exists():
        sys.stderr.write(f"[ERROR] 文件不存在：{path}\n")
        sys.exit(2)
    if path.suffix.lower() not in ALLOWED_EXTS:
        sys.stderr.write(
            f"[ERROR] 音频格式 {path.suffix} 不支持，仅接受 {sorted(ALLOWED_EXTS)}\n"
        )
        sys.exit(2)
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        sys.stderr.write(
            f"[ERROR] 文件大小 {size / 1024 / 1024:.2f}MB 超过 {MAX_FILE_BYTES // 1024 // 1024}MB 上限。\n"
        )
        sys.exit(2)
    if kind == "prompt":
        print("[WARN] 请确认参考音频时长 < 8 秒（MiniMax 硬约束）。")


def _upload_file(audio_path: Path, purpose: str, api_key: str) -> str:
    headers = {"Authorization": f"Bearer {api_key}"}
    with audio_path.open("rb") as fp:
        files = {"file": (audio_path.name, fp)}
        data = {"purpose": purpose}
        response = requests.post(
            UPLOAD_URL,
            headers=headers,
            data=data,
            files=files,
            timeout=60,
        )
    response.raise_for_status()
    body = response.json()
    file_id = body.get("file", {}).get("file_id")
    if not file_id:
        raise RuntimeError(f"上传 {purpose} 失败，缺少 file_id：{body}")
    return file_id


def _download_preview(audio_url: str, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(audio_url, timeout=60, stream=True) as r:
        r.raise_for_status()
        with out_path.open("wb") as fp:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    fp.write(chunk)


def clone_voice(
    clone_audio_path: str | os.PathLike[str],
    *,
    voice_id: str = DEFAULT_VOICE_ID,
    prompt_audio_path: str | os.PathLike[str] | None = None,
    prompt_text: str | None = None,
    text: str = DEFAULT_PREVIEW_TEXT,
    model: str = DEFAULT_MODEL,
    preview_out: str | os.PathLike[str] | None = "./audios/cloned_preview.mp3",
    api_key: str | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """一键上传 + 克隆 + 保存试听音频。

    参数：
        clone_audio_path: 必填，待克隆音频（10s~5min，<=20MB）
        voice_id:         自定义 voice_id，需唯一
        prompt_audio_path: 可选，参考音频（< 8s，<=20MB）
        prompt_text:       可选，参考音频对应的文字
        text:              试听合成文本
        model:             语音模型，默认 speech-2.8-hd
        preview_out:       试听音频保存路径，传 None 则不下载
        api_key:           显式传入 API Key；缺省读 MINIMAX_API_KEY 环境变量

    返回：
        dict 形式的接口响应（其中含 voice_id）。
    """
    api_key = api_key or _ensure_api_key()
    clone_path = Path(clone_audio_path)
    _validate_audio(clone_path, kind="clone")

    print(f"[STEP 1] 上传复刻音频 {clone_path.name} ...")
    file_id = _upload_file(clone_path, purpose="voice_clone", api_key=api_key)
    print(f"[OK]    file_id = {file_id}")

    prompt_file_id: str | None = None
    if prompt_audio_path is not None:
        prompt_path = Path(prompt_audio_path)
        _validate_audio(prompt_path, kind="prompt")
        print(f"[STEP 2] 上传参考音频 {prompt_path.name} ...")
        prompt_file_id = _upload_file(prompt_path, purpose="prompt_audio", api_key=api_key)
        print(f"[OK]    prompt_file_id = {prompt_file_id}")

    if prompt_file_id and not prompt_text:
        raise ValueError("提供了 prompt_audio_path 但缺少 prompt_text，两者必须同时给")

    print(f"[STEP 3] 调用 /v1/voice_clone (voice_id={voice_id}, model={model}) ...")
    clone_payload: dict[str, Any] = {
        "file_id": file_id,
        "voice_id": voice_id,
        "text": text,
        "model": model,
    }
    if prompt_file_id and prompt_text:
        clone_payload["clone_prompt"] = {
            "prompt_audio": prompt_file_id,
            "prompt_text": prompt_text,
        }

    clone_headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    response = requests.post(
        CLONE_URL,
        headers=clone_headers,
        json=clone_payload,
        timeout=timeout,
    )
    response.raise_for_status()
    body: dict[str, Any] = response.json()

    if preview_out is not None:
        preview_url = (
            body.get("audio_url")
            or body.get("demo_audio")
            or body.get("file_url")
            or (body.get("data") or {}).get("audio_url")
        )
        if preview_url:
            _download_preview(preview_url, Path(preview_out))
            print(f"[OK]    试听音频已保存到 {preview_out}")
        else:
            print("[WARN] 响应中未找到试听音频 URL，跳过下载。")

    return body


def main() -> None:
    parser = argparse.ArgumentParser(description="一键克隆 MiniMax 音色")
    parser.add_argument("--clone-audio", required=True, help="待克隆音频路径")
    parser.add_argument("--voice-id", default=os.getenv("VOICE_ID", DEFAULT_VOICE_ID), help="自定义 voice_id")
    parser.add_argument("--prompt-audio", help="可选：参考音频路径（< 8s）")
    parser.add_argument("--prompt-text", default=os.getenv("PROMPT_TEXT", DEFAULT_PROMPT_TEXT))
    parser.add_argument("--text", default=os.getenv("PREVIEW_TEXT", DEFAULT_PREVIEW_TEXT))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--out", default="./audios/cloned_preview.mp3", help="试听音频保存路径")
    args = parser.parse_args()

    body = clone_voice(
        clone_audio_path=args.clone_audio,
        voice_id=args.voice_id,
        prompt_audio_path=args.prompt_audio,
        prompt_text=args.prompt_text if args.prompt_audio else None,
        text=args.text,
        model=args.model,
        preview_out=args.out,
    )
    print("[DONE] 响应：")
    print(json.dumps(body, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()