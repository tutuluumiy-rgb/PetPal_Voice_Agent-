"""真实 uvicorn 启动冒烟：验证中间件堆栈构建 + Origin 中间件在真实请求下生效。

背景：_OriginGuard 首参名 inner 导致 Starlette build_middleware_stack 抛
`unexpected keyword argument 'app'`——进程能启动，但一接请求整个 ASGI 栈崩。
此脚本用与生产相同的 uvicorn 栈发真实 HTTP/WS 请求：
  - 无 Origin / 本机 Origin  → HTTP 应通过（/health 200）
  - 恶意 Origin             → HTTP 403 / WS 非 101（1008 拒绝）
  - 中间件堆栈能构建         → 即本次崩溃路径不再复现

用法：cd backend && python scripts/smoke_start.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import uvicorn
from uvicorn.config import Config

import main as main_mod

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  [PASS] {name} {detail}")
    else:
        FAIL.append(name)
        print(f"  [FAIL] {name} {detail}")


async def http_probe(port, origin, path="/health"):
    """原始 TCP GET：返回状态行（如 'HTTP/1.1 200 OK'）。"""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    req = f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
    if origin is not None:
        req += f"Origin: {origin}\r\n"
    req += "Connection: close\r\n\r\n"
    writer.write(req.encode("latin1"))
    await writer.drain()
    data = await reader.read()
    writer.close()
    await writer.wait_closed()
    return data.split(b"\r\n", 1)[0].decode("latin1", "replace")


async def ws_probe(port, origin, path="/ws/audio"):
    """原始 TCP WebSocket 握手：返回状态行（101 或拒绝码）。"""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
        "Sec-WebSocket-Version: 13\r\n"
    )
    if origin is not None:
        req += f"Origin: {origin}\r\n"
    req += "\r\n"
    writer.write(req.encode("latin1"))
    await writer.drain()
    data = await reader.read()
    writer.close()
    await writer.wait_closed()
    return data.split(b"\r\n", 1)[0].decode("latin1", "replace")


async def run():
    # 1) 中间件堆栈构建（本 bug 的崩溃点）
    try:
        main_mod.app.build_middleware_stack()
        check("中间件堆栈可构建", True)
    except Exception as e:
        check("中间件堆栈可构建", False, f"{type(e).__name__}: {e}")
        return 1

    # 2) 真实 uvicorn 起服务（与生产同栈）
    config = Config(app=main_mod.app, host="127.0.0.1", port=0,
                    log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    if not server.started:
        check("uvicorn 启动", False, "server 未就绪")
        task.cancel()
        return 1
    port = next(iter(server.servers)).sockets[0].getsockname()[1]
    check("uvicorn 启动", True, f"port={port}")

    # 3) HTTP：无 Origin / 本机 Origin 通过；恶意 Origin 403
    s_no = await http_probe(port, None)
    check("HTTP 无 Origin 放行", "200" in s_no, s_no)
    s_local = await http_probe(port, "http://127.0.0.1:8001")
    check("HTTP 本机 Origin 放行", "200" in s_local, s_local)
    s_evil = await http_probe(port, "http://evil.example.com")
    check("HTTP 恶意 Origin 拒绝(403)", "403" in s_evil, s_evil)

    # 4) WS：恶意 Origin 拒绝（非 101）；本机 Origin 握手成功
    ws_evil = await ws_probe(port, "https://attacker.io")
    check("WS 恶意 Origin 拒绝(非101)", "101" not in ws_evil, ws_evil)
    ws_local = await ws_probe(port, "http://127.0.0.1:8001")
    check("WS 本机 Origin 握手成功", "101" in ws_local, ws_local)

    server.should_exit = True
    await asyncio.wait_for(task, timeout=5)
    return 1 if FAIL else 0


def main():
    print("== 真实 uvicorn 启动冒烟 ==")
    rc = asyncio.run(run())
    print(f"\n冒烟结果：通过 {len(PASS)} / 失败 {len(FAIL)}")
    return rc


if __name__ == "__main__":
    sys.exit(main())