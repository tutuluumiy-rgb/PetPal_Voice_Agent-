"""ASR Provider：阿里云 Qwen3-ASR-Flash-Realtime（迁自 asr_engine.py）

采用「攒整句 + 一次性识别 + 增量展示」模式：
- 用户说话时，音频累积在本地（不发云端）
- 用户说完（端点检测触发），一次性发云端识别
- 云端返回增量 text 时，通过 on_partial 回调实时展示（流式效果）
- completed 时返回最终完整文本

API Key 优先用 ASR_API_KEY，否则回退 DASHSCOPE_API_KEY。
"""

import os
import json
import base64
import threading
import time

import websocket  # websocket-client

from dotenv import load_dotenv

from .base import ASRProvider

load_dotenv()

ASR_MODEL = os.getenv("ASR_MODEL", "qwen3-asr-flash-realtime")
ASR_API_KEY = os.getenv("ASR_API_KEY") or os.getenv("DASHSCOPE_API_KEY")

SAMPLE_RATE = 16000
WS_URL = f"wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model={ASR_MODEL}"


class AliyunASR(ASRProvider):
    """流式识别器：累积音频，finalize 时识别并实时回调增量文本"""

    def __init__(self):
        if not ASR_API_KEY:
            raise RuntimeError("未配置 ASR_API_KEY 或 DASHSCOPE_API_KEY，请在 backend/.env 里填写")
        self._sessions = {}  # session_id -> bytearray（累积的音频）

    def start_streaming(self, session_id: str, on_partial):
        """开始新一轮识别会话：注册增量回调，准备累积音频"""
        self._sessions[session_id] = {
            "audio": bytearray(),
            "on_partial": on_partial,
        }

    def feed(self, session_id: str, pcm: bytes):
        """喂入音频（同步累积，识别时一次性发云端）"""
        sess = self._sessions.get(session_id)
        if sess:
            sess["audio"].extend(pcm)

    async def finalize(self, session_id: str) -> str:
        """用户说完：把累积音频一次性发云端识别，返回最终文本"""
        sess = self._sessions.pop(session_id, None)
        if sess is None:
            return ""
        audio = bytes(sess["audio"])
        on_partial = sess.get("on_partial")
        if len(audio) < int(SAMPLE_RATE * 0.3 * 2):  # 少于 0.3 秒，忽略
            return ""

        import asyncio
        result = await asyncio.to_thread(_recognize_sync, audio, on_partial)
        return result

    def reset(self, session_id: str):
        self._sessions.pop(session_id, None)


def _recognize_sync(audio: bytes, on_partial) -> str:
    """同步识别一段音频（在后台线程里调用），走原生 WebSocket"""
    final_text = ""
    done = threading.Event()

    def on_open(ws):
        # 会话配置：pcm 16k，服务端 VAD 关闭（我们主动提交）
        event = {
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
        ws.send(json.dumps(event))

        # 分块发送音频
        chunk_size = 3200  # 100ms 一包
        for i in range(0, len(audio), chunk_size):
            chunk = audio[i : i + chunk_size]
            encoded = base64.b64encode(chunk).decode("utf-8")
            evt = {
                "event_id": "event_" + str(int(time.time() * 1000)),
                "type": "input_audio_buffer.append",
                "audio": encoded,
            }
            ws.send(json.dumps(evt))

        # 提交音频，触发识别
        commit = {"event_id": "event_commit", "type": "input_audio_buffer.commit"}
        ws.send(json.dumps(commit))

    def on_message(ws, message):
        nonlocal final_text
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return
        msg_type = data.get("type", "")

        # 增量文本：实时展示
        if msg_type == "conversation.item.input_audio_transcription.text":
            delta = data.get("transcript", "")
            if delta and on_partial:
                try:
                    on_partial(delta)  # 注意：这里传的是 delta，前端累积
                except Exception:
                    pass

        # 最终完整文本
        elif msg_type == "conversation.item.input_audio_transcription.completed":
            text = data.get("transcript", "")
            if not text:
                item = data.get("item", {})
                content = item.get("content", [])
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("transcript"):
                            text = c["transcript"]
                            break
                elif isinstance(content, dict) and content.get("transcript"):
                    text = content["transcript"]
            final_text = text
            done.set()

        elif msg_type == "error":
            print(f"[ASR错误] {json.dumps(data, ensure_ascii=False)[:300]}")
            done.set()

    def on_error(ws, error):
        print(f"[ASR WS错误] {error}")
        done.set()

    def on_close(ws, code, msg):
        done.set()

    ws = websocket.WebSocketApp(
        WS_URL,
        header=[
            "Authorization: Bearer " + ASR_API_KEY,
            "OpenAI-Beta: realtime=v1",
        ],
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )

    wst = threading.Thread(target=ws.run_forever, daemon=True)
    wst.start()

    done.wait(timeout=15)
    try:
        ws.close()
    except Exception:
        pass

    return final_text
