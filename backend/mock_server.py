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
    "user_profile": "# 用户档案\n\n- 称呼：主人\n- 偏好：喜欢 AI 与科技新闻\n",
    "voice": {"volume": 80, "pitch": 50, "voice": "default"},
    "history": [
        {"id": "h-1001", "mode": "chat", "time": int(time.time() * 1000) - 60_000,
         "preview": "你好球球，今天天气怎么样？"},
        {"id": "h-1002", "mode": "work", "time": int(time.time() * 1000) - 600_000,
         "preview": "帮我整理一下项目需求文档"},
        {"id": "h-1003", "mode": "chat", "time": int(time.time() * 1000) - 3600_000,
         "preview": "讲个笑话给我听听"},
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
                # 追加一条假历史
                STATE["history"].insert(0, {
                    "id": "h-" + uuid.uuid4().hex[:4], "mode": mode,
                    "time": int(time.time() * 1000), "preview": (text or "")[:40],
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
            # ── 人设 / 用户档案 ──
            elif mtype == "personality:get":
                await _send(ws, {"type": "personality:get:ok", "id": mid, "content": STATE["personality"]})
            elif mtype == "personality:set":
                STATE["personality"] = msg.get("content", "")
                await _send(ws, {"type": "personality:set:ok", "id": mid})
            elif mtype == "user:get":
                await _send(ws, {"type": "user:get:ok", "id": mid, "content": STATE["user_profile"]})
            elif mtype == "user:set":
                STATE["user_profile"] = msg.get("content", "")
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
                if vc in ("default", "cute", "calm", "bright"):
                    STATE["voice"]["voice"] = vc
                await _send(ws, {"type": "voice:settings:set:ok", "id": mid, **STATE["voice"]})
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
