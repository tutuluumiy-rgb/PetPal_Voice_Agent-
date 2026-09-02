"""一次性探针：复现「session.update 后立即 append → commit」竞态（阿里云 Qwen3-ASR-Realtime）

对照两组时序：
  A. update 后【立即】append(preRoll) + commit（当前 asr.py 的实际时序）
  B. update 后【等 0.6s】（服务端已确认会话）再 append + commit
观察是否 A 触发 "Error committing input audio buffer, maybe no invalid audio stream"。

用法：cd backend && python scripts/probe_asr_commit.py
"""
import base64
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import websocket  # websocket-client

from dotenv import load_dotenv
load_dotenv()

ASR_MODEL = os.getenv("ASR_MODEL", "qwen3-asr-flash-realtime")
ASR_BASE_URL = os.getenv("ASR_BASE_URL", "wss://dashscope.aliyuncs.com/api-ws/v1/realtime")
WS_URL = f"{ASR_BASE_URL}?model={ASR_MODEL}"
ASR_API_KEY = os.getenv("ASR_API_KEY") or os.getenv("DASHSCOPE_API_KEY")

# 256ms / 16k / mono / 16bit 静音 PCM（等价 preRoll 大小）
PCM = b"\x00" * 8192


def run_case(name: str, wait_after_update: float):
    print(f"\n===== 时序 {name}（update 后等 {wait_after_update}s 再 append）=====")
    events: list[str] = []
    done = {"ok": False}

    def on_open(ws):
        session_update = {
            "event_id": "event_conf",
            "type": "session.update",
            "session": {
                "modalities": ["text"],
                "input_audio_format": "pcm",
                "sample_rate": 16000,
                "input_audio_transcription": {"language": "zh"},
                "turn_detection": None,
            },
        }
        ws.send(json.dumps(session_update))
        events.append("sent: session.update")

        if wait_after_update > 0:
            time.sleep(wait_after_update)

        append = {
            "event_id": "evt_append_probe",
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(PCM).decode(),
        }
        ws.send(json.dumps(append))
        events.append("sent: input_audio_buffer.append(8192B)")
        time.sleep(0.2)
        ws.send(json.dumps({"event_id": "evt_commit_probe", "type": "input_audio_buffer.commit"}))
        events.append("sent: input_audio_buffer.commit")

    def on_message(ws, raw):
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            return
        t = d.get("type", "?")
        if t in ("error", "conversation.item.input_audio_transcription.completed",
                 "session.created", "conversation.created", "response.done"):
            events.append(f"recv: {t} :: {str(d.get('error') or d.get('transcript') or '')[:120]}")
        if t == "conversation.item.input_audio_transcription.completed":
            done["ok"] = True
            done["text"] = d.get("transcript", "")
            try:
                ws.close()
            except Exception:
                pass
        elif t == "error":
            done["ok"] = True
            try:
                ws.close()
            except Exception:
                pass

    def on_error(ws, error):
        events.append(f"ws-error: {error}")
        done["ok"] = True
        try:
            ws.close()
        except Exception:
            pass

    ws = websocket.WebSocketApp(
        WS_URL,
        header=["Authorization: Bearer " + ASR_API_KEY, "OpenAI-Beta: realtime=v1"],
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
    )
    ws.run_forever()
    # 收尾：再等一小会儿让 error/completed 回来
    time.sleep(2.5)
    try:
        ws.close()
    except Exception:
        pass
    for e in events:
        print("  " + e)
    err = [e for e in events if e.startswith("recv: error")]
    return err, done


if __name__ == "__main__":
    if not ASR_API_KEY:
        print("未配置 ASR key")
        sys.exit(2)
    for label, wait in (("A 立即 append（当前实现时序）", 0.0), ("B 等 0.6s 再 append", 0.6)):
        errs, done = run_case(label, wait)
        status = "FAIL(触发服务端错误)" if errs else ("OK" if done.get("ok") else "超时/无事件")
        print(f"  ==> {label}: {status}  transcript={done.get('text') or ''!r}")