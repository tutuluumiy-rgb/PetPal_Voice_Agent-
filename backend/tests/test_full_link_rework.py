# -*- coding: utf-8 -*-
"""改造后完整链路自动测试（改造清单 #2/#4 + 语义裁决）

覆盖链路（MockWs 模拟前端事件，mock ASR/LLM/TTS，避免真实云调用）：
  1. 正常说话链路：speech_start(listening) → ASR 收话 → speech_end → finalize 有效 → LLM 新回复
  2. 打断确认 + 挂起上下文：speaking 态 speech_start → 物理复核(确认) → barge_confirm + suspended_reply 快照
  3. 语义裁决"无效"（语气词）→ 恢复：重播被打断回复（reply + tts 下发），挂起被消费
  4. 语义裁决"有效"（真指令）→ 真正丢弃：suspended_reply 清空 + 新流水线启动
  5. 物理复核（改造#2）：短缓存 → 跳过能量闸、占比用严阈值 0.3
  6. 物理复核（改造#2）：长缓存 + 能量无跃升（头部基线 vs 近窗）→ 拒绝
  7. 物理复核（改造#2）：长缓存 + 能量跃升 → 确认

运行：cd backend && python tests/test_full_link_rework.py
"""
import asyncio
import base64
import json
import os
import sys
import time
import unittest.mock as um

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

import main as main_mod  # noqa: E402
from smart_turn import UnavailableJudge  # noqa: E402
from main import (  # noqa: E402
    ConversationSession,
    handle_speech_start,
    handle_speech_end,
    handle_audio_frame,
    handle_control_message,
    finish_user_speech,
    _confirm_real_speech,
)


# ───────────────────────── 测试替身 ─────────────────────────

class MockWs:
    def __init__(self):
        self.messages = []  # [(ts, type, data)]
        self.audio_bytes = 0

    async def send_json(self, obj):
        self.messages.append((time.time(), obj.get("type", "?"), obj))

    async def send_bytes(self, data):
        self.audio_bytes += len(data)


class MockASR:
    """可控 ASR：start_streaming/feed/reset 记录，finalize 返回预设文本"""

    def __init__(self, final_text=""):
        self.final_text = final_text
        self.started = []
        self.fed = []
        self.reset_calls = 0
        self.last_partial_cb = None  # 供测试模拟流式 partial

    def start_streaming(self, sid, on_partial):
        self.started.append(sid)
        self.last_partial_cb = on_partial

    def feed(self, sid, pcm):
        self.fed.append((sid, bytes(pcm)))

    async def finalize(self, sid):
        # 模拟真实 ASR（AliyunASR.finalize 内部 await to_thread）——若生产路径有
        # "任务自取消"，会在这里抛 CancelledError；mock 不 await 就永远测不出来。
        await asyncio.sleep(0)
        return self.final_text

    def reset(self, sid):
        self.reset_calls += 1


class MockTTS:
    def __init__(self):
        self.cancelled = 0
        self.speaks = []

    def cancel(self):
        self.cancelled += 1

    async def preheat(self):
        pass

    async def speak_and_send(self, ws, text, session_id, params=None):
        await ws.send_json({"type": "tts_start", "session_id": session_id, "text": text})
        self.speaks.append(text)
        # 不产出音频（测试关注消息契约）
        await ws.send_json({"type": "tts_end", "session_id": session_id})


class FakeDelta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class FakeChoice:
    def __init__(self, delta):
        self.delta = delta


class FakeChunk:
    def __init__(self, content):
        self.choices = [FakeChoice(FakeDelta(content=content))]


class FakeStream:
    def __init__(self, texts):
        self.texts = [t for t in texts if t]
        self._i = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._i >= len(self.texts):
            raise StopAsyncIteration
        t = self.texts[self._i]
        self._i += 1
        return FakeChunk(t)


class FakeChatCompletions:
    def __init__(self, reply_text):
        self.reply_text = reply_text

    async def create(self, **kwargs):
        # 拆成多 chunk，验证流式逐句
        return FakeStream([self.reply_text])


class FakeChat:
    """client.chat 层级（OpenAI: client.chat.completions.create）"""

    def __init__(self, completions):
        self.completions = completions


class FakeLLMClient:
    def __init__(self, reply_text):
        self.chat = FakeChat(FakeChatCompletions(reply_text))


class FakeLLM:
    """替代 main.llm（run_agent_loop 需要的字段）"""

    def __init__(self, reply_text):
        self.client = FakeLLMClient(reply_text)
        self.model = "mock-model"
        self.timeout = 45
        self.first_token_time = None
        self.total_time = None


class FakeVAD:
    """替代 main.backend_vad（is_speech 可控）"""

    def __init__(self, result=True):
        self.result = result
        self.last_threshold = None
        self.last_ratio_thr = None
        self.last_audio = None  # 记录送入人声判定的音频（验证近窗是否含 preRoll）

    def is_speech(self, audio, threshold, ratio_threshold=0.3):
        self.last_threshold = threshold
        self.last_ratio_thr = ratio_threshold
        self.last_audio = audio
        return self.result, 0.9 if self.result else 0.01


class SeqJudge(UnavailableJudge):
    """顺序输出概率的判定替身：每次 judge 依次弹出列表中的值（测"首次未完→重判已完"）"""

    def __init__(self, ps):
        super().__init__(None)
        self._ps = list(ps)

    def judge(self, pcm):
        if self._ps:
            return self._ps.pop(0)
        return None


# ───────────────────────── 工具 ─────────────────────────

SAMPLE = 16000


def pcm_ms(ms, amplitude=1000.0):
    """生成 Pcm 16bit 单声道音频（ms 时长，恒定幅度）"""
    n = int(SAMPLE * ms / 1000)
    data = (np.full(n, amplitude, dtype=np.float32)).astype(np.int16).tobytes()
    return data


def ws_types(ws):
    return [t for (_ts, t, _d) in ws.messages]


