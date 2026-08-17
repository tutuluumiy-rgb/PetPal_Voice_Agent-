"""ASR Provider：阿里云 Qwen3-ASR-Flash-Realtime（真流式版）

流式架构（每轮说话一个 WebSocket 长连接）：
- speech_start → start_streaming：建立 WS 连接 + 后台线程，注册增量回调
- 说话期间 feed：音频块入队，后台线程实时 input_audio_buffer.append 发送
- 服务端边识别边返回 partial（stash 字段，全量修订型：每次是「当前整句假设」，会改写前文）
- speech_end → finalize：发送 commit → 收 completed → 返回最终文本 → 关连接

partial 行为（实测确认）：
- 事件 conversation.item.input_audio_transcription.text 的 【stash】 字段是增量文本
- 是全量修订：stash = 当前整句（"从五" → "从无数" → "从五数"），前端展示需【覆盖】而非追加
- completed 事件的 transcript 字段是最终稳定文本

API Key 优先用 ASR_API_KEY，否则回退 DASHSCOPE_API_KEY。
"""

import os
import json
import base64
import queue
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

# 单次 finalize 等待完成的最长时间（秒）
FINALIZE_TIMEOUT = 20


class AliyunASR(ASRProvider):
    """真流式识别器：边说话边发云端，partial 实时回调，finalize 时 commit 收最终文本"""

    def __init__(self):
        if not ASR_API_KEY:
            raise RuntimeError("未配置 ASR_API_KEY 或 DASHSCOPE_API_KEY，请在 backend/.env 里填写")
        self._sessions = {}  # session_id -> {queue, on_partial, done, final_text, ws}

    # ── 接口实现 ──────────────────────────────────────────
    def start_streaming(self, session_id: str, on_partial):
        """开始新一轮流式识别：建 WS 连接 + 后台线程发送队列"""
        sess = {
            "queue": queue.Queue(),      # ("audio", pcm) / ("commit", None) / ("close", None)
            "on_partial": on_partial,
            "done": threading.Event(),
            "final_text": {},
            "ws": None,
        }
        self._sessions[session_id] = sess
        t = threading.Thread(target=self._run_ws, args=(session_id, sess), daemon=True)
        t.start()

    def feed(self, session_id: str, pcm: bytes):
        """喂入音频（实时入队，后台线程发送给云端）"""
        sess = self._sessions.get(session_id)
        if sess:
            sess["queue"].put(("audio", pcm))

    async def finalize(self, session_id: str) -> str:
        """用户说完：commit 提交识别，返回最终文本（等待 completed）"""
        sess = self._sessions.pop(session_id, None)
        if sess is None:
            return ""
        sess["queue"].put(("commit", None))

        import asyncio
        await asyncio.to_thread(sess["done"].wait, FINALIZE_TIMEOUT)
        # 关闭连接
        try:
            sess["queue"].put(("close", None))
        except Exception:
            pass
        return sess["final_text"].get("text", "")

    def reset(self, session_id: str):
        """清理会话（打断/误报撤销）：关闭连接"""
        sess = self._sessions.pop(session_id, None)
        if sess:
            try:
                sess["queue"].put(("close", None))
            except Exception:
                pass

    # ── 内部：WS 后台线程 ──────────────────────────────────
    def _run_ws(self, session_id: str, sess: dict):
        connected = threading.Event()  # WS 连接建立后再开始发送

        def on_open(ws):
            # 会话配置：pcm 16k，服务端 VAD 关闭（我们主动提交端点）
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
            connected.set()  # 连接已建立，允许发送

        def on_message(ws, message):
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                return
            msg_type = data.get("type", "")

            # 增量文本：stash 字段 = 当前整句假设（全量修订型，覆盖展示）
            if msg_type == "conversation.item.input_audio_transcription.text":
                stash = data.get("stash", "")
                if stash and sess.get("on_partial"):
                    try:
                        sess["on_partial"](stash)
                    except Exception:
                        pass

            # 最终完整文本
            elif msg_type == "conversation.item.input_audio_transcription.completed":
                sess["final_text"]["text"] = data.get("transcript", "")
                sess["done"].set()

            elif msg_type == "error":
                print(f"[ASR错误] {json.dumps(data, ensure_ascii=False)[:300]}")
                sess["done"].set()

        def on_error(ws, error):
            print(f"[ASR WS错误] {error}")
            sess["done"].set()

        def on_close(ws, code, msg):
            sess["done"].set()

        ws = websocket.WebSocketApp(
            WS_URL,
            header=["Authorization: Bearer " + ASR_API_KEY, "OpenAI-Beta: realtime=v1"],
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        sess["ws"] = ws

        def _run_forever():
            ws.run_forever()
            # 连接关闭后确保 done（防止 finalize 卡死）
            sess["done"].set()

        wst = threading.Thread(target=_run_forever, daemon=True)
        wst.start()

        # 等待连接建立（最多 10s），失败则结束
        if not connected.wait(timeout=10):
            print(f"[ASR] {session_id} 连接超时")
            return

        # 主循环：消费队列 → 发送
        while True:
            try:
                kind, payload = sess["queue"].get(timeout=30)
            except queue.Empty:
                # 30s 无消息，认为会话超时关闭
                try:
                    ws.close()
                except Exception:
                    pass
                break

            if kind == "audio":
                try:
                    encoded = base64.b64encode(payload).decode("utf-8")
                    evt = {
                        "event_id": "evt_" + str(int(time.time() * 1000)),
                        "type": "input_audio_buffer.append",
                        "audio": encoded,
                    }
                    ws.send(json.dumps(evt))
                except Exception as e:
                    print(f"[ASR] 发送失败: {e}")
                    break
            elif kind == "commit":
                try:
                    ws.send(json.dumps({"event_id": "event_commit", "type": "input_audio_buffer.commit"}))
                except Exception as e:
                    print(f"[ASR] commit 失败: {e}")
                    break
            elif kind == "close":
                try:
                    ws.close()
                except Exception:
                    pass
                break
