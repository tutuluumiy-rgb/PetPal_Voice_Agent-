# -*- coding: utf-8 -*-
"""PetPal Voice Agent — Mock 后端服务器（供前端 Electron 开发界面用）

不连真实数据库/LLM/TTS，只按「前端对接交底协议」（frontend/docs/后端对接交底.md 方案 A）
返回编造的假数据，让前端主进程（WebSocket 客户端）能连上它开发/联调界面。

通用帧（方案 A）：
  客户端→服务端  { "type": "<op>", "id": "c-xxx", ... }
  服务端→客户端  { "type": "<op>:ok", "id": "c-xxx", ... }
                 { "type": "<op>:error", "id": "c-xxx", "code": "E_XXX", "message": "..." }
                 { "type": "<event>", ... }   # 主动事件（无 id）
  流式回复：chat:send:start → chat:send:delta(...) → chat:send:done

支持的命令：auth / ping / mode:get / mode:set / auth:policy:get / auth:policy:set /
  chat:send / chat:abort / history:list / history:search / personality:get / personality:set /
  user:get / user:set / voice:settings:get / voice:settings:set

启动：
  cd backend && python mock_server.py        # 监听 0.0.0.0:9000，/ws
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

app = FastAPI(title="PetPal Mock Backend")

# ── 服务端假状态（内存，不落盘）────────────────────
STATE = {
    "mode": "chat",
    "auth_policy": "ask",
    "personality": (
        "# 人设\n\n你是「宠伴」（PetPal），一只俏皮又贴心的 AI 语音宠物。\n"
        "- 说话带语气词，亲切自然\n- 主动发起话题\n- 关心主人的情绪\n"
    ),
    "user_profile": {
        "basic": {"name": "主人", "role": "owner"},
        "reply_style": "活泼撒娇、话多、偶尔卖萌",
        "likes": ["被摸头", "被夸", "一起看视频"],
        "dislikes": ["熬夜", "被冷落"],
        "daily": {"wake_time": "07:30", "sleep_time": "23:30"},
    },
    "voice": {"volume": 80, "pitch": 50, "voice": "Mochi"},
    "history": [
        {"sessionId": "mock-s1", "time": int(time.time() * 1000) - 60_000, "preview": "你好西西，今天天气怎么样？",
         "msgCount": 6, "runCount": 2},
        {"sessionId": "mock-s2", "time": int(time.time() * 1000) - 600_000, "preview": "帮我整理一下项目需求文档",
         "msgCount": 5, "runCount": 1},
        {"sessionId": "mock-s3", "time": int(time.time() * 1000) - 3600_000, "preview": "讲个笑话给我听听",
         "msgCount": 4, "runCount": 1},
    ],
    "clients": [],  # 所有已连接 ws（用于广播）
}

# 假 TTS 音频（一段极小的 WAV base64，仅供前端解码触发“说话动画”测试）
_FAKE_AUDIO_B64 = (
    "UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA="
)

# chat 假回复（可替换 / 扩展）
_CHAT_REPLIES = {
    "chat": ("【action:blink】好呀主人～我是宠伴，随时在呢！",
             "我听到你说的话啦，虽然现在是开发用的临时回复，等我接上真正的脑子就更好玩啦～"),
    "work": ("【action:wave】收到！我正在按工作模式帮你处理～",
             "我会调用工具来查找资料，稍等一下下，马上回来给你结果。"),
}


async def _send(ws: WebSocket, obj: dict):
    await ws.send_text(json.dumps(obj, ensure_ascii=False))


def _fmt_mock_time(ms: float) -> str:
    """毫秒时间戳 → 'MM-DD HH:mm'（抽屉标题用）"""
    try:
        import datetime
        return datetime.datetime.fromtimestamp(ms / 1000).strftime("%m-%d %H:%M")
    except Exception:
        return "--:--"


async def _stream_reply(ws: WebSocket, sid: str, text: str):
    """按句间隔模拟流式：start → delta* → done。"""
    await _send(ws, {"type": "chat:send:start", "id": sid, "sessionId": "mock-s1"})
    # 拆成 2-3 个 delta（按分号/句号粗略切）
    import re
    parts = [p for p in re.split(r"(?<=[。！？；])", text) if p.strip()]
    if not parts:
        parts = [text]
    for p in parts:
        await asyncio.sleep(0.25)  # 模拟生成延迟
        await _send(ws, {"type": "chat:send:delta", "id": sid, "sessionId": "mock-s1",
                         "text": p.strip(), "action": None})
    await asyncio.sleep(0.3)
    await _send(ws, {
        "type": "chat:send:done", "id": sid, "sessionId": "mock-s1",
        "reply": {"text": "".join(parts), "action": "wave"},
        "audio": _FAKE_AUDIO_B64,
    })
    # 主动事件：TTS 开始/结束（触发前端说话动画）
    await _send(ws, {"type": "tts:start", "sessionId": "mock-s1"})
    await asyncio.sleep(0.1)
    await _send(ws, {"type": "tts:end", "sessionId": "mock-s1"})


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    client_id = "mock-" + uuid.uuid4().hex[:8]
    STATE["clients"].append(ws)
    print(f"[mock] 连接建立 client={client_id}")
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await _send(ws, {"type": "_:error", "id": None, "code": "E_VALIDATION",
                                 "message": "JSON 解析失败"})
                continue
            mtype = msg.get("type", "")
            mid = msg.get("id")

            # ── 握手 / 心跳 ──
            if mtype == "auth":
                await _send(ws, {"type": "auth:ok", "id": mid, "clientId": client_id})
            elif mtype == "ping":
                await _send(ws, {"type": "pong", "id": mid})
            # ── 模式 ──
            elif mtype == "mode:get":
                await _send(ws, {"type": "mode:get:ok", "id": mid, "mode": STATE["mode"]})
            elif mtype == "mode:set":
                m = msg.get("mode")
                if m in ("chat", "work"):
                    STATE["mode"] = m
                    await _send(ws, {"type": "mode:set:ok", "id": mid, "mode": m})
                    for c in STATE["clients"]:
                        try:
                            await _send(c, {"type": "mode:changed", "mode": m})
                        except Exception:
                            pass
                else:
                    await _send(ws, {"type": "mode:set:error", "id": mid, "code": "E_VALIDATION",
                                     "message": "mode 必须是 chat 或 work"})
            # ── 权限策略 ──
            elif mtype == "auth:policy:get":
                await _send(ws, {"type": "auth:policy:get:ok", "id": mid, "policy": STATE["auth_policy"]})
            elif mtype == "auth:policy:set":
                p = msg.get("policy")
                if p in ("full", "ask"):
                    STATE["auth_policy"] = p
                    await _send(ws, {"type": "auth:policy:set:ok", "id": mid, "policy": p})
                else:
                    await _send(ws, {"type": "auth:policy:set:error", "id": mid, "code": "E_VALIDATION",
                                     "message": "policy 必须是 full 或 ask"})
            # ── 对话 / LLM（流式）──
            elif mtype == "chat:send":
                mode = msg.get("mode", STATE["mode"])
                text = msg.get("text", "")
                await _send(ws, {"type": "chat:running", "sessionId": "mock-s1", "running": True})
                replies = _CHAT_REPLIES.get(mode, _CHAT_REPLIES["chat"])
                plan = _stream_reply(ws, mid, " ".join(replies))
                await plan
                await _send(ws, {"type": "chat:running", "sessionId": "mock-s1", "running": False})
                # 追加到 mock-s1 会话（session 粒度：更新该条而非新建）
                row = next((h for h in STATE["history"] if h["sessionId"] == "mock-s1"), None)
                if row:
                    row["time"] = int(time.time() * 1000)
                    row["runCount"] = row.get("runCount", 1) + 1
                    row["msgCount"] = row.get("msgCount", 1) + 1
                else:
                    STATE["history"].insert(0, {
                        "sessionId": "mock-s1", "mode": mode,
                        "time": int(time.time() * 1000), "preview": (text or "")[:40],
                        "msgCount": 1, "runCount": 1,
                    })
            elif mtype == "chat:abort":
                await _send(ws, {"type": "chat:abort:ok", "id": mid, "aborted": True})
                await _send(ws, {"type": "chat:running", "sessionId": "mock-s1", "running": False})
            # ── 历史 ──
            elif mtype == "history:list" or mtype == "history:search":
                page = int(msg.get("page", 1) or 1)
                pageSize = int(msg.get("pageSize", 20) or 20)
                keyword = (msg.get("keyword", "") or "").strip()
                items = STATE["history"]
                if keyword:
                    items = [h for h in items if keyword in h["preview"]]
                total = len(items)
                start = (page - 1) * pageSize
                page_items = items[start:start + pageSize]
                await _send(ws, {"type": "history:list:ok", "id": mid,
                                 "items": page_items, "total": total, "page": page})
            elif mtype == "history:detail":
                # 按 sessionId 构造假事件流（两轮 run，演示按轮分组；真实后端见 mgmt_api）
                sid = msg.get("sessionId") or ""
                item = next((h for h in STATE["history"] if h["sessionId"] == sid), None)
                if item is None and STATE["history"]:
                    item = STATE["history"][0]
                base_ts = (item or {}).get("time", int(time.time() * 1000)) / 1000
                preview = (item or {}).get("preview", "你好西西，今天天气怎么样？")
                run_a = {"ts": base_ts, "runId": "run-a", "kind": "user", "text": preview, "subTurn": 1}
                events = [
                    run_a,
                    {"ts": base_ts + 0.8, "runId": "run-a", "kind": "assistant",
                     "text": "[开心]好呀主人～我是宠伴，随时在呢！", "subTurn": 1},
                    {"ts": base_ts + 3.5, "runId": "run-b", "kind": "user", "text": "继续讲讲。", "subTurn": 1},
                    {"ts": base_ts + 4.4, "runId": "run-b", "kind": "tool", "name": "search",
                     "args": {"query": preview}, "subTurn": 1},
                    {"ts": base_ts + 5.6, "runId": "run-b", "kind": "tool_result", "name": "search",
                     "text": "（mock 结果）找到 3 条相关结果…", "subTurn": 1},
                    {"ts": base_ts + 6.5, "runId": "run-b", "kind": "assistant",
                     "text": "[平静]查好啦，这是你要的信息～", "subTurn": 2},
                ]
                await _send(ws, {"type": "history:detail:ok", "id": mid, "sessionId": sid,
                                 "title": f"{_fmt_mock_time((item or {}).get('time', 0))} {preview[:20]}",
                                 "events": events})
            elif mtype == "history:delete":
                sid = msg.get("sessionId") or ""
                before = len(STATE["history"])
                STATE["history"] = [h for h in STATE["history"] if h.get("sessionId") != sid]
                if len(STATE["history"]) != before:
                    await _send(ws, {"type": "history:delete:ok", "id": mid, "sessionId": sid})
                else:
                    await _send(ws, {"type": "history:delete:error", "id": mid, "code": "E_NOT_FOUND",
                                     "message": "session 不存在"})
            # ── 人设 / 用户档案 ──
            elif mtype == "personality:get":
                await _send(ws, {"type": "personality:get:ok", "id": mid, "content": STATE["personality"]})
            elif mtype == "personality:set":
                STATE["personality"] = msg.get("content", "")
                await _send(ws, {"type": "personality:set:ok", "id": mid})
            elif mtype == "user:get":
                await _send(ws, {"type": "user:get:ok", "id": mid, **STATE["user_profile"]})
            elif mtype == "user:set":
                payload = msg.get("profile") if isinstance(msg.get("profile"), dict) else msg
                for k in ("basic", "reply_style", "likes", "dislikes", "daily"):
                    if k in payload:
                        STATE["user_profile"][k] = payload[k]
                await _send(ws, {"type": "user:set:ok", "id": mid})
            # ── 语音参数 ──
            elif mtype == "voice:settings:get":
                await _send(ws, {"type": "voice:settings:get:ok", "id": mid, **STATE["voice"]})
            elif mtype == "voice:settings:set":
                v = msg.get("volume")
                p = msg.get("pitch")
                vc = msg.get("voice")
                if isinstance(v, (int, float)):
                    STATE["voice"]["volume"] = int(v)
                if isinstance(p, (int, float)):
                    STATE["voice"]["pitch"] = int(p)
                if vc:
                    STATE["voice"]["voice"] = vc
                await _send(ws, {"type": "voice:settings:set:ok", "id": mid, **STATE["voice"]})
            # ── 音色列表（mock）──
            elif mtype == "voice:voices":
                await _send(ws, {"type": "voice:voices:ok", "id": mid,
                                 "model": STATE.get("tts_model", "qwen3-tts-instruct-flash-realtime"),
                                 "current": STATE["voice"].get("voice", "Mochi"),
                                 "voices": [
                                     {"id": "Cherry", "label": "Cherry · 甜美女声"},
                                     {"id": "Serena", "label": "Serena · 温柔女声"},
                                     {"id": "Mochi", "label": "Mochi · 沙小弥"},
                                 ]})
            # ── 模型配置（mock，5 组）──
            elif mtype == "model:get":
                await _send(ws, {"type": "model:get:ok", "id": mid, **STATE.setdefault("model", {
                    "llm": {"type": "llm", "label": "大语言模型", "hint": "", "sub": "Qwen（百炼）",
                            "url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-flash",
                            "api_key_set": True, "api_key_env": "QWEN_LLM_API_KEY", "api_key_masked": "sk-****4A1w"},
                    "asr": {"type": "asr", "label": "ASR", "hint": "仅支持 *-realtime 流式识别模型", "sub": "阿里云 Qwen3-ASR",
                            "url": "wss://dashscope.aliyuncs.com/api-ws/v1/realtime", "model": "qwen3-asr-flash-realtime",
                            "api_key_set": True, "api_key_env": "ASR_API_KEY", "api_key_masked": "sk-****5T7Z"},
                    "tts": {"type": "tts", "label": "TTS", "hint": "仅支持 *-realtime 流式合成模型", "sub": "阿里云 Qwen3-TTS",
                            "url": "https://dashscope.aliyuncs.com", "model": "qwen3-tts-instruct-flash-realtime",
                            "voice": "Mochi", "api_key_set": True, "api_key_env": "DASHSCOPE_API_KEY", "api_key_masked": "sk-****Ndgc"},
                    "vision": {"type": "vision", "label": "识图模型", "hint": "用于普通图片消息的识图服务", "sub": "智谱 GLM 视觉",
                               "url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4.6v-flash",
                               "api_key_set": False, "api_key_env": "VISION_API_KEY", "api_key_masked": ""},
                    "video": {"type": "video", "label": "视频模型", "hint": "视频 Base URL 需以 /v1 结尾，例如 https://api.example.com/v1", "sub": "",
                              "url": "https://api.example.com/v1", "model": "",
                              "api_key_set": False, "api_key_env": "VIDEO_API_KEY", "api_key_masked": ""},
                })})
            elif mtype == "model:set":
                STATE["model"] = STATE.get("model", {})
                sec = msg.get("sections") if isinstance(msg.get("sections"), dict) else {}
                for typ, s in sec.items():
                    base = STATE["model"].setdefault(typ, {})
                    for f in ("url", "api_key", "model", "voice"):
                        if f in s:
                            base[f] = s[f]
                await _send(ws, {"type": "model:set:ok", "id": mid, **STATE["model"]})
            elif mtype == "model:check":
                await _send(ws, {"type": "model:check:ok", "id": mid,
                                 "ok": False,
                                 "checks": [
                                     {"key": "QWEN_LLM_API_KEY", "label": "大语言模型 · API Key", "status": "ok",
                                      "detail": "sk-****4A1w", "model": "qwen-flash"},
                                     {"key": "ASR_API_KEY", "label": "ASR · API Key", "status": "ok",
                                      "detail": "sk-****5T7Z", "model": "qwen3-asr-flash-realtime"},
                                     {"key": "DASHSCOPE_API_KEY", "label": "TTS · API Key", "status": "ok",
                                      "detail": "sk-****Ndgc", "model": "qwen3-tts-instruct-flash-realtime"},
                                     {"key": "VISION_API_KEY", "label": "识图模型 · API Key", "status": "missing",
                                      "detail": "需要填写，否则该服务会失败", "model": "glm-4.6v-flash"},
                                     {"key": "VIDEO_API_KEY", "label": "视频模型 · API Key", "status": "missing",
                                      "detail": "需要填写，否则该服务会失败", "model": ""},
                                 ],
                                 "live": {"status": "ok", "detail": "连通正常（mock）", "latency_ms": 0},
                                 "required": ["QWEN_LLM_API_KEY", "ASR_API_KEY", "DASHSCOPE_API_KEY",
                                              "VISION_API_KEY", "VIDEO_API_KEY"]})
            elif mtype == "model:list":
                _MODELS = {
                    "llm": [{"id": "qwen-flash", "label": "Qwen · qwen-flash"}, {"id": "deepseek-v4-flash", "label": "DeepSeek · deepseek-v4-flash"}],
                    "asr": [{"id": "qwen3-asr-flash-realtime", "label": "qwen3-asr-flash-realtime（流式）"}],
                    "tts": [{"id": "qwen3-tts-instruct-flash-realtime", "label": "qwen3-tts-instruct-flash-realtime（流式·指令）"}],
                    "vision": [{"id": "glm-4.6v-flash", "label": "GLM-4.6V-Flash（免费）"}],
                    "video": [],
                }
                t = msg.get("category", "")
                await _send(ws, {"type": "model:list:ok", "id": mid, "category": t, "label": t,
                                 "models": _MODELS.get(t, [])})
            # ── 未知 ──
            else:
                await _send(ws, {"type": mtype + ":error", "id": mid, "code": "E_NOT_FOUND",
                                 "message": f"未知消息类型: {mtype}"})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[mock] 连接异常: {e}")
    finally:
        STATE["clients"].remove(ws)
        print(f"[mock] 连接关闭 client={client_id}")


if __name__ == "__main__":
    import uvicorn
    port = 9000
    print(f"[mock] PetPal Mock 后端启动 → ws://127.0.0.1:{port}/ws   (0.0.0.0:{port})")
    uvicorn.run(app, host="0.0.0.0", port=port)
