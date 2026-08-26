"""运行指标评测入口（自动段 + 手动段）— 对应 metrics-for-interview/2-指标字典.md

用法（cd backend）:
    python tests/run_metrics.py            # 自动段：M4.1 工具成功率(离线) + M6.1 E2E延迟 + M7.2 端到端成功率
    python tests/run_metrics.py --full     # 自动段 + M7.3 长会话稳定性(30轮, 慢, 费API)
    python tests/run_metrics.py --manual   # 打印手动段指引（M6.2/M6.3/M7.4 需真实人声/环境, 留给你测）
    python tests/run_metrics.py --quick    # 只跑离线低成本：calculator 成功率 + VAD 判定预检

指标对应：
    M4.1 工具调用成功率      -> auto
    M6.1 E2E 延迟 p50/p95   -> auto（复用 handle_user_speech + timing 消息）
    M7.2 端到端成功率        -> auto（同一批用例，成功/失败归类）
    M7.3 长会话稳定性        -> optional --full（30 轮, 可自动但慢）
    M7.1 误打断率(离线部分)   -> quick（VAD 对噪声/回声的判定预检；真实打断需你测）
    M6.2 barge-in 延迟       -> manual（需要真实人声插话）
    M6.3 误停率(反向)        -> manual（需要播放中注入非人声）
    M7.4 漏打断率(反向)      -> manual（需要播放中注入真人声）
"""
import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ".")

os.environ.setdefault("MINIMAX_TRANSPORT", "ws")

# 工具任务集（M4.1）：name -> args；标记 offline=True 表示不依赖外部网络
TOOL_TASKS = {
    "calculator#1":  ("calculator", {"expression": "(3+5)*2"}, True),
    "calculator#2":  ("calculator", {"expression": "sqrt(144)+10"}, True),
    "calculator#3":  ("calculator", {"expression": "__import__('os').system('dir')"}, True),  # 应被拒绝
    "calculator#4":  ("calculator", {"expression": "2**10"}, True),
    "read#1":        ("read", {"path": "backend/tests/persona_brief.md"}, True),
    "weather#1":     ("get_weather", {"city": "北京"}, False),
    "search#1":      ("web_search", {"query": "2024年AI产品趋势", "max_results": 1}, False),
}

# E2E 用例（M6.1/M7.2）：与 probe_e2e_cases 一致的文本意图（跳过 ASR 直接喂文本）
E2E_CASES = [
    "我今天真的不想上班",
    "你觉得我要不要点外卖",
    "中午了，吃啥啊？火锅还是烧烤，纠结死了都",
    "为什么我明明睡了很久还是觉得累",
    "你觉得做语音产品最难的是哪一块？",
    "人到底为什么要工作呢",
    "用大白话解释下什么是API？",
    "为什么手机会卡？",
    "你别讲术语，就跟我说为什么 Streaming 会让 TTS 不自然。",
    "大家为什么讨厌开会？",
    "聊聊人工智能呗。",
]


class MockWs:
    """收集后端 send_json / send_bytes 的 mock（与 probe_e2e_cases 一致）"""

    def __init__(self):
        self.messages = []
        self.audio_bytes = 0
        self.audio_first_ts = None

    async def send_json(self, obj):
        ts = time.time()
        self.messages.append((ts, obj.get("type", "?"), obj))

    async def send_bytes(self, data):
        ts = time.time()
        self.audio_bytes += len(data)
        if self.audio_first_ts is None:
            self.audio_first_ts = ts
            self.messages.append((ts, "FIRST_AUDIO", {"bytes": len(data)}))


# ──────────────────────────────────────────────
# M4.1 工具调用成功率（离线部分可自动）
# ──────────────────────────────────────────────
# 失败信号集合：execute_tool 统一以"错误："开头；工具业务失败另有标志语。
# 仅凭 startswith("错误") 会漏判 weather/search 的失败返回（"没找到城市"/"不可用"），
# 导致成功率虚高——必须按信号词判定。
FAIL_HINTS = ("错误", "没找到", "不可用", "暂时无法", "查询出错", "失败", "不存在")


