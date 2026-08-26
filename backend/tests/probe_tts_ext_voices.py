"""快速探测补充音色（官网截图里的 Maia 等）。复用 probe 逻辑，只测候选新增项。"""
from __future__ import annotations

import base64
import os
import queue
import sys
import threading
import time

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
MODEL = os.getenv("TTS_MODEL", "qwen3-tts-instruct-flash-realtime")

# 官网音色表补充候选：已确认 Maia(四月) 可用；这批是"小婉"等音色的可能 id
CANDIDATES = ["Xiaowan", "Wanwan", "Xiaomei", "Mengmeng", "Yiyi", "Nuan", "Xiaoshuang", "Wanxi", "Xiaoran", "Qingqing"]

BYTES_PER_SEC = 48000


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
    return f"OK {sum(len(c) for c in chunks)/BYTES_PER_SEC:.1f}s"


if __name__ == "__main__":
    print(f"模型: {MODEL}   补充候选 {len(CANDIDATES)} 个", flush=True)
    for v in CANDIDATES:
        r = synth_once(v)
        print(f"[{'OK' if r.startswith('OK') else '--'}] {v:<10} {r}", flush=True)
        time.sleep(0.3)