"""管理端点 /ws/mgmt — 前端控制面板对接真实后端（契约：backend/docs/MOCK_CONTRACT.md §6）

与语音端点 /ws/audio 并存于同一 FastAPI 应用，实现 Mock 契约里的管理域：
  auth / ping / mode:get|set / auth:policy:get|set / personality:get|set /
  user:get|set / voice:settings:get|set / history:list|search|detail

真实数据源：
  - personality:* → prompts/personality.md
  - user:*       → users/<ACTIVE_USER>/profile.json（结构化）
  - voice:*      → data/voice_settings.json（voice_settings 模块）
  - auth:policy  → data/auth_policy.json（'full'|'ask'）
  - history:*    → sessions/*.jsonl（按 run_id 聚合）
"""

from __future__ import annotations

import json
import os
import uuid

from fastapi import WebSocket, WebSocketDisconnect

from prompt_loader import get_active_user_id

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPTS_DIR = os.path.join(BASE_DIR, "prompts")
USERS_DIR = os.path.join(BASE_DIR, "users")
SESSIONS_DIR = os.path.join(BASE_DIR, "sessions")
DATA_DIR = os.path.join(BASE_DIR, "data")
AUTH_POLICY_PATH = os.path.join(DATA_DIR, "auth_policy.json")

PERSONALITY_PATH = os.path.join(PROMPTS_DIR, "personality.md")

DEFAULT_AUTH_POLICY = "ask"

# ── 文件读写 ─────────────────────────────────────────


def _read_file(path: str, default: str = "") -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return default


def _write_file(path: str, content: str) -> bool:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return True
    except OSError as e:
        print(f"[mgmt] 写文件失败 {path}: {e}")
        return False


def _load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _save_json(path: str, obj) -> bool:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        return True
    except OSError as e:
        print(f"[mgmt] 写 JSON 失败 {path}: {e}")
        return False


# ── 历史聚合（sessions/*.jsonl 按 run 分组）────────────────


def _iter_session_msgs():
    """yield (session_id, msg) 遍历所有 session 文件。"""
    try:
        files = [f for f in os.listdir(SESSIONS_DIR) if f.endswith(".jsonl")]
    except OSError:
        return
    for name in sorted(files):
        sid = name[:-6]
        path = os.path.join(SESSIONS_DIR, name)
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield sid, json.loads(line)
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue


def _short(text: str, n: int = 20) -> str:
    t = (text or "").replace("\n", " ").strip()
    return (t[:n] + "…") if len(t) > n else t


def collect_sessions() -> list[dict]:
    """按 session 聚合全部会话（一次对话=一个 session，内含多轮 run），按最后时间倒序。

    条目字段：sessionId / time(最后时间) / preview(首个 user 输入前20字+…) /
    msgCount(全部消息) / runCount(轮数)。
    """
    sess: dict[str, dict] = {}
    for sid, msg in _iter_session_msgs():
        s = sess.setdefault(sid, {
            "sessionId": sid, "firstTs": None, "lastTs": None,
            "preview": None, "userText": None, "msgCount": 0,
            "runIds": set(), "messages": [],
        })
        s["messages"].append(msg)
        ts = msg.get("ts")
        if ts is not None:
            s["lastTs"] = max(s["lastTs"] or 0, ts)
            s["firstTs"] = s["firstTs"] or ts
        rid = msg.get("run_id")
        if rid:
            s["runIds"].add(rid)
        if msg.get("role") == "user" and isinstance(msg.get("content"), str):
            s["userText"] = s["userText"] or msg["content"]
            s["preview"] = s["preview"] or _short(msg["content"])
        s["msgCount"] += 1
    out = []
    for s in sess.values():
        out.append({
            "sessionId": s["sessionId"],
            "time": int((s["lastTs"] or s["firstTs"] or 0) * 1000),
            "preview": s["preview"] or _short(s["userText"]) or "（本次会话无文本）",
            "msgCount": s["msgCount"],
            "runCount": len(s["runIds"]),
            "_messages": s["messages"],
        })
    out.sort(key=lambda s: s["time"], reverse=True)
    return out


