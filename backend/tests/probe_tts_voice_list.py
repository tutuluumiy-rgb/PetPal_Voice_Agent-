"""探测 Qwen3-TTS-Realtime 当前模型下可用的全部音色。

绕过 providers/voice_catalog（避免目录 fallback 干扰），直接调底层 QwenTtsRealtime，
对候选 voice 逐个合成一句，成功出音频 = 可用。

用法: python tests/probe_tts_voice_list.py [模型名]
"""
from __future__ import annotations

import asyncio
import base64
import os
import queue
import sys
import threading

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dashscope  # noqa: E402
from dashscope.audio.qwen_tts_realtime import (  # noqa: E402
    AudioFormat,
    QwenTtsRealtime,
    QwenTtsRealtimeCallback,
)

dashscope.api_key = os.getenv("DASHSCOPE_API_KEY")
MODEL = sys.argv[1] if len(sys.argv) > 1 else os.getenv("TTS_MODEL", "qwen3-tts-instruct-flash-realtime")

# 候选音色（官方文档常见 id + 补充猜测；实测通过的才进 catalog）
CANDIDATES = [
    "Cherry", "Serena", "Chelsie", "Ethan", "Mochi",
    "Lelian", "Amber", "Vinnie", "Harlem", "Lucy",
    "Claudia", "Fiona", "Oliver", "Ryan", "Nick",
    "Danny", "Wenjie", "Xiaozhi", "Cherry2", "Senbon",
]

BYTES_PER_SEC = 48000  # 24kHz 16bit 单声道


def synth_once(voice: str) -> str:
    audio_q: queue.Queue[bytes] = queue.Queue()
    done = threading.Event()
    errors: list[str] = []

    class CB(QwenTtsRealtimeCallback):
        def on_open(self):
            pass

        def on_event(self, message):
            if isinstance(message, dict):
                mtype = message.get("type", "")
                if mtype == "response.audio.delta" and message.get("delta"):
                    try:
                        audio_q.put(base64.b64decode(message["delta"]))
                    except Exception:
                        pass
                elif "done" in mtype or mtype == "error":
                    done.set()

        def on_close(self):
            done.set()

    tts = QwenTtsRealtime(model=MODEL, callback=CB())

    def _run():
        try:
            tts.connect()
            tts.update_session(
                voice=voice,
                response_format=AudioFormat.PCM_24000HZ_MONO_16BIT,
                instructions="用自然温和的语气说",
            )
            tts.append_text("你好呀，我是岁岁，欢迎回家")
            tts.commit()
            done.wait(timeout=8)
            tts.close()
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))
            done.set()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    chunks: list[bytes] = []
    while not done.is_set() or not audio_q.empty():
        try:
            chunks.append(audio_q.get(timeout=0.25))
        except queue.Empty:
            continue
    if errors:
        return f"ERROR {errors[0][:80]}"
    if not chunks:
        return "FAIL(无音频)"
    dur = sum(len(c) for c in chunks) / BYTES_PER_SEC
    return f"OK {dur:.1f}s"


def main():
    print(f"模型: {MODEL}   候选 {len(CANDIDATES)} 个音色\n")
    ok: list[tuple[str, str]] = []
    for v in CANDIDATES:
        r = synth_once(v)
        mark = "[OK] " if r.startswith("OK") else "[--] "
        print(f"{mark}{v:<10} {r}", flush=True)
        if r.startswith("OK"):
            ok.append((v, r))
        asyncio.run(asyncio.sleep(0.4))  # 温和一点，避免限流
    print(f"\n可用音色 {len(ok)} 个: {', '.join(v for v, _ in ok)}")


if __name__ == "__main__":
    main()