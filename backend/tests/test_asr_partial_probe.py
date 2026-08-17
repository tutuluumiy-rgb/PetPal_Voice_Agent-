"""实验：验证 qwen3-asr-flash-realtime 的 partial 行为（流式提交时）

回答两个问题：
1. 边发音频边（不 commit / 延迟 commit）时，服务端是否返回 partial（transcription.text）事件？
2. partial 的 transcript 是「增量(delta)」还是「全量(full)」？会不会修订之前已输出的文字？

方法：把一段 TTS 音频分块流式发送，逐步观察返回事件。

用法：
  cd backend
  python tests\test_asr_partial_probe.py
"""

import asyncio
import base64
import json
import os
import sys
import threading
import time

import websocket  # websocket-client

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

ASR_API_KEY = os.getenv("ASR_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
ASR_MODEL = os.getenv("ASR_MODEL", "qwen3-asr-flash-realtime")
WS_URL = f"wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model={ASR_MODEL}"
SAMPLE_RATE = 16000


async def synth_16k(tts, text: str) -> bytes:
    import numpy as np

    chunks = []
    async for c in tts.synth_stream(text):
        chunks.append(c)
    pcm24 = b"".join(chunks)
    data = np.frombuffer(pcm24, dtype=np.int16).astype(np.float32)
    n_out = int(len(data) * 16000 / 24000)
    x_old = np.linspace(0, 1, len(data))
    x_new = np.linspace(0, 1, n_out)
    return np.interp(x_new, x_old, data).astype(np.int16).tobytes()


def probe(audio: bytes):
    """流式发音频，收集所有返回事件，分析 partial 行为"""
    events = []
    done = threading.Event()

    def on_open(ws):
        cfg = {
            "event_id": "event_conf",
            "type": "session.update",
            "session": {
                "modalities": ["text"],
                "input_audio_format": "pcm",
                "sample_rate": SAMPLE_RATE,
                "input_audio_transcription": {"language": "zh"},
                "turn_detection": None,
            },
        }
        ws.send(json.dumps(cfg))
        # 分块流式发送（200ms/块），不立即 commit，观察是否出 partial
        chunk_bytes = int(SAMPLE_RATE * 0.2 * 2)
        for i in range(0, len(audio), chunk_bytes):
            chunk = audio[i:i + chunk_bytes]
            evt = {
                "event_id": "evt_" + str(int(time.time() * 1000)),
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(chunk).decode(),
            }
            ws.send(json.dumps(evt))
            time.sleep(0.05)  # 留时间让服务端处理/返回
        # 发完后 commit
        ws.send(json.dumps({"event_id": "event_commit", "type": "input_audio_buffer.commit"}))

    def on_message(ws, message):
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return
        events.append(data)
        t = data.get("type", "?")
        # 打印所有事件的关键字段（排查为什么 transcript 为空）
        if t in ("conversation.item.input_audio_transcription.text",
                 "conversation.item.input_audio_transcription.completed",
                 "error"):
            print(f"  [事件] {t}")
            if t == "conversation.item.input_audio_transcription.text":
                print(f"         transcript: {data.get('transcript', '')!r}")
                print(f"         原始: {json.dumps(data, ensure_ascii=False)[:300]}")
            elif t == "conversation.item.input_audio_transcription.completed":
                print(f"         原始: {json.dumps(data, ensure_ascii=False)[:600]}")
            elif t == "error":
                print(f"         error: {json.dumps(data, ensure_ascii=False)[:300]}")
        if t in ("conversation.item.input_audio_transcription.completed", "error", "response.done"):
            done.set()

    def on_error(ws, error):
        print(f"  [WS错误] {error}")
        done.set()

    def on_close(ws, code, msg):
        done.set()

    ws = websocket.WebSocketApp(
        WS_URL,
        header=["Authorization: Bearer " + ASR_API_KEY, "OpenAI-Beta: realtime=v1"],
        on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close,
    )
    wst = threading.Thread(target=ws.run_forever, daemon=True)
    wst.start()
    done.wait(timeout=20)
    try:
        ws.close()
    except Exception:
        pass
    return events


async def main():
    from providers.tts import AliyunTTS

    tts = AliyunTTS()
    audio = await synth_16k(tts, "从五数到十今天天气怎么样")
    print(f"音频 {len(audio)/2/SAMPLE_RATE*1000:.0f}ms，开始流式探测 partial…")
    print("=" * 60)
    events = probe(audio)

    # 分析 partial 事件
    partials = [e for e in events if e.get("type") == "conversation.item.input_audio_transcription.text"]
    finals = [e for e in events if e.get("type") == "conversation.item.input_audio_transcription.completed"]
    print("=" * 60)
    print(f"partial 事件数: {len(partials)}")
    print(f"final 事件数: {len(finals)}")
    if partials:
        print("\npartial 序列（看是否修订/增量/全量）:")
        for i, p in enumerate(partials):
            print(f"  partial#{i}: {p.get('transcript', '')!r}")


if __name__ == "__main__":
    asyncio.run(main())
