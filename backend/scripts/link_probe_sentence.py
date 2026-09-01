"""链路探针：用真实句子「你今天在干嘛」实测两种状态（listening / 打断）的完整链路

场景 A（listening 正常收话）：state=listening → speech_start(正常) → 逐帧喂 ASR →
     speech_end(带真实语音整段) → SmartTurn(真实模型) 判完整 → 提交 → LLM → reply。
场景 B（打断收话）      ：state=speaking（球球在播）→ speech_start(preRoll,is_playing) →
     物理复核(打桩通过) → barge_confirm + 挂起快照 → 逐帧喂 ASR → speech_end(真实语音段) →
     SmartTurn(真实模型) → 提交 → 语义有效 → 丢弃挂起 + 新回复。

音频 = edge-tts 合成「你今天在干嘛」（16k mono，与前端说话段同源）。
ASR/LLM/TTS 用替身（不联网）；SmartTurn 用真实模型（models/smart_turn_v3.onnx）。

用法：cd backend && python scripts/link_probe_sentence.py
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
from tests.test_full_link_rework import (  # noqa: E402
    MockWs,
    MockASR,
    MockTTS,
    FakeLLM,
    FakeVAD,
    pcm_ms,
    ws_types,
    msg_texts,
)

SENTENCE = "你今天在干嘛"


def make_patches():
    """asr/tts/llm/backend_vad 打桩（SmartTurn 保持真实模型）"""
    patchers = []
    asr = MockASR(SENTENCE)
    tts = MockTTS()
    llm = FakeLLM("[开心]我在看看今天有没有什么安排，你呢？")
    vad = FakeVAD(True)
    for name, obj in (("asr", asr), ("tts", tts), ("llm", llm), ("backend_vad", vad)):
        p = um.patch.object(main_mod, name, obj)
        patchers.append(p)
        p.start()
    return patchers, {"asr": asr, "tts": tts}


def teardown(patchers):
    for p in patchers:
        p.stop()


async def wait_settle(session, timeout_s=4.0):
    """等端点判定/窗口/流水线收尾（含尾静音重判与补充窗口，测试内时限加速）"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        done = True
        if session._supplement_task and not session._supplement_task.done():
            done = False
        if session._rejudge_task and not session._rejudge_task.done():
            done = False
        if session.user_speech_task and not session.user_speech_task.done():
            done = False
        if done:
            break
        await asyncio.sleep(0.01)


async def feed_utterance(ws, session, pcm):
    """把整句音频按 100ms 帧喂给后端（模拟前端实时推帧 → 增量 ASR）"""
    chunk = int(16000 * 0.1)
    for i in range(0, len(pcm), chunk):
        await handle_audio_frame(ws, session, (np.clip(pcm[i:i+chunk], -1, 1) * 32767).astype(np.int16).tobytes())


def seg_b64(pcm):
    seg = np.concatenate([pcm, np.zeros(int(16000 * 0.32))])  # +320ms 尾静音（说话段口径）
    return base64.b64encode((np.clip(seg, -1, 1) * 32767).astype(np.int16).tobytes()).decode()


def summary(ws, tag):
    types = ws_types(ws)
    reply = "".join(msg_texts(ws, "reply")) + "".join(msg_texts(ws, "reply_append"))
    print(f"  [{tag}] 消息序列: {types}")
    if reply:
        print(f"  [{tag}] 最终下发回复: {reply!r}")


async def scenario_listening(utterance):
    print(f"\n=== 场景 A：listening 正常收话 ===")
    ws = MockWs()
    session = ConversationSession()
    session.state = "listening"
    await handle_speech_start(ws, session, pre_roll_b64=base64.b64encode(pcm_ms(256, 200)).decode(), is_playing=False)
    print(f"  [A] speech_start → ASR 会话启动: {len(ws_types(ws))} 事件")
    await feed_utterance(ws, session, utterance)
    await handle_speech_end(ws, session, audio_b64=seg_b64(utterance))
    p = main_mod.smart_turn.judge(None)  # 只用于展示 judge 可用性（真实判定在上面事件里）
    _ = p
    await wait_settle(session)
    summary(ws, "A")
    final_ok = "reply" in ws_types(ws) or "reply_append" in ws_types(ws)
    print(f"  [A] 进入 LLM 链路并出回复: {final_ok}")
    return final_ok


