"""
Step 3: 调用 MiniMax /v1/voice_clone 进行音色克隆

官方文档对应章节："3. 进行音色克隆"
https://platform.minimaxi.com/docs/guides/speech-voice-clone

用途：
    基于 Step 1/2 拿到的 file_id 和 prompt_file_id，发起音色克隆。
    成功后会得到一个 voice_id 供后续 T2A 语音合成接口复用。

⚠️ 计费提醒：
    MiniMax 在首次使用 voice_id 进行 T2A 语音合成时收取 9.9 元复刻费。
    试听本身也会按字符数走 T2A 计费。

运行：
    python voice_clone.py \\
        --file-id <step1 返回的 file_id> \\
        --voice-id my_voice_01 \\
        --prompt-file-id <step2 返回的 prompt_file_id> \\
        --prompt-text "参考音频对应的文字" \\
        --text "用新音色合成出来的试听文本" \\
        --model speech-2.8-hd \\
        --out ./audios/cloned_preview.mp3

环境变量：
    MINIMAX_API_KEY    MiniMax 开放平台 API Key（必填）

注意：官方文档示例中有 bug（headers 变量名混用、clone_payload 变量名错乱），
      本文件已修正，并补齐 response.raise_for_status 与异常处理。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import requests

CLONE_URL = "https://api.minimaxi.com/v1/voice_clone"

# 官方文档示例模型；MiniMax 会持续迭代，可按需替换
DEFAULT_MODEL = "speech-2.8-hd"

DEFAULT_PREVIEW_TEXT = (
    "大兄弟，听您口音不是本地人吧，头回来天津卫，啊，"
    "待会您可甭跟着导航走，那玩意儿净给您往大马路上绕。"
)
DEFAULT_PROMPT_TEXT = "后来认为啊，是有人抓这鸡，可是抓鸡的地方呢没人听过鸡叫。"


def _ensure_api_key() -> str:
    api_key = os.getenv("MINIMAX_API_KEY")
    if not api_key:
        sys.stderr.write(
            "[ERROR] 缺少环境变量 MINIMAX_API_KEY。\n"
            "        在控制台 https://platform.minimaxi.com 获取后写入 .env 或 export。\n"
        )
        sys.exit(2)
    return api_key


def clone_voice(
    file_id: str,
    voice_id: str,
    *,
    prompt_file_id: str | None = None,
    prompt_text: str | None = None,
    text: str = DEFAULT_PREVIEW_TEXT,
    model: str = DEFAULT_MODEL,
    out_path: str | os.PathLike[str] | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """调用 /v1/voice_clone 克隆音色。

    参数：
        file_id:        Step 1 上传复刻音频得到的 file_id（必填）
        voice_id:       自定义的音色 ID（必填，需保持唯一）
        prompt_file_id: Step 2 上传参考音频得到的 file_id（可选，但强烈建议）
        prompt_text:    与 prompt_file_id 对应的文字（可选，但强烈建议）
        text:           试听合成文本，将用新音色朗读一次
        model:          语音模型，默认 speech-2.8-hd
        out_path:       如果提供试听音频的可下载 URL，会自动下载到这个路径

    返回：
        dict 形式的接口响应；含 ``voice_id`` 与试听音频字段。
    """
    api_key = _ensure_api_key()

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

    # 试听音频下载（不同版本返回字段名可能不同，做兼容）
    if out_path is not None:
        preview_url = (
            body.get("audio_url")
            or body.get("demo_audio")
            or body.get("file_url")
            or (body.get("data") or {}).get("audio_url")
        )
        if preview_url:
            out = Path(out_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            with requests.get(preview_url, timeout=60, stream=True) as r:
                r.raise_for_status()
                with out.open("wb") as fp:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            fp.write(chunk)
            print(f"[OK]   试听音频已保存到 {out}")
        else:
            print("[WARN] 响应中未找到试听音频 URL，跳过下载。")

    return body


def main() -> None:
    parser = argparse.ArgumentParser(description="调用 MiniMax /v1/voice_clone 克隆音色")
    parser.add_argument("--file-id", required=True, help="Step 1 返回的 file_id")
    parser.add_argument("--voice-id", required=True, help="自定义的音色 ID（唯一）")
    parser.add_argument(
        "--prompt-file-id",
        default=os.getenv("PROMPT_FILE_ID"),
        help="Step 2 返回的 prompt_file_id（可选但推荐）",
    )
    parser.add_argument(
        "--prompt-text",
        default=os.getenv("PROMPT_TEXT") or DEFAULT_PROMPT_TEXT,
        help="与 prompt_file_id 对应的文字（可选但推荐）",
    )
    parser.add_argument("--text", default=DEFAULT_PREVIEW_TEXT, help="试听合成文本")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="语音模型")
    parser.add_argument("--out", default="./audios/cloned_preview.mp3", help="试听音频保存路径")
    args = parser.parse_args()

    print(f"[INFO] 调用 /v1/voice_clone：voice_id={args.voice_id} model={args.model}")
    body = clone_voice(
        file_id=args.file_id,
        voice_id=args.voice_id,
        prompt_file_id=args.prompt_file_id,
        prompt_text=args.prompt_text,
        text=args.text,
        model=args.model,
        out_path=args.out,
    )
    print("[OK]   响应：")
    print(json.dumps(body, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()