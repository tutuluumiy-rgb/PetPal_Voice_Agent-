"""流式 ASR 引擎（阿里云 Qwen3-ASR-Flash-Realtime）

用原生 WebSocket 直连阿里云百炼的实时语音识别接口。
接口走 OpenAI Realtime 协议（参考官方示例）：
- wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=qwen3-asr-flash-realtime
- header: Authorization: Bearer <key>, OpenAI-Beta: realtime=v1
- 事件：session.update（配置）、input_audio_buffer.append（发音频）、
        input_audio_buffer.commit（提交）
- 识别结果在 transcription 事件里返回

API Key 优先用 ASR_API_KEY，否则回退 DASHSCOPE_API_KEY。
"""

import os
import json
import base64
import threading
import time

import websocket  # websocket-client

from dotenv import load_dotenv

load_dotenv()

ASR_MODEL = os.getenv("ASR_MODEL", "qwen3-asr-flash-realtime")
ASR_API_KEY = os.getenv("ASR_API_KEY") or os.getenv("DASHSCOPE_API_KEY")

SAMPLE_RATE = 16000
WS_URL = f"wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model={ASR_MODEL}"


class StreamingASR:
    """流式识别器：持续喂音频，finalize 时返回完整识别结果"""

    def __init__(self):
        if not ASR_API_KEY:
            raise RuntimeError("未配置 ASR_API_KEY 或 DASHSCOPE_API_KEY，请在 backend/.env 里填写")
        self._sessions = {}  # session_id -> 累积的音频

    async def feed(self, session_id: str, pcm: bytes):
        """喂入一段音频（PCM 16bit 16kHz）"""
        if session_id not in self._sessions:
            self._sessions[session_id] = bytearray()
        self._sessions[session_id].extend(pcm)

    async def finalize(self, session_id: str) -> str:
        """用户说完，把累积音频一次性发给云端识别，返回文本"""
        audio = self._sessions.pop(session_id, bytearray())
        if len(audio) < int(SAMPLE_RATE * 0.3 * 2):  # 少于 0.3 秒，忽略
            return ""

        result_holder = {}

        def _run():
            try:
                result_holder["text"] = _recognize_sync(bytes(audio))
            except Exception as e:
                result_holder["error"] = str(e)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=15)

        if "error" in result_holder:
            print(f"[ASR错误] {result_holder['error']}")
            return ""
        return result_holder.get("text", "")

    def reset(self, session_id: str):
        self._sessions.pop(session_id, None)


def _recognize_sync(audio: bytes) -> str:
    """同步识别一段音频（在后台线程里调用），走原生 WebSocket"""
    transcripts = []
    done = threading.Event()

    def on_open(ws):
        # 会话配置：pcm 16k，服务端 VAD 关闭（我们自己攒整句）
        event = {
            "event_id": "event_conf",
            "type": "session.update",
            "session": {
                "modalities": ["text"],
                "input_audio_format": "pcm",
                "sample_rate": SAMPLE_RATE,
                "input_audio_transcription": {
                    "language": "zh",
                },
                "turn_detection": None,  # 关闭服务端VAD，音频由我们主动提交
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
        commit = {
            "event_id": "event_commit",
            "type": "input_audio_buffer.commit",
        }
        ws.send(json.dumps(commit))

    def on_message(ws, message):
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return
        msg_type = data.get("type", "")

        # 提取转录文本
        # Qwen3-ASR 的 text 事件是增量 delta，completed 事件是最终完整文本
        # 只取 completed 的完整文本，避免 delta 拼接错误
        text = None
        if msg_type == "conversation.item.input_audio_transcription.completed":
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
            if text:
                transcripts.append(text)

        # 识别完成信号
        if msg_type == "error":
            print(f"[ASR事件详情] {json.dumps(data, ensure_ascii=False)[:500]}")
            done.set()
        elif msg_type == "conversation.item.input_audio_transcription.completed":
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

    # 在后台线程跑 WebSocket，主线程等 done
    wst = threading.Thread(target=ws.run_forever, daemon=True)
    wst.start()

    # 等待识别完成，最多 15 秒
    done.wait(timeout=15)
    try:
        ws.close()
    except Exception:
        pass

    text = "".join(transcripts).strip()
    return text