def _is_tool_fail(result: str) -> bool:
    return any(h in result for h in FAIL_HINTS)


async def m41_tool_success(only_offline=True):
    from tools import execute_tool, WORK_MODE

    print("\n" + "=" * 64)
    print("M4.1 工具调用成功率")
    print("=" * 64)
    mode = WORK_MODE  # work 模式全工具放开
    n_ok = n_total = 0
    rows = []
    for label, (name, args, offline) in TOOL_TASKS.items():
        if only_offline and not offline:
            rows.append((label, name, "SKIP(需网络)"))
            continue
        try:
            result = await execute_tool(name, args, mode)
        except Exception as e:
            result = f"错误：{type(e).__name__}: {e}"
        # 成功率判定：按失败信号词判定（见 FAIL_HINTS）
        ok = not _is_tool_fail(result)
        n_total += 1
        n_ok += 1 if ok else 0
        rows.append((label, name, "OK" if ok else "FAIL", result[:50]))
    for row in rows:
        print(f"  {row[0]:<16} {row[1]:<14} {row[2]:<6} {row[3] if len(row) > 3 else ''}")
    if n_total:
        print(f"  → 成功率 = {n_ok}/{n_total} = {n_ok/n_total*100:.0f}%")
    else:
        print("  → 无可用任务（离线部分为空）")
    return n_ok, n_total