def msg_texts(ws, mtype):
    return [d.get("text", "") for (_ts, t, d) in ws.messages if t == mtype]


def setup_patches(final_text: str, reply_text: str = "[开心]好的，这就帮你查！", vad_result: bool = True):
    """返回 (patchers列表, patches dict asr/tts/llm/vad)"""
    patchers = []
    asr = MockASR(final_text)
    tts = MockTTS()
    llm = FakeLLM(reply_text)
    vad = FakeVAD(vad_result)
    for name, obj in (("asr", asr), ("tts", tts), ("llm", llm), ("backend_vad", vad)):
        p = um.patch.object(main_mod, name, obj)
        patchers.append(p)
        p.start()
    return patchers, {"asr": asr, "tts": tts, "llm": llm, "vad": vad}


def teardown(patchers):
    for p in patchers:
        p.stop()


async def wait_turn_submitted(session):
    """等补充窗口结束并完成该用户 turn 的提交（改造清单#7：端点判定后可能开窗，非立即 finalize）"""
    if session._supplement_task:
        try:
            await session._supplement_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
    if session.user_speech_task:
        await session.user_speech_task


def _seg_b64():
    """构造"该说话段"音频（1.6s，供 smart-turn 端点判定）"""
    return base64.b64encode(pcm_ms(1600, 300)).decode()


# ───────────────────────── 用例 ─────────────────────────

async def t1_normal_speech_link():
    """正常说话链路：收话 → 语义有效 → LLM 新回复（走 finish_user_speech → handle_user_speech）"""
    patchers, patches = setup_patches(final_text="你好呀", reply_text="[开心]嗨！今天想聊什么？")
    try:
        ws = MockWs()
        session = ConversationSession()
        session.state = "listening"
        await handle_speech_start(ws, session, pre_roll_b64=None, is_playing=False)
        assert patches["asr"].started, "正常收话应启动 ASR 会话"
        session.is_user_speaking = True
        await handle_speech_end(ws, session)
        # 等待新回复流水线完成
        if session.user_speech_task:
            await session.user_speech_task
        types = ws_types(ws)
        assert "asr_final" in types, f"应下发 asr_final, got {types}"
        assert "reply" in types or "reply_append" in types, f"应下发 reply, got {types}"
        assert "timing" in types, f"应下发 timing, got {types}"
        assert session.suspended_reply is None
        joined = "".join(msg_texts(ws, "reply")) + "".join(msg_texts(ws, "reply_append"))
        assert "嗨" in joined, f"回复内容异常: {joined!r}"
        print("[PASS] t1 正常说话链路（收话→语义→LLM 新回复）")
    finally:
        teardown(patchers)


async def t2_barge_suspend_resume():
    """打断确认→挂起上下文；语义无效（语气词）→ 恢复重播"""
    patchers, patches = setup_patches(final_text="嗯", reply_text="[开心]临时新回复")
    try:
        # 构造：球球正在 speaking，已下发部分回复文本
        ws = MockWs()
        session = ConversationSession()
        session.state = "speaking"
        session.pending_reply_text = "好的，这就帮你查一下天气！"
        pre_roll = base64.b64encode(pcm_ms(256, 200)).decode()
        # 物理复核直接放行（确认打断）
        with um.patch.object(main_mod, "_confirm_real_speech", lambda confirm, cache: True):
            await handle_speech_start(ws, session, pre_roll_b64=pre_roll, is_playing=True)
        assert "barge_confirm" in ws_types(ws), f"应下发 barge_confirm, got {ws_types(ws)}"
        assert session.suspended_reply is not None, "打断确认后应快照挂起内容"
        assert session.suspended_reply["text"] == "好的，这就帮你查一下天气！"
        assert patches["asr"].started, "确认打断后应启动 ASR 接收插话"

        # 用户打断后说语气词 → speech_end → 语义无效 → 恢复（重播放）
        session.is_user_speaking = True
        await handle_speech_end(ws, session)
        await wait_turn_submitted(session)
        types = ws_types(ws)
        assert "resume_playback" in types or "reply" in types, f"无效打断应恢复, got {types}"
        # 恢复路径应重播被打断文本
        resumed = "".join(msg_texts(ws, "reply"))
        assert "查一下天气" in resumed or "查天气" in resumed, f"未恢复被打断内容: {resumed!r}"
        assert "tts_start" in types, f"恢复应重新下发 TTS, got {types}"
        assert session.suspended_reply is None, "恢复后挂起内容应被消费清空"
        print("[PASS] t2 打断挂起 → 语义无效 → 恢复重播")
    finally:
        teardown(patchers)


async def t3_barge_suspend_discard():
    """打断确认→挂起；语义有效（真指令）→ 真正丢弃 + 启动新流水线"""
    patchers, patches = setup_patches(final_text="帮我查一下今天深圳的天气", reply_text="[开心]深圳今天晴，28 度")
    try:
        ws = MockWs()
        session = ConversationSession()
        session.state = "speaking"
        session.pending_reply_text = "好的，这就帮你查一下天气！"
        pre_roll = base64.b64encode(pcm_ms(256, 200)).decode()
        with um.patch.object(main_mod, "_confirm_real_speech", lambda confirm, cache: True):
            await handle_speech_start(ws, session, pre_roll_b64=pre_roll, is_playing=True)
        assert session.suspended_reply is not None

        session.is_user_speaking = True
        await handle_speech_end(ws, session)
        await wait_turn_submitted(session)
        # 语义有效 → 挂起内容真正丢弃（不恢复旧回复）
        assert session.suspended_reply is None, "有效打断后旧挂起内容应被真正丢弃"
        types = ws_types(ws)
        assert "reply" in types, f"有效打断应进入新回复, got {types}"
        joined = "".join(msg_texts(ws, "reply")) + "".join(msg_texts(ws, "reply_append"))
        assert "深圳" in joined, f"新回复内容异常: {joined!r}"
        print("[PASS] t3 打断挂起 → 语义有效 → 真正丢弃 + 新回复")
    finally:
        teardown(patchers)


