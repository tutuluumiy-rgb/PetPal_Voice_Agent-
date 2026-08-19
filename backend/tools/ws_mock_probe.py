# -*- coding: utf-8 -*-
"""验证 Mock 后端（backend/mock_server.py, ws://127.0.0.1:9000/ws）。

用 Python 标准库实现的极简 WebSocket 客户端（无第三方依赖）：
手动完成 HTTP/1.1 Upgrade 握手 + 帧编解码，向 Mock 服务器发送若干
契约消息，打印响应，验证各功能域返回假数据。

运行：
  python tools/ws_mock_probe.py
"""
import base64
import hashlib
import json
import os
import socket
import struct

HOST = "127.0.0.1"
PORT = 9000
PATH = "/ws"

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_connect(host, port, path):
    """返回 (sock, recv_buffer)。完成 WebSocket 握手。"""
    sock = socket.create_connection((host, port), timeout=5)
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    sock.sendall(req.encode())
    # 读握手响应（直到 \r\n\r\n）
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("握手失败：连接关闭")
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    if b"101" not in head.split(b"\r\n")[0]:
        raise RuntimeError(f"握手被拒绝: {head.decode(errors='replace')}")
    return sock, rest


def _recv_frame(ws, buf):
    """从连接读取一帧，返回 (opcode, payload_bytes, 剩余buffer)。"""
    while True:
        if len(buf) >= 2:
            b1, b2 = buf[0], buf[1]
            opcode = b1 & 0x0F
            masked = b2 >> 7
            ln = b2 & 0x7F
            idx = 2
            if ln == 126:
                if len(buf) < 4:
                    buf += _recv_some(ws)
                    continue
                ln = struct.unpack(">H", buf[2:4])[0]
                idx = 4
            elif ln == 127:
                if len(buf) < 10:
                    buf += _recv_some(ws)
                    continue
                ln = struct.unpack(">Q", buf[2:10])[0]
                idx = 10
            need = idx + (4 if masked else 0) + ln
            while len(buf) < need:
                buf += _recv_some(ws)
            mask = buf[idx:idx + 4] if masked else b""
            payload = bytearray(buf[idx + (4 if masked else 0):idx + (4 if masked else 0) + ln])
            if masked:
                for i in range(len(payload)):
                    payload[i] ^= mask[i % 4]
            return opcode, bytes(payload), buf[need:]
        buf += _recv_some(ws)


def _recv_some(ws):
    data = ws.recv(4096)
    if not data:
        raise RuntimeError("连接关闭")
    return data


def _send_frame(ws, payload: bytes):
    mask = os.urandom(4)
    header = bytearray()
    header.append(0x81)  # FIN + text
    ln = len(payload)
    if ln < 126:
        header.append(0x80 | ln)
    elif ln < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", ln)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", ln)
    header += mask
    masked = bytearray(payload)
    for i in range(len(masked)):
        masked[i] ^= mask[i % 4]
    ws.sendall(bytes(header) + bytes(masked))


def send_json(ws, obj):
    _send_frame(ws, json.dumps(obj, ensure_ascii=False).encode())


def recv_json_all(ws, buf, timeout=8.0, max_msgs=30):
    """连续收消息直到 超时/id 关联的流式结束，收集所有 (type,payload)。"""
    ws.settimeout(timeout)
    out = []
    try:
        while len(out) < max_msgs:
            opcode, payload, buf = _recv_frame(ws, buf)
            if opcode == 8:  # close
                break
            try:
                out.append(json.loads(payload.decode()))
            except Exception:
                out.append({"raw": payload.decode(errors="replace")})
    except socket.timeout:
        pass
    except (ConnectionResetError, OSError, RuntimeError):
        pass
    return out, buf


def main():
    print(f"连接 ws://{HOST}:{PORT}{PATH} ...")
    ws, buf = _ws_connect(HOST, PORT, PATH)
    print("[连接 OK]")

    def do(mtype, payload=None):
        send_json(ws, {"type": mtype, "id": "req-1", **(payload or {})})

    # 1) auth 握手
    do("auth", {"clientId": "probe-1", "token": "fake"})
    msgs, buf = recv_json_all(ws, buf, timeout=1.0)
    print("\n[auth] 响应:", msgs)

    # 2) mode:get / chat:send（流式）
    do("mode:get")
    msgs, buf = recv_json_all(ws, buf, timeout=1.0)
    print("[mode:get] 响应:", msgs)

    do("chat:send", {"mode": "work", "text": "帮我查两篇AI新闻"})
    msgs, buf = recv_json_all(ws, buf, timeout=1.0)
    print("[chat:send] 事件序列类型:", [m.get("type") for m in msgs])
    done = next((m for m in msgs if m.get("type") == "chat:send:done"), None)
    print("[chat:send:done] reply:", done.get("reply") if done else "N/A")

    # 3) history
    do("history:list", {"page": 1, "pageSize": 5})
    msgs, buf = recv_json_all(ws, buf, timeout=1.0)
    print("[history:list] 响应:", msgs)

    # 4) personality / user / voice / mode / auth:policy
    for t in ["personality:get", "user:get", "voice:settings:get",
              "mode:get", "auth:policy:get"]:
        do(t)
        msgs, buf = recv_json_all(ws, buf, timeout=1.0)
        print(f"[{t}] 响应类型:", [m.get("type") for m in msgs], "| data:", msgs[0] if msgs else "EMPTY")

    ws.close()


if __name__ == "__main__":
    main()
    print("\nMock 探针完成 ✅")
