"""场景审计：五组场景全链路自测（不改人声占比判定）

场景组：
  1) 正常说话链路（短句直提 / 语气词过滤）
  2) 球球短播后插话（cache<1200ms）：插短话、插长话（含续说合并）
  3) 球球长播后插话（cache≥1200ms）：插短话、插长话（含续说合并）
  4) ASR 准确性：每次 finalize 恰一次、无 finalize 后 feed、无重复 asr_final/回复、
     合并 turn 文本/音频完整（模拟真实 ASR 的"按累积音频出文本 + 异常监测"）
  5) SmartTurn 窗口链路：直提无窗 / 低 p 开窗→超时提交 / 开窗→续说合并→一次提交
     / 尾静音重判提前提交（确定性用固定 p 替身；真实 p 作为观测打印）

方法：mock ASR/LLM/TTS/VAD（不联网），物理复核/端点判定按场景可控；
不修改生产代码；运行前自动打时间窗补丁（SUPPLEMENT 300ms / REJUDGE 150ms）。

用法：cd backend && python scripts/scenario_audit.py
"""

import asyncio
import base64
import os
import sys
import time
import unittest.mock as um

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

import main as main_mod  # noqa: E402
from main import (  # noqa: E402
    ConversationSession,
    handle_speech_start,
    handle_speech_end,
    handle_audio_frame,
)
from smart_turn import UnavailableJudge  # noqa: E402
from tests.test_full_link_rework import (  # noqa: E402
    MockWs,
    MockTTS,
    FakeLLM,
    FakeVAD,
    pcm_ms,
    ws_types,
    msg_texts,
)

SAMPLE = 16000
PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  [PASS] {name} {detail}")
    else:
        FAIL.append(name)
        print(f"  [FAIL] {name} {detail}")


class AuditASR:
    """模拟真实流式 ASR：累积音频、按会话出文本；并监测重复/错序/丢字。

    - start_streaming/finalize/reset 次数分别记录；
    - finalize 后若再 feed → 置 feed_after_finalize；
    - 同会话 finalize 多次 → 置 finalized_twice；
    - finalize 文本 = 预设文本（会话第一次 finalize 返回之），后续 finalize 返回空模拟异常。
    """

    def __init__(self):
        self._sessions = {}
        self.start_count = 0
        self.finalize_count = 0
        self.reset_count = 0
        self.feed_after_finalize = False
        self.finalized_twice = False
        self.fed_bytes = {}       # session_id -> 累积字节
        self.text = "默认文本"

    def set_text(self, t):
        self.text = t

    def start_streaming(self, sid, on_partial):
        self.start_count += 1
        self._sessions[sid] = {"fed": 0, "finalized": False}

    def feed(self, sid, pcm):
        s = self._sessions.get(sid)
        if s is None:
            return
        if s["finalized"]:
            self.feed_after_finalize = True
        s["fed"] += len(pcm)
        self.fed_bytes[sid] = s["fed"]

    async def finalize(self, sid):
        await asyncio.sleep(0)
        self.finalize_count += 1
        s = self._sessions.get(sid)
        if s is None:
            return ""
        if s["finalized"]:
            self.finalized_twice = True
            return ""
        s["finalized"] = True
        return self.text

    def reset(self, sid):
        self.reset_count += 1
        self._sessions.pop(sid, None)


def setup_patches(text):
    patchers = []
    asr = AuditASR()
    asr.set_text(text)
    _llm = FakeLLM("[开心]好的～")
    for name, obj in (("asr", asr), ("tts", MockTTS()), ("llm", _llm), ("backend_vad", FakeVAD(True))):
        p = um.patch.object(main_mod, name, obj)
        patchers.append(p)
        p.start()
    # 按模式选模型（work→deepseek）在审计场景统一指向 FakeLLM
    p = um.patch.object(main_mod, "get_llm_for_mode", lambda mode=None: _llm)
    patchers.append(p)
    p.start()
    return patchers, asr


def teardown(patchers):
    for p in patchers:
        p.stop()


def b64_of(pcm):
    return base64.b64encode((np.clip(pcm, -1, 1) * 32767).astype(np.int16).tobytes()).decode()