def t4_short_cache_uses_strict_ratio():
    """物理复核（改造#2）：短缓存 → 跳过能量闸，占比用严阈值 0.3（FakeVAD 校验）"""
    patchers, patches = setup_patches(final_text="", vad_result=False)
    try:
        vad = patches["vad"]
        # 短缓存 600ms + preRoll 256ms → confirm 总长 856ms（≥200 过闸①），cache 600ms < 1200 门槛
        confirm = bytearray(pcm_ms(256, 300)) + bytearray(pcm_ms(600, 500))
        cache = bytearray(pcm_ms(600, 500))
        result = _confirm_real_speech(confirm, cache)
        assert vad.last_ratio_thr == 0.3, f"短缓存应使用严阈值 0.3, got {vad.last_ratio_thr}"
        assert result is False  # FakeVAD 返回 False
        print("[PASS] t4 短缓存 → 跳过能量闸 + 占比 0.3 严阈值")
    finally:
        teardown(patchers)


def t5_long_cache_no_energy_jump_reject():
    """物理复核（改造#2）：长缓存 + 能量无跃升（头部基线≈近窗）→ 拒绝（能量闸先行）"""
    patchers, patches = setup_patches(final_text="", vad_result=True)  # FakeVAD 判人声 True，也应被能量闸拦
    try:
        # cache 1280ms：头部 512ms=振幅1000（基线），中 256ms=振幅1000，尾部 512ms=振幅1000 → jump≈1
        confirm = bytearray(pcm_ms(256, 200)) + bytearray(pcm_ms(1280, 1000))
        cache = bytearray(pcm_ms(1280, 1000))
        with um.patch.object(main_mod, "backend_vad", patches["vad"]):
            result = _confirm_real_speech(confirm, cache)
        assert result is False, "能量无跃升应拒绝打断（即使 Silero 判人声）"
        print("[PASS] t5 长缓存 + 能量无跃升（头部基线）→ 拒绝")
    finally:
        teardown(patchers)


def t6_long_cache_energy_jump_confirm():
    """物理复核（改造#2）：长缓存 + 能量跃升（头部基线低、近窗高）→ 走占比 → 确认"""
    patchers, patches = setup_patches(final_text="", vad_result=True)
    try:
        vad = patches["vad"]
        # cache 1280ms：头部 512ms=振幅1000（基线），中 256ms=1000，尾部 512ms=振幅5000 → jump≈5
        # confirm = preRoll + 【同一份 cache】（近窗=confirm 尾部=插话高能段，与生产一致）
        cache = bytearray(pcm_ms(512, 1000)) + bytearray(pcm_ms(256, 1000)) + bytearray(pcm_ms(512, 5000))
        confirm = bytearray(pcm_ms(256, 200)) + bytearray(cache)
        with um.patch.object(main_mod, "backend_vad", vad):
            result = _confirm_real_speech(confirm, cache)
        assert vad.last_ratio_thr == 0.03, f"长缓存应使用松阈值 0.03, got {vad.last_ratio_thr}"
        assert result is True
        print("[PASS] t6 长缓存 + 能量跃升（头部基线 vs 近窗）→ 占比 0.03 → 确认")
    finally:
        teardown(patchers)


def t6c_recent_includes_preroll():
    """物理复核：短缓存时人声判定输入应包含 preRoll（首字「那/对」在 preRoll 里）"""
    patchers, patches = setup_patches(final_text="", vad_result=True)
    try:
        vad = patches["vad"]
        pre = pcm_ms(256, 3000)   # preRoll：开口首个字（高音量人声）
        cache = pcm_ms(100, 300)  # 短缓存：球球回声（弱，比窗口短 → 近窗=preRoll尾段+缓存）
        confirm = bytearray(pre) + bytearray(cache)
        with um.patch.object(main_mod, "backend_vad", vad):
            result = _confirm_real_speech(confirm, bytearray(cache))
        assert result is True, f"短缓存+preRoll含首字应确认打断, got {result}"
        window_bytes = int(16000 * 0.32 * 2)
        offset = len(confirm) - window_bytes  # 近窗起点（在 preRoll 内 → preRoll 首字尾部进近窗）
        assert 0 <= offset < len(pre), f"offset={offset} 应在 preRoll 区间内"
        recent = bytes(vad.last_audio)
        assert recent == bytes(confirm[offset:]), "近窗应=confirm 尾部"
        assert recent.startswith(pre[offset:]), "近窗应包含 preRoll 的开口首字段"
        print("[PASS] t6c 近窗判定输入包含 preRoll → 首字开口不再被误拒")
    finally:
        teardown(patchers)


def t6b_baseline_silent_recent_energy_confirm():
    """物理复核：基线静音（≈0，mic 听不到球球）但近窗有明显能量 → 直接确认打断（跳过占比闸）"""
    patchers, patches = setup_patches(final_text="", vad_result=False)  # 占比闸若走会被 FakeVAD 拒
    try:
        vad = patches["vad"]
        # cache 1280ms：头部 768ms=振幅10（基线≈静音），尾部 512ms=振幅2000（用户插话能量）
        confirm = bytearray(pcm_ms(256, 200)) + bytearray(pcm_ms(1280, 1500))
        cache = bytearray(pcm_ms(768, 10)) + bytearray(pcm_ms(512, 2000))
        assert len(cache) == int(16000 * 1.28 * 2)
        with um.patch.object(main_mod, "backend_vad", vad):
            result = _confirm_real_speech(confirm, cache)
        assert result is True, "基线静音+近窗有能量应确认打断（占比闸被跳过）"
        assert vad.last_threshold is None, "该分支不应走占比闸（Silero VAD 不应被调用）"
        print("[PASS] t6b 基线静音 + 近窗能量下限 → 跳过占比直接确认打断")
    finally:
        teardown(patchers)


