"""验证 telemetry._extract_objective 的修复：
后端改造清单 §7 废弃 timing.total 后，total_ms 不再恒为 0，
改用 MockWs 绝对时间戳（send_json 记录 time.time()）近似「完整回合」服务端整轮耗时。

不依赖真实 LLM/GPU —— 用手工构造的 MockWs 消息模拟。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import telemetry


class FAKE_SESSION:
    round_id = 3


def make_mockws(msgs):
    """按 (abs_ts, type, data) 构造 MockWs"""

    class _W(telemetry.MockWs):
        def __init__(self):
            super().__init__()
            self.messages = msgs

    return _W()


def test_total_ms_uses_abs_ts_fallback():
    """废弃 total 后，total_ms = 最后一条消息绝对时刻 - t0"""
    t0 = time.time()
    msgs = [
        (t0 + 0.100, "event", {"type": "event", "stage": "LLM", "detail": "开始生成回复"}),
        (t0 + 0.200, "event", {"type": "event", "stage": "TTS", "detail": "合成: 你好"}),
        (t0 + 0.350, "timing", {"type": "timing", "current": {
            "asr": 0.05,
            "llm_first_token": 0.02,
            "llm_first_sentence": 0.15,
            "tts_first_packet": 0.05,
            "e2e": 0.25,
            "e2e_first_round": 0.25,
            "llm_first_sentence_first_round": 0.15,
            "tts_first_packet_first_round": 0.05,
        }, "avg": {}, "count": 1, "avg_count": 1}),
    ]
    ws = make_mockws(msgs)
    obj = telemetry._extract_objective(ws, t0, FAKE_SESSION(), expected_tool=None)

    # 首轮首响字段
    assert obj["e2e_latency_ms"] == 250.0, f"e2e={obj['e2e_latency_ms']}"
    assert obj["llm_first_sentence_ms"] == 150.0
    assert obj["tts_first_packet_ms"] == 50.0
    # total_ms 应为 350ms（最后一条 absolute ts - t0），不再是 0
    assert obj["total_ms"] == 350.0, f"total_ms={obj['total_ms']}，期望 350.0"
    print(f"[OK] total_ms={obj['total_ms']}ms (≈350ms)")


def test_total_ms_when_no_msgs():
    """无任何消息（异常路径）→ total_ms 为 None，不崩"""
    t0 = time.time()
    ws = make_mockws([])
    obj = telemetry._extract_objective(ws, t0, FAKE_SESSION(), expected_tool="none")
    assert obj["total_ms"] is None
    assert obj["tool_call_success"] is None
    print("[OK] 空消息 total_ms=None")


def test_total_ms_legacy_fields_fallback():
    """旧 backend（无 first_round 字段）兜底：用 e2e/llm_first_sentence/tts_first_packet 末轮值"""
    t0 = time.time()
    msgs = [
        (t0 + 0.100, "event", {"type": "event", "stage": "LLM", "detail": "开始生成回复"}),
        (t0 + 0.300, "timing", {"type": "timing", "current": {
            "asr": 0.05, "llm_first_token": 0.02, "llm_first_sentence": 0.15,
            "tts_first_packet": 0.05, "e2e": 0.25,
        }, "avg": {}, "count": 1, "avg_count": 1}),
    ]
    ws = make_mockws(msgs)
    obj = telemetry._extract_objective(ws, t0, FAKE_SESSION(), expected_tool=None)
    assert obj["e2e_latency_ms"] == 250.0
    assert obj["total_ms"] == 300.0, f"total_ms={obj['total_ms']}"
    print(f"[OK] legacy fallback e2e={obj['e2e_latency_ms']}ms total_ms={obj['total_ms']}ms")


if __name__ == "__main__":
    test_total_ms_uses_abs_ts_fallback()
    test_total_ms_when_no_msgs()
    test_total_ms_legacy_fields_fallback()
    print("\n[ALL PASS] telemetry._extract_objective total_ms 修复验证通过")