async def scenario_barge(utterance):
    print(f"\n=== 场景 B：打断（球球在播）收话 ===")
    ws = MockWs()
    session = ConversationSession()
    session.state = "speaking"
    session.pending_reply_text = "[开心]我今天准备去公园走走，然后…"  # 球球正在播的旧回复
    old_len = len(session.pending_reply_text)
    with um.patch.object(main_mod, "_confirm_real_speech", lambda c, k: True):
        await handle_speech_start(ws, session, pre_roll_b64=base64.b64encode(pcm_ms(256, 200)).decode(), is_playing=True)
    barge_types = ws_types(ws)
    print(f"  [B] speech_start(挂起中) → 物理复核通过 → barge_confirm: {'barge_confirm' in barge_types}")
    print(f"  [B] 挂起快照: {session.suspended_reply is not None}（{old_len} 字旧回复已挂起）")
    await feed_utterance(ws, session, utterance)
    await handle_speech_end(ws, session, audio_b64=seg_b64(utterance))
    await wait_settle(session)
    summary(ws, "B")
    print(f"  [B] 新回复替代旧播报(挂起被丢弃): {session.suspended_reply is None}")
    ok = ("reply" in ws_types(ws) or "reply_append" in ws_types(ws)) and session.suspended_reply is None
    print(f"  [B] 打断后语义有效 → 新回复 ({ok})")
    return ok


async def main():
    # 加速窗口（生产默认：SUPPLEMENT_WINDOW_MS=1200 / REJUDGE_MS=600，不动生产配置）
    _w = um.patch.object(main_mod, "SUPPLEMENT_WINDOW_MS", 600)
    _w.start()
    _r = um.patch.object(main_mod, "SMART_TURN_REJUDGE_MS", 150)
    _r.start()
    try:
        # 真实语音：edge-tts 合成「你今天在干嘛」
        import miniaudio
        import edge_tts
        async def synth(text, out):
            await edge_tts.Communicate(text, "zh-CN-XiaoxiaoNeural", rate="-10%").save(out)
        mp3 = "probe_link_sentence.mp3"
        await synth(SENTENCE, mp3)
        with open(mp3, "rb") as f:
            dec = miniaudio.decode(f.read(), output_format=miniaudio.SampleFormat.SIGNED16,
                                   nchannels=1, sample_rate=16000)
        utterance = np.frombuffer(bytes(dec.samples), dtype=np.int16).astype(np.float32) / 32768.0
        print(f"真实语音: 「{SENTENCE}」时长 {len(utterance)/16000:.2f}s")

        judge = main_mod.smart_turn
        print(f"SmartTurn 真实模型可用: {judge.available}, threshold={judge.threshold}")
        # 直接量一下这句的"话是否完整"得分
        p = judge.judge((np.clip(np.concatenate([utterance, np.zeros(int(16000*0.32))]), -1, 1) * 32767).astype(np.int16).tobytes())
        print(f"该句说话段 p = {p:.3f} → {'>阈值 直接提交' if p and p > judge.threshold else '≤阈值 开补充窗口(重判/超时提交)'}")

        patchers, _ = make_patches()
        try:
            a = await scenario_listening(utterance)
            b = await scenario_barge(utterance)
        finally:
            teardown(patchers)
        print(f"\n结果: listening={a} barge={b} → {'全部通过' if a and b else '存在失败!'}")
    finally:
        _r.stop()
        _w.stop()


if __name__ == "__main__":
    asyncio.run(main())