async def t7_stream_partial_early_effective():
    """"有效打断"流式提前判定：ASR partial 出现非语气词 → 不等 speech_end/finalize 即判有效、
    丢弃挂起；随后即使 finalize 是语气词也走新回复（不回恢复重播）。"""
    patchers, patches = setup_patches(final_text="嗯", reply_text="[开心]好的，上海天气晴")  # finalize 是语气词
    try:
        ws = MockWs()
        session = ConversationSession()
        session.state = "speaking"
        session.pending_reply_text = "好的，这就帮你查一下天气！"
        pre_roll = base64.b64encode(pcm_ms(256, 200)).decode()
        with um.patch.object(main_mod, "_confirm_real_speech", lambda confirm, cache: True):
            await handle_speech_start(ws, session, pre_roll_b64=pre_roll, is_playing=True)
        assert session.suspended_reply is not None

        # 模拟流式 partial：先"嗯"，再出现实质内容（全量修订）
        cb = patches["asr"].last_partial_cb
        assert cb is not None, "打断后应注册 ASR partial 回调"
        cb("嗯")            # 语气词 partial：不提前判有效
        assert session.suspended_reply is not None, "纯语气词 partial 不应丢弃挂起"
        cb("帮我查一下上海天气")  # 非语气词内容 → 流式提前判有效
        assert session.is_effective_interrupt is True, "非语气词 partial 应提前判有效"
        assert session.suspended_reply is None, "流式判有效后挂起应被立即丢弃（不等 speech_end）"

        # 用户说完，finalize 返回"嗯"（语气词）→ 因流式已判有效 → 走新回复而非恢复重播
        session.is_user_speaking = True
        await handle_speech_end(ws, session)
        await wait_turn_submitted(session)
        types = ws_types(ws)
        joined = "".join(msg_texts(ws, "reply")) + "".join(msg_texts(ws, "reply_append"))
        assert "上海" in joined, f"流式判有效应走新回复（含 mock LLM 文本）, got {joined!r}"
        assert "查一下天气" not in joined, "流式判有效后不应恢复重播旧内容"
        print("[PASS] t7 流式 partial 提前判有效（不等 speech_end/finalize）→ 新回复")
    finally:
        teardown(patchers)


async def t8_stream_partial_filler_keeps_suspend():
    """"纯语气词"流式不得提前判有效：partial 全程为语气词 → 挂起保留 → finalize 语气词 → 恢复重播"""
    patchers, patches = setup_patches(final_text="嗯嗯", reply_text="[开心]临时")
    try:
        ws = MockWs()
        session = ConversationSession()
        session.state = "speaking"
        session.pending_reply_text = "好的，这就帮你查一下天气！"
        pre_roll = base64.b64encode(pcm_ms(256, 200)).decode()
        with um.patch.object(main_mod, "_confirm_real_speech", lambda confirm, cache: True):
            await handle_speech_start(ws, session, pre_roll_b64=pre_roll, is_playing=True)

        cb = patches["asr"].last_partial_cb
        cb("嗯")
        cb("嗯嗯")
        assert session.is_effective_interrupt is False, "纯语气词 partial 不应判有效"
        assert session.suspended_reply is not None, "语气词 partial 不应丢弃挂起"

        session.is_user_speaking = True
        await handle_speech_end(ws, session)
        await wait_turn_submitted(session)
        joined = "".join(msg_texts(ws, "reply"))
        assert "查一下天气" in joined, f"filler 打断应恢复重播旧内容, got {joined!r}"
        print("[PASS] t8 纯语气词流式不提前判有效 → 恢复重播")
    finally:
        teardown(patchers)


def t9_no_short_text_filter():
    """"取消极短文本过滤"静态契约：恢复/无效判定不应引用长度阈值（RESUME_MAX_SHORT_CHARS）"""
    src = open(os.path.join(os.path.dirname(main_mod.__file__), "main.py"), encoding="utf-8").read()
    assert "RESUME_MAX_SHORT_CHARS" not in src, "极短文本过滤已取消，不应存在长度判据"
    print("[PASS] t9 已取消极短文本过滤（无长度判据残留）")


async def t10_supplement_window_commit():
    """补充窗口：打断暂断(SOFT_ENDED)后窗口内无补充 → 不立即 finalize，窗口结束才提交 → 新回复"""
    patchers, patches = setup_patches(final_text="帮我查一下上海天气", reply_text="[开心]上海晴，28 度")
    try:
        ws = MockWs()
        session = ConversationSession()
        session.state = "speaking"
        session.pending_reply_text = "旧播报内容"
        pre_roll = base64.b64encode(pcm_ms(256, 200)).decode()
        with um.patch.object(main_mod, "_confirm_real_speech", lambda c, k: True):
            await handle_speech_start(ws, session, pre_roll_b64=pre_roll, is_playing=True)
        assert session.supplement_state is None  # 端点判定接管（非打断专有状态）
        session.is_user_speaking = True
        with um.patch.object(main_mod, "smart_turn", UnavailableJudge(0.3)):  # p≤0.5 → 未完 → 开窗
            await handle_speech_end(ws, session, audio_b64=_seg_b64())
        assert session.supplement_state == "soft_ended", "端点判未完应进入 SOFT_ENDED"
        await asyncio.sleep(0.02)
        early = ws_types(ws)
        assert "reply" not in early and "timing" not in early, f"窗口内不应提前提交, got {early}"
        await wait_turn_submitted(session)
        joined = "".join(msg_texts(ws, "reply")) + "".join(msg_texts(ws, "reply_append"))
        assert "上海" in joined, f"窗口结束应提交并走新回复, got {joined!r}"
        assert session.supplement_state is None
        assert session.turn_generation >= 1
        print("[PASS] t10 补充窗口：暂断不立即提交，窗口结束才提交 → 新回复")
    finally:
        teardown(patchers)