def build_session_events(messages: list[dict]) -> list[dict]:
    """会话内全部消息 → 有序事件流（含 runId 便于按轮分组）。"""
    events: list[dict] = []
    ordered = sorted(messages, key=lambda m: (m.get("ts") or 0, m.get("id") or ""))
    for m in ordered:
        role = m.get("role")
        ts = m.get("ts")
        sub = m.get("sub_turn")
        rid = m.get("run_id")
        if role == "user":
            if m.get("content"):
                events.append({"ts": ts, "runId": rid, "kind": "user", "text": str(m["content"]), "subTurn": sub})
        elif role == "assistant":
            tcs = m.get("tool_calls")
            if tcs:
                for tc in tcs:
                    fn = tc.get("function", {}) if isinstance(tc, dict) else getattr(tc, "function", None)
                    name = fn.get("name") if isinstance(fn, dict) else getattr(fn, "name", "") if fn else ""
                    raw = fn.get("arguments", "{}") if isinstance(fn, dict) else getattr(fn, "arguments", "{}") if fn else "{}"
                    try:
                        args = json.loads(raw or "{}")
                    except (ValueError, TypeError):
                        args = {"_raw": raw}
                    events.append({"ts": ts, "runId": rid, "kind": "tool", "name": name, "args": args, "subTurn": sub})
            if m.get("content"):
                events.append({"ts": ts, "runId": rid, "kind": "assistant", "text": str(m["content"]), "subTurn": sub})
        elif role == "tool":
            events.append({"ts": ts, "runId": rid, "kind": "tool_result",
                           "text": _short(str(m.get("content", "")), 120), "subTurn": sub})
    return events


# ── WebSocket 处理 ────────────────────────────────────


async def _send(ws: WebSocket, obj: dict):
    await ws.send_text(json.dumps(obj, ensure_ascii=False))


async def _err(ws: WebSocket, mid, code, message):
    await _send(ws, {"type": "_.error", "id": mid, "code": code, "message": message})