def tone(ms, amp=1500.0, f0=220.0):
    t = np.arange(int(SAMPLE * ms / 1000)) / SAMPLE
    return (amp * np.sin(2 * np.pi * f0 * t)).astype(np.float32)


async def feed_frames(ws, session, pcm, chunk_ms=100):
    n = int(SAMPLE * chunk_ms / 1000)
    for i in range(0, len(pcm), n):
        f = pcm[i:i + n]
        await handle_audio_frame(ws, session, (np.clip(f, -1, 1) * 32767).astype(np.int16).tobytes())


async def wait_settle(session, timeout_s=3.0):
    """让事件循环真正跑任务（不能用 time.sleep 阻塞——窗口/LLM 任务需要循环让步）"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        pending = []
        if session._supplement_task and not session._supplement_task.done():
            pending.append(session._supplement_task)
        if session._rejudge_task and not session._rejudge_task.done():
            pending.append(session._rejudge_task)
        if session.user_speech_task and not session.user_speech_task.done():
            pending.append(session.user_speech_task)
        if not pending:
            return
        try:
            await asyncio.wait_for(asyncio.gather(*pending), timeout=1.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass  # 继续等
    print("  [warn] wait_settle 超时")


def assert_turn_ok(ws, asr, tag, expect_reply=True, expect_asr_final=True):
    types = ws_types(ws)
    n_final = sum(1 for t in types if t == "asr_final")
    n_timing = sum(1 for t in types if t == "timing")
    n_reply = sum(1 for t in types if t in ("reply", "reply_append"))
    check(f"{tag}: asr_final 恰一次", n_final == (1 if expect_asr_final else 0), f"got {n_final}")
    check(f"{tag}: timing 恰一次", n_timing == 1, f"got {n_timing}")
    if expect_reply:
        check(f"{tag}: 有回复", n_reply >= 1, f"got {n_reply}")
    check(f"{tag}: 无重复识别（started/finalize/reset 数量）",
          asr.start_count == 1 and asr.finalize_count == 1 and asr.reset_count == 0,
          f"start={asr.start_count} finalize={asr.finalize_count} reset={asr.reset_count}")
    check(f"{tag}: 无 finalize 后 feed / 无重复 finalize",
          not asr.feed_after_finalize and not asr.finalized_twice)
    check(f"{tag}: 音频进过 ASR（累积>0）", len(asr.fed_bytes) > 0 and max(asr.fed_bytes.values()) > 0)


# ───────────────────────── 场景 1：正常说话链路 ─────────────────────────

async def s1_normal_short():
    print("\n== 场景1a 正常说话：短句直提 ==")
    patchers, asr = setup_patches("帮我查天气")
    try:
        ws, session = MockWs(), ConversationSession()
        session.state = "listening"
        await handle_speech_start(ws, session, pre_roll_b64=None, is_playing=False)
        session.is_user_speaking = True
        await feed_frames(ws, session, tone(900))
        with um.patch.object(main_mod, "smart_turn", UnavailableJudge(0.9)):
            await handle_speech_end(ws, session, audio_b64=b64_of(np.concatenate([tone(900), np.zeros(int(SAMPLE*0.32))])))
        await wait_settle(session)
        assert_turn_ok(ws, asr, "S1a")
        check("S1a: 无挂起/无残留", session.suspended_reply is None and session.supplement_state is None)
    finally:
        teardown(patchers)


async def s1_normal_filler():
    print("\n== 场景1b 正常说话：纯语气词（应过滤不回复）==")
    patchers, asr = setup_patches("嗯")
    try:
        ws, session = MockWs(), ConversationSession()
        session.state = "listening"
        await handle_speech_start(ws, session, pre_roll_b64=None, is_playing=False)
        session.is_user_speaking = True
        await feed_frames(ws, session, tone(500))
        with um.patch.object(main_mod, "smart_turn", UnavailableJudge(0.9)):
            await handle_speech_end(ws, session, audio_b64=b64_of(np.concatenate([tone(500), np.zeros(int(SAMPLE*0.32))])))
        await wait_settle(session)
        types = ws_types(ws)
        check("S1b: 语气词被过滤（无 reply/timing）",
              "reply" not in types and "reply_append" not in types and "timing" not in types,
              f"got {types}")
        check("S1b: 会话复位 listening", session.state == "listening")
    finally:
        teardown(patchers)


# ───────────────────────── 场景 2/3：球球播放中插话 ─────────────────────────

async def _barge(short_cache, tag, short_utterance=True):
    """state=speaking（球球在播）→ 插话：短缓存(短播)或长缓存(长播)；话分短/长。

    长话 = 插话说完又停顿再续说（speech_end→窗口→合并→snapshot…→最终提交一次）。
    物理复核：记录 cache 长度并放行（判定链路由单测 t4~t6b 覆盖，这里测"确认后的链路"）。
    """
    text = "换一个话题" if short_utterance else "我刚刚在想一个问题然后想换个话题"
    print(f"\n== 场景{'2' if short_cache else '3'}{'短' if short_utterance else '长'} "
          f"{'短播' if short_cache else '长播'}后插{'短' if short_utterance else '长'}话 ==")
    patchers, asr = setup_patches(text)
    try:
        ws, session = MockWs(), ConversationSession()
        session.state = "speaking"
        session.pending_reply_text = "球球正在说的旧回复内容"  # 挂起快照素材
        cache_ms = 600 if short_cache else 1600

        # 先灌"球球回声"，让 speaking_audio_cache 反映短播/长播（state=speaking 且非用户说话 → 缓存分支）
        pet_echo = np.concatenate([tone(560, 400), np.zeros(int(SAMPLE * 40 / 1000))])
        feed_len = 0
        while feed_len < cache_ms:
            await feed_frames(ws, session, pet_echo[: int(SAMPLE * 0.2)])
            feed_len += 200
        cache_real_ms = len(session.speaking_audio_cache) / 2 / SAMPLE * 1000
        print(f"  [i] 预灌球球回声缓存：{cache_real_ms:.0f}ms（目标 {cache_ms}ms）")

        seen = {}

        def fake_confirm(confirm, cache):
            seen["cache_ms"] = len(cache) / 2 / SAMPLE * 1000
            return True

        with um.patch.object(main_mod, "_confirm_real_speech", fake_confirm):
            pre_roll = b64_of(tone(256, 900))
            await handle_speech_start(ws, session, pre_roll_b64=pre_roll, is_playing=True)
        check(f"{tag}: 物理复核收到缓存={short_cache and '<1200ms' or '≥1200ms'}",
              seen.get("cache_ms", 0) > 0 and (seen["cache_ms"] < 1200 if short_cache else seen["cache_ms"] >= 1200),
              f"cache={seen.get('cache_ms', 0):.0f}ms")
        check(f"{tag}: barge_confirm 已发", "barge_confirm" in ws_types(ws))
        check(f"{tag}: 挂起快照已生成", session.suspended_reply is not None)

        # 用户插话语音（喂 ASR / 说话段）
        session.is_user_speaking = True
        u1 = tone(900 if short_utterance else 1200, 1800)
        await feed_frames(ws, session, u1)

        if short_utterance:
            with um.patch.object(main_mod, "smart_turn", UnavailableJudge(0.9)):
                await handle_speech_end(ws, session, audio_b64=b64_of(np.concatenate([u1, np.zeros(int(SAMPLE*0.32))])))
            await wait_settle(session)
        else:
            # 长话：第一段（未完 p 低 → 开窗）→ 停顿后续说（合并）→ 说完提交一次
            with um.patch.object(main_mod, "smart_turn", UnavailableJudge(0.3)):
                await handle_speech_end(ws, session, audio_b64=b64_of(np.concatenate([u1, np.zeros(int(SAMPLE*0.32))])))
            check(f"{tag}: 长话首段低 p 开窗", session.supplement_state == "soft_ended")
            await handle_speech_start(ws, session, pre_roll_b64=None, is_playing=False)  # 续说
            check(f"{tag}: 续说合并 rev+1", session.turn_revision == 1 and session.supplement_state == "pending")
            u2 = tone(900, 1800)
            await feed_frames(ws, session, u2)
            with um.patch.object(main_mod, "smart_turn", UnavailableJudge(0.9)):
                await handle_speech_end(ws, session, audio_b64=b64_of(np.concatenate([u2, np.zeros(int(SAMPLE*0.32))])))
            await wait_settle(session)
            check(f"{tag}: 合并后一次提交（无多次 timing）",
                  ws_types(ws).count("timing") == 1)

        assert_turn_ok(ws, asr, tag)
        check(f"{tag}: 挂起已被消费/丢弃", session.suspended_reply is None)
        check(f"{tag}: 无补充窗口残留", session.supplement_state is None)
    finally:
        teardown(patchers)


# ───────────────────────── 场景 4：ASR 准确性附加检查 ─────────────────────────

async def s4_asr_anomalies():
    print("\n== 场景4 ASR 异常监测（真实累积 + 并发/重复防护）==")
    patchers, asr = setup_patches("完整句文本")
    try:
        ws, session = MockWs(), ConversationSession()
        session.state = "listening"
        await handle_speech_start(ws, session, pre_roll_b64=None, is_playing=False)
        session.is_user_speaking = True
        # 双段模拟：短停（<320ms 不触发 speech_end）→ 合并成一句
        u1 = tone(700); u2 = tone(900)
        await feed_frames(ws, session, u1)
        await feed_frames(ws, session, np.concatenate([np.zeros(int(SAMPLE*0.2)), u2]))
        with um.patch.object(main_mod, "smart_turn", UnavailableJudge(0.9)):
            await handle_speech_end(ws, session, audio_b64=b64_of(np.concatenate([u1, np.zeros(int(SAMPLE*0.2)), u2, np.zeros(int(SAMPLE*0.32))])))
        await wait_settle(session)
        assert_turn_ok(ws, asr, "S4")
        # 累积音频应覆盖 u1+gap+u2（≥ 700+180+900 ms 对应的字节下限）
        min_bytes = int(SAMPLE * (0.7 + 0.18 + 0.9) * 2) - 2000
        got = max(asr.fed_bytes.values())
        check("S4: 音频全量累积（无丢字下限）", got >= min_bytes, f"fed={got}B >= {min_bytes}B")
        check("S4: asr_final 文本消息唯一", ws_types(ws).count("asr_final") == 1)
    finally:
        teardown(patchers)


# ───────────────────────── 场景 5：SmartTurn 窗口链路 ─────────────────────────

async def s5_window_direct():
    print("\n== 场景5a 窗口链路：p 高直提（无窗）==")
    patchers, asr = setup_patches("高完整度句")
    try:
        ws, session = MockWs(), ConversationSession()
        session.state = "listening"
        await handle_speech_start(ws, session, pre_roll_b64=None, is_playing=False)
        session.is_user_speaking = True
        await feed_frames(ws, session, tone(1000))
        with um.patch.object(main_mod, "smart_turn", UnavailableJudge(0.9)):
            await handle_speech_end(ws, session, audio_b64=b64_of(tone(1000)))
        check("S5a: p高不开窗", session.supplement_state is None)
        await wait_settle(session)
        assert_turn_ok(ws, asr, "S5a")
    finally:
        teardown(patchers)


async def s5_window_timeout():
    print("\n== 场景5b 窗口链路：低 p 开窗 → 无补充 → 超时提交 ==")
    patchers, asr = setup_patches("未完句")
    try:
        ws, session = MockWs(), ConversationSession()
        session.state = "listening"
        await handle_speech_start(ws, session, pre_roll_b64=None, is_playing=False)
        session.is_user_speaking = True
        await feed_frames(ws, session, tone(1000))
        with um.patch.object(main_mod, "smart_turn", UnavailableJudge(0.3)):
            await handle_speech_end(ws, session, audio_b64=b64_of(tone(1000)))
        check("S5b: 低 p 开窗 soft_ended", session.supplement_state == "soft_ended")
        t0 = time.time()
        await wait_settle(session)
        el = time.time() - t0
        check("S5b: 窗口超时后才提交（≥ 补丁窗口 250ms）", el >= 0.25, f"elapsed={el:.2f}s")
        assert_turn_ok(ws, asr, "S5b")
        check("S5b: 提交后窗口状态复位", session.supplement_state is None)
    finally:
        teardown(patchers)


async def s5_window_merge():
    print("\n== 场景5c 窗口链路：开窗 → 续说合并 → 一次提交 ==")
    patchers, asr = setup_patches("续说完整句")
    try:
        ws, session = MockWs(), ConversationSession()
        session.state = "listening"
        await handle_speech_start(ws, session, pre_roll_b64=None, is_playing=False)
        session.is_user_speaking = True
        await feed_frames(ws, session, tone(900))
        with um.patch.object(main_mod, "smart_turn", UnavailableJudge(0.3)):
            await handle_speech_end(ws, session, audio_b64=b64_of(tone(900)))
        check("S5c: 首段开窗", session.supplement_state == "soft_ended")
        await handle_speech_start(ws, session, pre_roll_b64=None, is_playing=False)
        check("S5c: 续说合并 rev+1", session.turn_revision == 1)
        await feed_frames(ws, session, tone(900))
        with um.patch.object(main_mod, "smart_turn", UnavailableJudge(0.9)):
            await handle_speech_end(ws, session, audio_b64=b64_of(tone(900)))
        await wait_settle(session)
        check("S5c: 合并后仅一次提交", ws_types(ws).count("timing") == 1)
        assert_turn_ok(ws, asr, "S5c")
    finally:
        teardown(patchers)


async def s5_rejudge_early():
    print("\n== 场景5d 窗口链路：尾静音重判提前提交 ==")
    patchers, asr = setup_patches("重判文本")
    try:
        ws, session = MockWs(), ConversationSession()
        session.state = "listening"
        await handle_speech_start(ws, session, pre_roll_b64=None, is_playing=False)
        session.is_user_speaking = True
        with um.patch.object(main_mod, "smart_turn", _SeqJudge([0.3, 0.9])):
            await handle_speech_end(ws, session, audio_b64=b64_of(tone(900)))
            check("S5d: 首判开窗", session.supplement_state == "soft_ended")
            await handle_audio_frame(ws, session, (tone(150) * 0.3).astype(np.int16).tobytes())  # 尾静音帧
            await asyncio.sleep(0.22)  # REJUDGE=150ms 已过、SUPPLEMENT=300ms 未到
            check("S5d: 重判提前提交（尚未到窗口超时）",
                  "timing" in ws_types(ws) and session.supplement_state is None)
            await wait_settle(session)
        assert_turn_ok(ws, asr, "S5d")
    finally:
        teardown(patchers)


class _SeqJudge(UnavailableJudge):
    def __init__(self, ps):
        super().__init__(None)
        self._ps = list(ps)

    def judge(self, pcm):
        if self._ps:
            return self._ps.pop(0)
        return None


async def main():
    _w = um.patch.object(main_mod, "SUPPLEMENT_WINDOW_MS", 300)
    _w.start()
    _r = um.patch.object(main_mod, "SMART_TURN_REJUDGE_MS", 150)
    _r.start()
    try:
        await s1_normal_short()
        await s1_normal_filler()
        await _barge(short_cache=True, tag="S2a(短播·插短话)", short_utterance=True)
        await _barge(short_cache=True, tag="S2b(短播·插长话)", short_utterance=False)
        await _barge(short_cache=False, tag="S3a(长播·插短话)", short_utterance=True)
        await _barge(short_cache=False, tag="S3b(长播·插长话)", short_utterance=False)
        await s4_asr_anomalies()
        await s5_window_direct()
        await s5_window_timeout()
        await s5_window_merge()
        await s5_rejudge_early()
    finally:
        _r.stop()
        _w.stop()

    print(f"\n════ 汇总 ════")
    print(f"通过 {len(PASS)} 项: ")
    for p in PASS:
        print(f"  ✓ {p}")
    if FAIL:
        print(f"失败 {len(FAIL)} 项:")
        for f in FAIL:
            print(f"  ✗ {f}")
    else:
        print("无失败项。")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))