async def t11_supplement_merge_same_turn():
    """补充窗口：窗口内续说 → 同一 turn 合并（revision+1、窗口重置），最终只提交一次"""
    patchers, patches = setup_patches(final_text="顺便再查一下北京", reply_text="[开心]北京多云，20 度")
    try:
        ws = MockWs()
        session = ConversationSession()
        session.state = "speaking"
        session.pending_reply_text = "旧播报内容"
        pre_roll = base64.b64encode(pcm_ms(256, 200)).decode()
        with um.patch.object(main_mod, "_confirm_real_speech", lambda c, k: True):
            await handle_speech_start(ws, session, pre_roll_b64=pre_roll, is_playing=True)
        assert session.supplement_state is None  # 端点判定接管
        session.is_user_speaking = True
        with um.patch.object(main_mod, "smart_turn", UnavailableJudge(0.3)):
            await handle_speech_end(ws, session, audio_b64=_seg_b64())
        assert session.supplement_state == "soft_ended"
        # 窗口内用户继续说话（soft_ended → 回补充收话，同一 turn）
        await handle_speech_start(ws, session, pre_roll_b64=None, is_playing=False)
        assert session.supplement_state == "pending", "窗口内续说应回补充收话"
        assert session.turn_revision == 1, "补充应 revision+1"
        # 补充说话结束 → 端点复查（仍未完 p≤0.5）→ 再次 SOFT_ENDED（重置窗口）→ 窗口结束提交
        with um.patch.object(main_mod, "smart_turn", UnavailableJudge(0.3)):
            await handle_speech_end(ws, session, audio_b64=_seg_b64())
        assert session.supplement_state == "soft_ended"
        await wait_turn_submitted(session)
        joined = "".join(msg_texts(ws, "reply")) + "".join(msg_texts(ws, "reply_append"))
        assert "北京" in joined, f"合并后的同一 turn 应走新回复, got {joined!r}"
        assert session.turn_generation >= 1
        print("[PASS] t11 补充窗口：窗口内续说 → 同一 turn 合并（rev+1），最终只提交一次")
    finally:
        teardown(patchers)


async def t12_supplement_window_filler_resume():
    """补充窗口结束后语义无效（语气词）且挂起保留 → 恢复重播"""
    patchers, patches = setup_patches(final_text="嗯", reply_text="[开心]临时")
    try:
        ws = MockWs()
        session = ConversationSession()
        session.state = "speaking"
        session.pending_reply_text = "旧播报内容"
        pre_roll = base64.b64encode(pcm_ms(256, 200)).decode()
        with um.patch.object(main_mod, "_confirm_real_speech", lambda c, k: True):
            await handle_speech_start(ws, session, pre_roll_b64=pre_roll, is_playing=True)
        session.is_user_speaking = True
        with um.patch.object(main_mod, "smart_turn", UnavailableJudge(0.3)):
            await handle_speech_end(ws, session, audio_b64=_seg_b64())
        assert session.supplement_state == "soft_ended"
        await wait_turn_submitted(session)
        joined = "".join(msg_texts(ws, "reply"))
        assert "旧播报内容" in joined, f"窗口结束语义无效应恢复重播旧内容, got {joined!r}"
        print("[PASS] t12 补充窗口结束 + 语义无效 → 恢复重播")
    finally:
        teardown(patchers)


async def t13_endpoint_finished_direct_commit():
    """端点检测（正常收话链路）：smart-turn 判已说完(p>0.5) → 直接提交，不开补充窗口"""
    patchers, patches = setup_patches(final_text="今天气温多少", reply_text="[开心]今天 28 度")
    try:
        ws = MockWs()
        session = ConversationSession()
        session.state = "listening"
        await handle_speech_start(ws, session, pre_roll_b64=None, is_playing=False)
        assert patches["asr"].started
        session.is_user_speaking = True
        with um.patch.object(main_mod, "smart_turn", UnavailableJudge(0.9)):  # p>0.5 → 已说完
            await handle_speech_end(ws, session, audio_b64=_seg_b64())
        await wait_turn_submitted(session)
        types = ws_types(ws)
        assert "timing" in types, f"p>0.5 应直接提交（进 LLM，出 timing）, got {types}"
        assert session.supplement_state is None, "说完不应开补充窗口"
        joined = "".join(msg_texts(ws, "reply")) + "".join(msg_texts(ws, "reply_append"))
        assert "28" in joined, f"回复内容异常: {joined!r}"
        print("[PASS] t13 端点：p>0.5 已说完 → 直接提交（不开窗口）")
    finally:
        teardown(patchers)


async def t14_endpoint_unfinished_window_commit():
    """端点检测（正常收话链路）：smart-turn 判可能未完(p≤0.5) → 开补充窗口，无补充 → 窗口结束提交"""
    patchers, patches = setup_patches(final_text="今天气温多少", reply_text="[开心]今天 28 度")
    try:
        ws = MockWs()
        session = ConversationSession()
        session.state = "listening"
        await handle_speech_start(ws, session, pre_roll_b64=None, is_playing=False)
        session.is_user_speaking = True
        with um.patch.object(main_mod, "smart_turn", UnavailableJudge(0.3)):
            await handle_speech_end(ws, session, audio_b64=_seg_b64())
        assert session.supplement_state == "soft_ended", "p≤0.5 应开补充窗口"
        await asyncio.sleep(0.02)
        assert "timing" not in ws_types(ws), "窗口内不应提前提交"
        await wait_turn_submitted(session)
        assert "timing" in ws_types(ws), "窗口结束应提交"
        assert session.supplement_state is None
        print("[PASS] t14 端点：p≤0.5 可能未完 → 开窗，无补充 → 窗口结束提交")
    finally:
        teardown(patchers)