def register_mgmt(app) -> None:
    """挂载 /ws/mgmt 到 FastAPI 应用。"""

    @app.websocket("/ws/mgmt")
    async def mgmt_ws(ws: WebSocket):
        await ws.accept()
        client_id = "petpal-" + uuid.uuid4().hex[:6]
        authed = False
        print(f"[mgmt] 连接建立 {client_id}")
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await _err(ws, None, "E_VALIDATION", "JSON 解析失败")
                    continue
                mtype = msg.get("type", "")
                mid = msg.get("id")

                # 握手 / 心跳
                if mtype == "auth":
                    authed = True
                    await _send(ws, {"type": "auth:ok", "id": mid, "clientId": client_id})
                elif mtype == "ping":
                    await _send(ws, {"type": "pong", "id": mid})
                elif not authed:
                    await _err(ws, mid, "E_UNAUTHORIZED", "未鉴权")
                # ── 人设 ──
                elif mtype == "personality:get":
                    await _send(ws, {"type": "personality:get:ok", "id": mid, "content": _read_file(PERSONALITY_PATH)})
                elif mtype == "personality:set":
                    ok = _write_file(PERSONALITY_PATH, str(msg.get("content", "")))
                    await _send(ws, {"type": "personality:set:ok" if ok else "personality:set:error", "id": mid})
                # ── 用户档案（结构化）──
                elif mtype == "user:get":
                    profile = _load_json(
                        os.path.join(USERS_DIR, get_active_user_id(), "profile.json"), {})
                    await _send(ws, {"type": "user:get:ok", "id": mid, **profile})
                elif mtype == "user:set":
                    payload = msg.get("profile") if isinstance(msg.get("profile"), dict) else msg
                    profile = _load_json(
                        os.path.join(USERS_DIR, get_active_user_id(), "profile.json"), {})
                    for k in ("basic", "reply_style", "likes", "dislikes", "daily"):
                        if k in payload:
                            profile[k] = payload[k]
                    ok = _save_json(os.path.join(USERS_DIR, get_active_user_id(), "profile.json"), profile)
                    await _send(ws, {"type": "user:set:ok" if ok else "user:set:error", "id": mid})
                # ── 语音参数 ──
                elif mtype == "voice:settings:get":
                    from voice_settings import load_voice_settings
                    await _send(ws, {"type": "voice:settings:get:ok", "id": mid, **load_voice_settings()})
                elif mtype == "voice:settings:set":
                    from voice_settings import save_voice_settings
                    await _send(ws, {"type": "voice:settings:set:ok", "id": mid, **save_voice_settings(msg)})
                # ── 音色列表（按当前 TTS 模型实时拉取）──
                elif mtype == "voice:voices":
                    from voice_catalog import list_voices
                    await _send(ws, {"type": "voice:voices:ok", "id": mid, **list_voices(msg.get("model"))})
                # ── 模型配置（当前模型 + 所需 API 密钥）──
                elif mtype == "model:get":
                    from model_config import get_model_config
                    await _send(ws, {"type": "model:get:ok", "id": mid, **get_model_config()})
                elif mtype == "model:set":
                    from model_config import save_model_config
                    try:
                        await _send(ws, {"type": "model:set:ok", "id": mid, **save_model_config(msg)})
                    except Exception as e:
                        print(f"[mgmt] model:set 异常: {e}")
                        await _err(ws, mid, "E_MODEL_SAVE", f"保存模型配置失败: {e}")
                elif mtype == "model:check":
                    from model_config import check_model_config
                    try:
                        await _send(ws, {"type": "model:check:ok", "id": mid, **await check_model_config()})
                    except Exception as e:
                        print(f"[mgmt] model:check 异常: {e}")
                        await _err(ws, mid, "E_MODEL_CHECK", f"检查模型配置失败: {e}")
                elif mtype == "model:list":
                    from model_config import list_available_models
                    await _send(ws, {"type": "model:list:ok", "id": mid, **list_available_models(msg.get("category"))})
                # ── 权限策略 ──
                elif mtype == "auth:policy:get":
                    policy = _load_json(AUTH_POLICY_PATH, {"policy": DEFAULT_AUTH_POLICY}).get("policy", DEFAULT_AUTH_POLICY)
                    await _send(ws, {"type": "auth:policy:get:ok", "id": mid, "policy": policy})
                elif mtype == "auth:policy:set":
                    policy = msg.get("policy")
                    if policy not in ("full", "ask"):
                        await _err(ws, mid, "E_VALIDATION", "policy 必须是 full 或 ask")
                    else:
                        _save_json(AUTH_POLICY_PATH, {"policy": policy})
                        await _send(ws, {"type": "auth:policy:set:ok", "id": mid, "policy": policy})
                # ── 模式 ──
                elif mtype == "mode:get":
                    from mode_state import get_mode_state
                    await _send(ws, {"type": "mode:get:ok", "id": mid, "mode": get_mode_state().get_mode()})
                elif mtype == "mode:set":
                    requested = msg.get("mode")
                    if requested not in ("chat", "work"):
                        await _err(ws, mid, "E_VALIDATION", "mode 必须是 chat 或 work")
                    else:
                        from mode_state import get_mode_state
                        get_mode_state().switch(requested)
                        await _send(ws, {"type": "mode:set:ok", "id": mid, "mode": requested})
                # ── 历史 ──
                elif mtype in ("history:list", "history:search"):
                    page = max(1, int(msg.get("page", 1) or 1))
                    page_size = max(1, int(msg.get("pageSize", 20) or 20))
                    keyword = (msg.get("keyword", "") or "").strip()
                    sessions = collect_sessions()
                    if keyword:
                        sessions = [s for s in sessions if keyword in (s["preview"] or "")]
                    total = len(sessions)
                    items = [{k: s[k] for k in ("sessionId", "time", "preview", "msgCount", "runCount")}
                             for s in sessions[(page - 1) * page_size: page * page_size]]
                    await _send(ws, {"type": "history:list:ok", "id": mid,
                                     "items": items, "total": total, "page": page})
                elif mtype == "history:detail":
                    sid = msg.get("sessionId") or ""
                    sessions = collect_sessions()
                    sess = next((s for s in sessions if s["sessionId"] == sid), None)
                    if not sess:
                        await _err(ws, mid, "E_NOT_FOUND", "session 不存在")
                    else:
                        from datetime import datetime
                        title = f"{datetime.fromtimestamp(sess['time'] / 1000).strftime('%m-%d %H:%M')} {sess['preview']}"
                        await _send(ws, {"type": "history:detail:ok", "id": mid,
                                         "sessionId": sid, "title": title,
                                         "events": build_session_events(sess["_messages"])})
                elif mtype == "history:delete":
                    sid = msg.get("sessionId") or ""
                    if not sid:
                        await _err(ws, mid, "E_VALIDATION", "缺少 sessionId")
                    else:
                        path = os.path.join(SESSIONS_DIR, f"{sid}.jsonl")
                        if os.path.exists(path):
                            try:
                                os.remove(path)
                                await _send(ws, {"type": "history:delete:ok", "id": mid, "sessionId": sid})
                            except OSError as e:
                                await _err(ws, mid, "E_DELETE", f"删除会话失败: {e}")
                        else:
                            await _err(ws, mid, "E_NOT_FOUND", "session 不存在")
                else:
                    await _err(ws, mid, "E_NOT_FOUND", f"未知消息类型: {mtype}")
        except WebSocketDisconnect:
            pass
        except Exception as e:
            print(f"[mgmt] 连接异常: {e}")
        finally:
            print(f"[mgmt] 连接关闭 {client_id}")