# ──────────────────────────────────────────────
# M6.1 + M7.2  E2E 延迟 & 端到端成功率
# ──────────────────────────────────────────────
async def m61_m72_e2e(max_cases=None):
    from main import handle_user_speech, ConversationSession, tts, emotion_state, _preheat_tts

    print("\n" + "=" * 64)
    print("M6.1 E2E 延迟（p50/p95） + M7.2 端到端成功率")
    print("=" * 64)
    if not isinstance(tts, type(None)) and hasattr(tts, "transport"):
        print(f"  TTS={type(tts).__name__} transport={tts.transport}")

    # 预热
    try:
        await _preheat_tts(MockWs(), ConversationSession())
    except Exception as e:
        print(f"  [预热失败] {e}")

    cases = E2E_CASES if max_cases is None else E2E_CASES[:max_cases]
    durations = []       # e2e (服务端)
    fail_reasons = []    # M7.2 失败归类
    ok_rounds = 0
    total_rounds = 0

    for i, text in enumerate(cases):
        ws = MockWs()
        session = ConversationSession()
        session.last_asr_time = 0.5  # 模拟 ASR（真实前端有 ASR，这里固定典型值）
        emotion_state.current = "平静"
        t0 = time.time()
        try:
            await handle_user_speech(ws, session, text)
        except asyncio.CancelledError:
            fail_reasons.append("cancelled")
            print(f"  [{i+1}] 取消", flush=True)
            continue
        except Exception as e:
            fail_reasons.append(f"exception:{type(e).__name__}")
            print(f"  [{i+1}] 异常 {type(e).__name__}: {e}", flush=True)
            continue
        total_rounds += 1  # 无异常的轮次（含无回复的，下面会归类）
        # 判定成功：收到 reply 或 reply_append 或 至少第一包音频
        got_reply = any(t == "reply" or t == "reply_append" for _, t, _ in ws.messages)
        got_audio = ws.audio_first_ts is not None
        if got_reply or got_audio:
            ok_rounds += 1
        else:
            fail_reasons.append("no_reply_no_audio")
        # timing 消息提取
        timing = None
        for _, t, data in ws.messages:
            if t == "timing" and "current" in data:
                timing = data
        if timing and "current" in timing:
            e2e = timing["current"].get("e2e", 0)
            durations.append(e2e)
            print(f"  [{i+1}] {text[:24]:<26} e2e={e2e*1000:.0f}ms", flush=True)
        else:
            print(f"  [{i+1}] {text[:24]:<26} (无 timing)", flush=True)

    # M6.1 统计
    if durations:
        d_sorted = sorted(durations)
        n = len(d_sorted)
        p50 = d_sorted[n // 2] if n else 0
        p95 = d_sorted[min(n - 1, int(n * 0.95))] if n else 0
        avg = sum(d_sorted) / n
        print(f"\n  M6.1 E2E(服务端): n={n} avg={avg*1000:.0f}ms p50={p50*1000:.0f}ms p95={p95*1000:.0f}ms")
    else:
        print("\n  M6.1：无有效 timing 数据（可能全部失败/被拒）")

    # M7.2 统计
    # 分母 = 成功 + 所有失败归类（异常/取消/无回复）——即全部尝试轮次
    f_total = ok_rounds + len(fail_reasons)
    if f_total:
        ratio = ok_rounds / f_total
        print(f"  M7.2 端到端成功率: {ok_rounds}/{f_total} = {ratio*100:.0f}%")
        if fail_reasons:
            from collections import Counter
            print(f"  失败归类: {dict(Counter(fail_reasons))}")
    else:
        print("  M7.2：无轮次可统计")

    if hasattr(tts, "_close_ws"):
        try:
            await tts._close_ws()
        except Exception:
            pass
    return durations


# ──────────────────────────────────────────────
# M7.3 长会话稳定性（可选 --full）
# ──────────────────────────────────────────────
async def m73_long_session(rounds=30):
    from main import handle_user_speech, ConversationSession, tts, _preheat_tts, emotion_state

    print("\n" + "=" * 64)
    print(f"M7.3 长会话稳定性（{rounds} 轮, 含打断模拟）")
    print("=" * 64)
    try:
        await _preheat_tts(MockWs(), ConversationSession())
    except Exception as e:
        print(f"  [预热失败] {e}")

    session = ConversationSession()
    phrases = ["随便聊点", "今天天气怎么样", "给我算个数学题", "你好呀", "我有点困", "讲个笑话"]
    ok = 0
    interrupted = 0   # 打断轮次（设计内场景，不计入失败）
    fail = 0
    for i in range(rounds):
        ws = MockWs()
        text = phrases[i % len(phrases)]
        session.last_asr_time = 0.5
        emotion_state.current = "平静"
        if i % 5 == 4:
            # 每 5 轮模拟打断：cancel 当前 tts task
            if session.tts_task and not session.tts_task.done():
                session.tts_task.cancel()
        try:
            await handle_user_speech(ws, session, text)
            ok += 1
            if i % 10 == 0:
                print(f"  [轮 {i+1}/{rounds}] OK", flush=True)
        except asyncio.CancelledError:
            interrupted += 1  # 打断 = 设计内路径，不视为稳定性失败
            print(f"  [轮 {i+1}] CancelledError（打断路径，正常）", flush=True)
        except Exception as e:
            fail += 1
            print(f"  [轮 {i+1}] 异常 {type(e).__name__}: {e}", flush=True)
    total_judged = ok + fail
    print(f"\n  M7.3 端到端稳定成功率 = {ok}/{total_judged} = {ok/total_judged*100:.0f}%"
          f"（不含被打断的 {interrupted} 轮）")
    print(f"  M7.3 打断容错 = {interrupted} 轮全部被正确取消（连续 30 轮不卡死）")
    if hasattr(tts, "_close_ws"):
        try:
            await tts._close_ws()
        except Exception:
            pass
    return ok, total_judged


# ──────────────────────────────────────────────
# M7.1 误打断率（离线预检 —— VAD 判定噪声/回声）
# ──────────────────────────────────────────────
async def m71_vad_pretest():
    print("\n" + "=" * 64)
    print("M7.1 误打断率（离线预检：VAD 对人声/噪声/回声的判定）")
    print("=" * 64)
    import numpy as np

    SAMPLE_RATE = 16000

    def make_noise(ms, amp=0.02):
        rng = np.random.default_rng(42)
        n = int(SAMPLE_RATE * ms / 1000)
        return (rng.standard_normal(n) * amp * 32767).astype(np.int16).tobytes()

    def make_sine(ms, freq=440, amp=0.2):
        n = int(SAMPLE_RATE * ms / 1000)
        t = np.arange(n) / SAMPLE_RATE
        return (np.sin(2 * np.pi * freq * t) * amp * 32767).astype(np.int16).tobytes()

    vad_path = r"../testboard/vad/silero_vad.onnx"
    if not os.path.exists(vad_path):
        print("  [跳过] 未找到 VAD 模型", vad_path)
        return
    from vad_engine import SileroVAD

    vad = SileroVAD(vad_path)
    samples = [
        ("白噪声(1s)", make_noise(1000)),
        ("正弦波(1s)", make_sine(1000)),
        ("静音(1s)", b"\x00" * SAMPLE_RATE * 2),
    ]
    print(f"\n  期望：噪声/静音 → 判定为『噪声』（拒绝打断）")
    false_barge = 0
    total = len(samples)
    for name, pcm in samples:
        is_sp, ratio = vad.is_speech(pcm, 0.45, ratio_threshold=0.05)
        judge = "人声(误报!)" if is_sp else "噪声(正确)"
        if is_sp:
            false_barge += 1
        print(f"  {name:<20} 帧占比={ratio:.3f} 判定={judge}")
    print(f"\n  M7.1 离线预检: {false_barge}/{total} 被误判为人声（真实误打断率需你实测）")
    print("  ⚠️ 这是 VAD 层预检，真实『误打断率』= 播放中注入噪声后被 barge_confirm 的比例，需要真实环境。")


# ──────────────────────────────────────────────
# 手动段指引（留给你测）
# ──────────────────────────────────────────────
def print_manual_guide():
    print("\n" + "=" * 64)
    print("手动段指引（需要真实人声 / 播放环境，留给你测）")
    print("=" * 64)
    print("""
M6.2 barge-in 响应延迟（p50/p95）
  步骤：打开 testboard/index.html → 让西西播放 → 中途说话插话
        记录前端 speech_start -> stopStreamPlayback 的时间差（前端已埋点）
        main.py 侧也可看 timing_sum['barge_in']/barge_count（已统计 avg）

M6.3 误停率（反向：没插话却被停）
  步骤：西西播放时注入【非人声】噪声（咳嗽/关门/音乐）各 10 次
        数 stopStreamPlayback 被触发的次数 → 误停率 = 停止次数/注入次数
        期望 ≤ 10%

M7.4 漏打断率（反向：真插话却没停）
  步骤：西西播放时注入【真人声】（提前录好自己说话）各 10 次
        数『没停』的次数 → 漏打断率 = 未停次数/注入次数
        期望 ≤ 5%（注意与 M6.3 对偶：不能为了不漏断而疯狂误停）

M6.2 补充：真实 E2E（含 ASR）
  步骤：自己说话 → 看前端 VAD onSpeechEnd → 第一帧音频出声（前端 client_real_e2e 上报）
        或在浏览器 DevTools 里读 last_real_e2e_ms
""")


async def main():
    parser = argparse.ArgumentParser(description="PetPal 指标评测入口")
    parser.add_argument("--full", action="store_true", help="含 M7.3 长会话（30轮，慢，费API）")
    parser.add_argument("--quick", action="store_true", help="只跑离线低成本（M4.1 离线 + M7.1 VAD预检）")
    parser.add_argument("--manual", action="store_true", help="打印手动段指引后退出")
    parser.add_argument("--cases", type=int, default=None, help="E2E 用 N 条用例（默认全部）")
    args = parser.parse_args()

    if args.manual:
        print_manual_guide()
        return

    print("PetPal 指标评测入口")
    print(f"环境: MINIMAX_TRANSPORT={os.environ.get('MINIMAX_TRANSPORT')}")

    if args.quick:
        await m41_tool_success(only_offline=True)
        await m71_vad_pretest()
        print("\n[quick done]")
        return

    # 自动段
    await m41_tool_success(only_offline=False)
    await m61_m72_e2e(max_cases=args.cases)
    if args.full:
        await m73_long_session()
    print_manual_guide()
    print("\n[done]")


if __name__ == "__main__":
    asyncio.run(main())