async def t15_endpoint_supplement_merge():
    """端点检测：未完开窗 → 窗口内续说合并（rev+1）→ 再次说完(p>0.5) → 提交同一 turn 一次"""
    patchers, patches = setup_patches(final_text="顺便北京呢", reply_text="[开心]北京多云，20 度")
    try:
        ws = MockWs()
        session = ConversationSession()
        session.state = "listening"
        await handle_speech_start(ws, session, pre_roll_b64=None, is_playing=False)
        session.is_user_speaking = True
        with um.patch.object(main_mod, "smart_turn", UnavailableJudge(0.3)):
            await handle_speech_end(ws, session, audio_b64=_seg_b64())
        assert session.supplement_state == "soft_ended"
        # 窗口内续说（同一 turn 补充）
        await handle_speech_start(ws, session, pre_roll_b64=None, is_playing=False)
        assert session.supplement_state == "pending"
        assert session.turn_revision == 1
        assert session._rejudge_task is None, "续说合并应取消尾静音重判"
        # 续说结束，端点复查判"已说完" → 直接提交（一次）
        with um.patch.object(main_mod, "smart_turn", UnavailableJudge(0.9)):
            await handle_speech_end(ws, session, audio_b64=_seg_b64())
        await wait_turn_submitted(session)
        types = ws_types(ws)
        assert "timing" in types, f"续说完应提交一次, got {types}"
        joined = "".join(msg_texts(ws, "reply")) + "".join(msg_texts(ws, "reply_append"))
        assert "北京" in joined, f"同一 turn 合并后应出新回复, got {joined!r}"
        assert session.supplement_state is None
        print("[PASS] t15 端点：未完开窗→续说合并(rev+1)→说完提交一次")
    finally:
        teardown(patchers)


async def t16_rejudge_early_commit():
    """端点+尾静音重判：首判未完(p=0.3)开窗 → 收集到真实尾静音 → 重判 p>阈值 → 提前提交"""
    patchers, patches = setup_patches(final_text="帮我查天气", reply_text="[开心]好的，稍等")
    try:
        ws = MockWs()
        session = ConversationSession()
        session.state = "listening"
        await handle_speech_start(ws, session, pre_roll_b64=None, is_playing=False)
        session.is_user_speaking = True
        # 补丁必须覆盖窗口期：重判（~20ms）与断言（~60ms）都发生在 handle_speech_end 返回之后
        with um.patch.object(main_mod, "smart_turn", SeqJudge([0.3, 0.9])):  # 首判未完→重判已完
            await handle_speech_end(ws, session, audio_b64=_seg_b64())
            assert session.supplement_state == "soft_ended", "首判 p≤阈值 应开补充窗口"
            # 窗口期到达的"真实尾静音"帧（前端持续推帧 → handle_audio_frame 收集）
            await handle_audio_frame(ws, session, pcm_ms(50))
            await asyncio.sleep(0.06)  # > REJUDGE(20ms) 且 < 窗口(80ms)
            assert "timing" in ws_types(ws), "重判 p>阈值 应提前提交（不等 80ms 窗口结束）"
            assert session.supplement_state is None
            assert session._rejudge_task is None
            await wait_turn_submitted(session)
        joined = "".join(msg_texts(ws, "reply")) + "".join(msg_texts(ws, "reply_append"))
        assert "稍等" in joined, f"提前提交应出新回复, got {joined!r}"
        print("[PASS] t16 端点：首判未完开窗 → 真实尾静音重判 p>阈值 → 提前提交")
    finally:
        teardown(patchers)


async def t17_rejudge_still_low_window_commits():
    """端点+尾静音重判：重判仍 p≤阈值 → 维持窗口，窗口结束才提交"""
    patchers, patches = setup_patches(final_text="帮我查天气", reply_text="[开心]好的，稍等")
    try:
        ws = MockWs()
        session = ConversationSession()
        session.state = "listening"
        await handle_speech_start(ws, session, pre_roll_b64=None, is_playing=False)
        session.is_user_speaking = True
        with um.patch.object(main_mod, "smart_turn", SeqJudge([0.3, 0.3])):  # 重判仍未完
            await handle_speech_end(ws, session, audio_b64=_seg_b64())
            assert session.supplement_state == "soft_ended"
            await handle_audio_frame(ws, session, pcm_ms(50))  # 有尾静音进来
            await asyncio.sleep(0.05)  # REJUDGE(20ms) 已过
            assert "timing" not in ws_types(ws), "重判仍 p≤阈值 不应提前提交"
            assert session.supplement_state == "soft_ended", "重判未完 → 应维持补充窗口"
            await wait_turn_submitted(session)
        assert "timing" in ws_types(ws), "窗口结束应提交"
        assert session.supplement_state is None
        print("[PASS] t17 端点：重判仍 p≤阈值 → 维持窗口 → 窗口结束提交")
    finally:
        teardown(patchers)


async def t18_window_incremental_feed():
    """增量 ASR 验证：未说完开窗后，窗口期到达的帧持续喂 ASR（不阻塞、不丢弃、不重启会话），
    窗口结束 finalize 带入全部音频并进 LLM"""
    patchers, patches = setup_patches(final_text="帮我查天气", reply_text="[开心]好的，稍等")
    try:
        ws = MockWs()
        session = ConversationSession()
        session.state = "listening"
        await handle_speech_start(ws, session, pre_roll_b64=None, is_playing=False)
        session.is_user_speaking = True
        asr = patches["asr"]
        fed_before = len(asr.fed)
        with um.patch.object(main_mod, "smart_turn", UnavailableJudge(0.3)):
            await handle_speech_end(ws, session, audio_b64=_seg_b64())
        assert session.supplement_state == "soft_ended"
        # 窗口期前端仍持续推帧 → 应继续 feed 进同一 ASR 会话（增量）
        await handle_audio_frame(ws, session, pcm_ms(100))
        await handle_audio_frame(ws, session, pcm_ms(100))
        await asyncio.sleep(0.02)  # 让帧到达
        assert len(asr.fed) > fed_before, "窗口期帧应持续喂入 ASR（增量不丢失）"
        assert len(asr.started) == 1, "窗口期不应重启 ASR 会话（同一会话累积）"
        await wait_turn_submitted(session)
        types = ws_types(ws)
        assert "timing" in types, f"窗口结束应提交进入 LLM 链路, got {types}"
        joined = "".join(msg_texts(ws, "reply")) + "".join(msg_texts(ws, "reply_append"))
        assert "稍等" in joined
        print("[PASS] t18 增量 ASR：窗口期帧持续喂同一会话，超时提交带全部音频进 LLM")
    finally:
        teardown(patchers)


