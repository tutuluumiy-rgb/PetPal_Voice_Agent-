"""验证修复：后端处于 listening 时收到 speech_start(isPlaying=true) → 立即回 barge_confirm 掐断前端。

背景：client_playback_done 兜底/竞态会提前关掉打断窗口（后端切 listening），
但前端球球可能仍有音频在播。此时用户开口，前端只 ducking 不静音、后端只启动 ASR 不掐断
→ 球球旧音频继续播甚至恢复全音量。修复后：isPlaying=true → 后端立即 barge_confirm。

用法：python probe_listening_barge.py [port]（默认 8001，需后端已启动）
"""
import asyncio
import json
import sys
import time

import websockets

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8001
WS_URL = f"ws://127.0.0.1:{PORT}/ws/audio"


async def _expect_bargeconfirm(ws, is_playing: bool, expect: bool) -> bool:
    """发 speech_start，断言是否期望收到 barge_confirm。expect=True → 收到即 PASS；expect=False → 3s 无 barge_confirm 即 PASS。"""
    got = []
    await ws.send(json.dumps({
        "type": "speech_start",
        "preRollBase64": None,
        "isPlaying": is_playing,
    }))
    try:
        async with asyncio.timeout(3.0):
            while True:
                raw = await ws.recv()
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                if isinstance(msg, dict) and msg.get("type"):
                    got.append(msg.get("type"))
                if msg.get("type") == "barge_confirm":
                    if expect:
                        print(f"[PASS] isPlaying={is_playing} → 收到 barge_confirm（立即掐断） backend_ms={msg.get('backend_ms')}")
                        return True
                    print(f"[FAIL] isPlaying={is_playing} → 不应掐断却收到 barge_confirm")
                    return False
    except asyncio.TimeoutError:
        if expect:
            print(f"[FAIL] isPlaying={is_playing} → 3s 未收到 barge_confirm，收到: {got}")
            return False
        print(f"[PASS] isPlaying={is_playing} → 未掐断（无 barge_confirm），消息: {got}")
        return True
    return False


async def main():
    # 场景1：新连接 listening + 前端仍在播 → 应立即掐断
    async with websockets.connect(WS_URL, max_size=None) as ws:
        await _expect_bargeconfirm(ws, True, True)
    # 场景2：新连接 listening + 前端未在播 → 正常收话，不应掐断
    async with websockets.connect(WS_URL, max_size=None) as ws:
        await _expect_bargeconfirm(ws, False, False)


if __name__ == "__main__":
    code = asyncio.run(main())
    sys.exit(code)