"""真实后端 /ws/mgmt 全链路探测：按网关(GatewayClient)相同协议走一遍。

覆盖：auth 握手 / 恶意 token 拒绝 / personality:get / user:get / voice:settings:get /
      model:get / history:list / history:detail —— 全部应为【真实数据】而非 Mock 假数据。
用法：cd backend && python scripts/probe_mgmt_live.py
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import websockets

URL = "ws://127.0.0.1:8001/ws/mgmt"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {detail}")


async def main():
    ws = await websockets.connect(URL)
    seq = [0]

    def nid():
        seq[0] += 1
        return f"probe-{seq[0]}"

    async def send(obj):
        await ws.send(json.dumps(obj))

    async def recv_until(ttype, timeout=8):
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout)
            m = json.loads(raw)
            if m.get("type") == ttype:
                return m

    # 1) auth 成功（默认 fake-token）
    await send({"type": "auth", "id": nid(), "clientId": "probe", "token": "fake-token"})
    ok = await recv_until("auth:ok")
    check("auth(fake-token) 成功", True, f"clientId={ok.get('clientId')}")

    # 2) auth 再接一次走 ping
    await send({"type": "ping", "id": nid()})
    await recv_until("pong")
    check("ping/pong", True)

    # 3) personality:get → 真实文件内容（personality.md '# 名字\n西西'，字符数 8）
    await send({"type": "personality:get", "id": nid()})
    p = await recv_until("personality:get:ok")
    content = p.get("content", "")
    check("personality:get 真实文件", "西西" in content and 5 <= len(content) <= 40, f"content={content!r}")

    # 4) user:get → 真实 profile（user_001）
    await send({"type": "user:get", "id": nid()})
    u = await recv_until("user:get:ok")
    blobs = json.dumps(u, ensure_ascii=False)
    check("user:get 真实档案", len(blobs) > 60 or "basic" in u, f"keys={sorted(k for k in u if k != 'id')}")

    # 5) voice:settings:get
    await send({"type": "voice:settings:get", "id": nid()})
    v = await recv_until("voice:settings:get:ok")
    check("voice:settings:get", "volume" in v or "voice" in v, f"keys={sorted(v.keys())}")

    # 6) model:get
    await send({"type": "model:get", "id": nid()})
    m = await recv_until("model:get:ok")
    check("model:get", "llm" in m, f"keys={sorted(m.keys())}")

    # 7) history:list → 应非空（真实 sessions 目录 655 个）
    await send({"type": "history:list", "id": nid(), "page": 1, "pageSize": 5})
    h = await recv_until("history:list:ok")
    check("history:list 真实历史", h.get("total", 0) >= 50, f"total={h.get('total')} items={len(h.get('items') or [])}")
    sid = (h.get("items") or [{}])[0].get("sessionId")
    if sid:
        await send({"type": "history:detail", "id": nid(), "sessionId": sid})
        d = await recv_until("history:detail:ok")
        check("history:detail 事件流", "events" in d, f"sid={sid} events={len(d.get('events') or [])}")

    # 8) 恶意 token 应被拒（不阻塞后续）
    await send({"type": "auth", "id": nid(), "clientId": "bad", "token": "wrong-token"})
    bad = await recv_until("_.error")
    check("恶意 token 拒绝", bad.get("code") == "E_UNAUTHORIZED", bad.get("code"))

    await ws.close()
    print(f"\n探测结果：通过 {len(PASS)} / 失败 {len(FAIL)}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))