async def t19_vad_cancel_keeps_window_session():
    """vad_cancel（前端误报）在补充窗口期间不撤销 ASR 会话 → 窗口结束仍能提交进 LLM"""
    patchers, patches = setup_patches(final_text="帮我查天气", reply_text="[开心]好的，稍等")
    try:
        ws = MockWs()
        session = ConversationSession()
        session.state = "listening"
        await handle_speech_start(ws, session, pre_roll_b64=None, is_playing=False)
        session.is_user_speaking = True
        asr = patches["asr"]
        with um.patch.object(main_mod, "smart_turn", UnavailableJudge(0.3)):
            await handle_speech_end(ws, session, audio_b64=_seg_b64())
        assert session.supplement_state == "soft_ended"
        # 窗口期前端误报 → vad_cancel：修复后应忽略（不 reset ASR / 不复位说话状态）
        await handle_control_message(ws, session, json.dumps({"type": "vad_cancel"}))
        assert asr.reset_calls == 0, "窗口期 vad_cancel 不应 reset ASR 会话"
        assert session.is_user_speaking, "窗口期 vad_cancel 不应复位 is_user_speaking"
        assert session.supplement_state == "soft_ended", "窗口期 vad_cancel 不应改变补充窗口状态"
        await wait_turn_submitted(session)
        types = ws_types(ws)
        assert "timing" in types, f"窗口结束应提交进 LLM（音频未被丢弃）, got {types}"
        print("[PASS] t19 vad_cancel 窗口期不杀 ASR 会话 → 音频不丢、正常提交")
    finally:
        teardown(patchers)


async def t20_vad_cancel_pending_reverts_to_window():
    """续说中（pending）vad_cancel → 回退 soft_ended 并重挂窗口（不杀会话）→ 必提交一次"""
    patchers, patches = setup_patches(final_text="顺便查北京", reply_text="[开心]北京多云")
    try:
        ws = MockWs()
        session = ConversationSession()
        session.state = "listening"
        await handle_speech_start(ws, session, pre_roll_b64=None, is_playing=False)
        session.is_user_speaking = True
        asr = patches["asr"]
        with um.patch.object(main_mod, "smart_turn", UnavailableJudge(0.3)):
            await handle_speech_end(ws, session, audio_b64=_seg_b64())
        assert session.supplement_state == "soft_ended"
        # 窗口内用户续说（soft_ended → pending，合并同一 turn）
        await handle_speech_start(ws, session, pre_roll_b64=None, is_playing=False)
        assert session.supplement_state == "pending"
        assert session.turn_revision == 1
        # 续说疑似误报 → vad_cancel：回退 soft_ended + 重挂窗口（保留会话与音频）
        await handle_control_message(ws, session, json.dumps({"type": "vad_cancel"}))
        assert asr.reset_calls == 0, "续说期 vad_cancel 不应 reset ASR 会话"
        assert session.supplement_state == "soft_ended"
        assert session._supplement_task is not None, "应重挂补充窗口保证提交"
        await wait_turn_submitted(session)
        types = ws_types(ws)
        assert types.count("timing") == 1, f"最终应恰好提交一次, got {types}"
        joined = "".join(msg_texts(ws, "reply")) + "".join(msg_texts(ws, "reply_append"))
        assert "北京" in joined
        print("[PASS] t20 续说中 vad_cancel → 回退窗口重挂（会话保留）→ 提交一次")
    finally:
        teardown(patchers)


async def t21_confirm_retry_rescues_interrupt():
    """静音窗重试：首验拒断（近窗无音频/静音）→ 延时后用新缓存重验通过 → 确认真打断"""
    patchers, patches = setup_patches(final_text="我刚刚在想一个问题", reply_text="[开心]好的")
    try:
        ws = MockWs()
        session = ConversationSession()
        session.state = "speaking"
        session.pending_reply_text = "旧播报内容"
        calls = {"n": 0}

        def fake_confirm(c, k):
            calls["n"] += 1
            return calls["n"] >= 2  # 首次静音拒断，延时重验通过

        with um.patch.object(main_mod, "_confirm_real_speech", fake_confirm), \
             um.patch.object(main_mod, "CONFIRM_RETRY_MS", 20):
            await handle_speech_start(
                ws, session,
                pre_roll_b64=base64.b64encode(pcm_ms(256, 200)).decode(),
                is_playing=True,
            )
        assert calls["n"] == 2, f"应首验+重验共 2 次确认, got {calls['n']}"
        assert "barge_confirm" in ws_types(ws), "重验通过后应确认真打断（不再拒断）"
        assert session.suspended_reply is not None, "应快照挂起旧播报"
        assert session.turn_generation >= 1
        print("[PASS] t21 静音窗重试：首验拒断 → 延时重验通过 → 确认打断")
    finally:
        teardown(patchers)


async def t22_listening_feeds_preroll():
    """非打断（listening 正常收话）也应把 preRoll 喂给 ASR（防首字丢失/错认），且只喂一次"""
    patchers, patches = setup_patches(final_text="那对我觉得挺好的", reply_text="[开心]好的")
    try:
        ws = MockWs()
        session = ConversationSession()
        session.state = "listening"
        pre = pcm_ms(256, 1500)
        pre_b64 = base64.b64encode(pre).decode()
        await handle_speech_start(ws, session, pre_roll_b64=pre_b64, is_playing=False)
        asr = patches["asr"]
        preroll_feeds = [p for (_sid, p) in asr.fed if p == pre]
        assert len(preroll_feeds) == 1, f"非打断收话应恰好喂一次 preRoll（首字不丢）, got {len(preroll_feeds)}"
        assert len(asr.started) == 1
        print("[PASS] t22 非打断 listening：preRoll 恰好喂入一次（首字不丢、无重复）")
    finally:
        teardown(patchers)


async def t23a_silent_material_trusts_frontend():
    """头部基线静音（麦克风采不到球球回声）→ 首验+重验都拒 → 信任前端确认打断"""
    patchers, patches = setup_patches(final_text="朋友突然叫我出去吃饭", reply_text="[开心]好的")
    try:
        ws = MockWs()
        session = ConversationSession()
        session.state = "speaking"
        session.pending_reply_text = "旧播报"
        # 播放期采集被 AGC 压到近静音 → 缓存头部（球球回声区）几乎无声（实测 RMS≈0~22）
        silent_cache = bytearray(pcm_ms(1600, 3))
        await feed_frames_silent_cache(session, silent_cache)
        calls = {"n": 0}

        def fake_confirm(c, k):
            calls["n"] += 1
            return False  # 物理复核永远拒（人声占比恒 0）

        with um.patch.object(main_mod, "_confirm_real_speech", fake_confirm), \
             um.patch.object(main_mod, "CONFIRM_RETRY_MS", 20):
            await handle_speech_start(
                ws, session,
                pre_roll_b64=base64.b64encode(pcm_ms(256, 200)).decode(),
                is_playing=True,
            )
        assert calls["n"] == 2, f"应首验+重验共 2 次, got {calls['n']}"
        assert "barge_confirm" in ws_types(ws), "头部基线静音应信任前端确认打断（不再拒断）"
        assert session.suspended_reply is not None
        print("[PASS] t23a 头部基线静音(无球球回声) → 信任前端 VAD → 确认打断")
    finally:
        teardown(patchers)


async def t23b_nonsilent_reject_kept():
    """负例：近窗非静音 + 物理复核拒 → 保持拒绝（不误信前端），防回声/噪声误断"""
    patchers, patches = setup_patches(final_text="", reply_text="")
    try:
        ws = MockWs()
        session = ConversationSession()
        session.state = "speaking"
        session.pending_reply_text = "旧播报"
        noisy_cache = bytearray(pcm_ms(1600, 800))  # 近窗有明显能量（回声/噪声）
        await feed_frames_silent_cache(session, noisy_cache)

        def fake_confirm(c, k):
            return False

        with um.patch.object(main_mod, "_confirm_real_speech", fake_confirm), \
             um.patch.object(main_mod, "CONFIRM_RETRY_MS", 20):
            await handle_speech_start(
                ws, session,
                pre_roll_b64=base64.b64encode(pcm_ms(256, 200)).decode(),
                is_playing=True,
            )
        assert "barge_reject" in ws_types(ws), "近窗非静音时复核拒绝应保持（不信任前端）"
        assert "barge_confirm" not in ws_types(ws)
        print("[PASS] t23b 近窗非静音 → 复核拒绝保持（不误信前端）")
    finally:
        teardown(patchers)


async def feed_frames_silent_cache(session, cache_bytes):
    """把音频直接灌进 speaking_audio_cache（模拟 state=speaking 时后端缓存积累）"""
    session.speaking_audio_cache.extend(cache_bytes)


# ───────────────────────── 入口 ─────────────────────────

async def run_all():
    # 测试提速：补充窗口压缩到 80ms（生产默认 1200ms 在 main.py 参数区）
    _win = um.patch.object(main_mod, "SUPPLEMENT_WINDOW_MS", 80)
    _win.start()
    # 尾静音重判定时压缩到 20ms（生产默认 600ms）：早于 80ms 窗口触发
    _rj = um.patch.object(main_mod, "SMART_TURN_REJUDGE_MS", 20)
    _rj.start()
    # 全局默认打桩 smart_turn（不可用→direct）：防止"窗口/重判在用例 with 补丁退出后触发"
    # 时泄漏到真实模型（用例内需特定概率的，用 with 补丁覆盖整个异步跨度）
    _st = um.patch.object(main_mod, "smart_turn", UnavailableJudge(None))
    _st.start()
    try:
        await t1_normal_speech_link()
        await t2_barge_suspend_resume()
        await t3_barge_suspend_discard()
        t4_short_cache_uses_strict_ratio()
        t5_long_cache_no_energy_jump_reject()
        t6_long_cache_energy_jump_confirm()
        t6b_baseline_silent_recent_energy_confirm()
        t6c_recent_includes_preroll()
        await t7_stream_partial_early_effective()
        await t8_stream_partial_filler_keeps_suspend()
        t9_no_short_text_filter()
        await t10_supplement_window_commit()
        await t11_supplement_merge_same_turn()
        await t12_supplement_window_filler_resume()
        await t13_endpoint_finished_direct_commit()
        await t14_endpoint_unfinished_window_commit()
        await t15_endpoint_supplement_merge()
        await t16_rejudge_early_commit()
        await t17_rejudge_still_low_window_commits()
        await t18_window_incremental_feed()
        await t19_vad_cancel_keeps_window_session()
        await t20_vad_cancel_pending_reverts_to_window()
        await t21_confirm_retry_rescues_interrupt()
        await t22_listening_feeds_preroll()
        await t23a_silent_material_trusts_frontend()
        await t23b_nonsilent_reject_kept()
    finally:
        _st.stop()
        _rj.stop()
        _win.stop()
    print("\n全部通过（完整链路：barge-in 物理复核(含静音窗重试)→挂起 → 端点判定(SmartTurn)→补充窗口→尾静音重判→增量ASR→流式/最终语义→恢复/丢弃/合并）")


if __name__ == "__main__":
    asyncio.run(run_all())