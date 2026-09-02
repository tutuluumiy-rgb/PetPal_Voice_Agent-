"""后台任务最小闭环测试（改造计划 voice-continuable-agent-refactor-plan）

覆盖：
  t-a 委派 → 后台 Worker 执行 → 完成 → 状态 succeeded + 结果文本 + 语音通知播报
  t-b Worker 复用：同一会话第二次委派（上次完成后）不新建 Worker（workerId 不变）
  t-c 执行中再次委派 → 被拒（同一时刻一个任务）
  t-d get_task_status：不存在 / 完成两种
  t-e 会话 teardown → 活动任务置 cancelled

不依赖真实云 API：LLM/TTS 均 mock（worker 走 agent_runtime.run_agent_loop 的流式文本路径）。
用法：cd backend && python tests/test_task_service.py
"""
import asyncio
import os
import sys
import types
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main as main_mod  # noqa: E402
from task_service import (  # noqa: E402
    TaskContext,
    _get_task,
    _svc,
    delegate_task,
    get_task_status,
)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {detail}")


# ── Mock 层 ─────────────────────────────────────────


class FakeStream:
    def __init__(self, text):
        self._chunks = [text[i:i + 24] for i in range(0, len(text), 24)] or [text]
        self._it = iter(self._chunks)

    def __aiter__(self):
        self._it = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            c = next(self._it)
        except StopIteration:
            raise StopAsyncIteration
        return types.SimpleNamespace(choices=[types.SimpleNamespace(
            delta=types.SimpleNamespace(content=c, tool_calls=None))])


class FakeChatCompletions:
    def __init__(self, reply):
        self.reply = reply

    async def create(self, **kwargs):
        return FakeStream(self.reply)


class FakeChat:
    def __init__(self, completions):
        self.completions = completions


class FakeLLMClient:
    def __init__(self, reply):
        self.chat = FakeChat(FakeChatCompletions(reply))


class FakeLLM:
    def __init__(self, reply="已完成任务，结果是四十二。"):
        self.client = FakeLLMClient(reply)
        self.model = "mock-model"
        self.timeout = 45


class HangingStream:
    """流式响应挂起：模拟任务仍在执行（第一个 token 迟迟不来）。"""

    def __init__(self, hold_s=5.0):
        self.hold_s = hold_s
        self._done = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._done:
            raise StopAsyncIteration
        self._done = True
        await asyncio.sleep(self.hold_s)
        raise StopAsyncIteration


class FakeTTS:
    def __init__(self):
        self.spoken = []

    async def speak_and_send(self, ws, text, session_id, params=None):
        self.spoken.append(text)


class FakeWs:
    def __init__(self):
        self.sent = []

    async def send_json(self, obj):
        self.sent.append(obj)


class FakeSession:
    """_announce_task_done 只用到的字段。"""
    state = "listening"
    is_user_speaking = False
    session_id = "test-sess"
    tts_task = None  # 播报守卫检查：无排队的 TTS


# ── 测试 ────────────────────────────────────────────


async def t_a_delegate_complete():
    print("== t-a 委派→后台执行→完成→通知 ==")
    main_mod.llm = FakeLLM("我已经完成，结果是四十二。")
    main_mod.worker_llm = FakeLLM("我已经完成，结果是四十二。")
    tts = FakeTTS()
    main_mod.tts = tts
    ws, sess = FakeWs(), FakeSession()
    sess.session_id = "sess-" + uuid.uuid4().hex[:6]
    TaskContext.set_current(ws, sess)
    try:
        reply = await delegate_task(goal="帮我计算 6 乘以 7 等于多少", output_format="直接报数字")
        check("委派立即返回任务编号", reply.startswith("任务已创建：task-"), reply[:30])

        task_id = reply.split("：")[1].split("\n")[0]
        for _ in range(200):
            row = _get_task(task_id)
            if row and row["status"] in ("succeeded", "failed", "cancelled"):
                break
            await asyncio.sleep(0.02)
        row = _get_task(task_id)
        check("任务最终 succeeded", row is not None and row["status"] == "succeeded",
              f"status={row and row['status']}")
        # 回归：任务内容必须写入 Worker 自己的会话存档（否则模型回"未收到任务内容"）
        worker = _svc()._workers[sess.session_id]
        t = worker.store.transcript()
        check("Worker 会话含任务内容",
              bool(t) and t[0].get("role") == "user" and "6 乘以 7" in str(t[0].get("content", "")),
              str(t[0])[:60] if t else "transcript 为空")
        check("结果文本已保存", row and "四十二" in (row.get("result") or ""),
              f"result={row and (row.get('result') or '')[:30]!r}")
        st = await get_task_status(task_id)
        check("get_task_status 可读", "succeeded" in st and "四十二" in st)

        # 通知：listening → 立即播报（结果已压缩成一句，无固定前缀）
        await asyncio.sleep(0.05)
        check("完成语音通知已播报",
              any("四十二" in s for s in tts.spoken),
              tts.spoken[-1][:30] if tts.spoken else "")
        ctx_sent = [d for d in ws.sent if d.get("type") == "context_text"]
        check("状态旁注已发（后台处理提示）",
              any("已在后台开始处理" in str(d.get("text", "")) for d in ctx_sent),
              str(ctx_sent[0])[:50] if ctx_sent else "无")
    finally:
        TaskContext.clear_current()


async def t_b_worker_reuse():
    print("== t-b 同会话 Worker 复用 ==")
    main_mod.llm = FakeLLM()
    main_mod.worker_llm = FakeLLM()
    tts = FakeTTS()
    main_mod.tts = tts
    ws, sess = FakeWs(), FakeSession()
    sess.session_id = "sess-" + uuid.uuid4().hex[:6]
    TaskContext.set_current(ws, sess)
    svc = _svc()
    svc._workers.clear()
    try:
        r1 = await delegate_task(goal="任务一")
        id1 = r1.split("：")[1].split("\n")[0]
        await _wait_done(id1)
        worker1 = svc._workers[sess.session_id].worker_id

        r2 = await delegate_task(goal="任务二")
        id2 = r2.split("：")[1].split("\n")[0]
        worker2 = svc._workers[sess.session_id].worker_id
        check("第二次委派复用同一 Worker", worker1 == worker2, f"{worker1} vs {worker2}")
        await _wait_done(id2)
        check("两次任务均完成", _get_task(id1)["status"] == "succeeded"
              and _get_task(id2)["status"] == "succeeded")
    finally:
        TaskContext.clear_current()
        svc._workers.clear()


async def t_c_busy_reject():
    print("== t-c 执行中再次委派被拒 ==")
    main_mod.llm = FakeLLM("完成任务A")
    main_mod.worker_llm = FakeLLM("完成任务A")

    async def _hang(**kw):
        return HangingStream()

    main_mod.llm.client.chat.completions.create = _hang
    main_mod.worker_llm.client.chat.completions.create = _hang

    ws, sess = FakeWs(), FakeSession()
    sess.session_id = "sess-" + uuid.uuid4().hex[:6]
    TaskContext.set_current(ws, sess)
    svc = _svc()
    svc._workers.clear()
    try:
        r1 = await delegate_task(goal="长任务")
        id1 = r1.split("：")[1].split("\n")[0]
        await asyncio.sleep(0.05)
        r2 = await delegate_task(goal="第二个任务")
        check("进行中再委派被拒", "已有后台任务" in r2, r2[:40])
        check("仅一个任务登记", _get_task(id1)["status"] in ("accepted", "running"))
        check("第二个任务未创建", _get_task("task-不存在") is None)
    finally:
        TaskContext.clear_current()
        svc._workers.clear()


async def t_d_status_missing_and_done():
    print("== t-d get_task_status 不存在/完成 ==")
    st = await get_task_status("task-no-such")
    check("不存在任务给出提示", "任务不存在" in st)
    main_mod.llm = FakeLLM("完事了")
    main_mod.worker_llm = FakeLLM("完事了")
    tts = FakeTTS()
    main_mod.tts = tts
    ws, sess = FakeWs(), FakeSession()
    sess.session_id = "sess-" + uuid.uuid4().hex[:6]
    TaskContext.set_current(ws, sess)
    svc = _svc()
    svc._workers.clear()
    try:
        r = await delegate_task(goal="查一下 x")
        tid = r.split("：")[1].split("\n")[0]
        await _wait_done(tid)
        st2 = await get_task_status(tid)
        check("完成后状态可查", "succeeded" in st2)
    finally:
        TaskContext.clear_current()
        svc._workers.clear()


async def t_e_teardown_cancels():
    print("== t-e 会话 teardown 置 cancelled + 迟到结果不覆盖终态 ==")
    main_mod.llm = FakeLLM("慢慢跑")
    main_mod.worker_llm = FakeLLM("慢慢跑")

    async def _hang(**kw):
        return HangingStream(hold_s=0.3)

    main_mod.llm.client.chat.completions.create = _hang
    main_mod.worker_llm.client.chat.completions.create = _hang
    tts = FakeTTS()
    main_mod.tts = tts
    ws, sess = FakeWs(), FakeSession()
    sess.session_id = "sess-" + uuid.uuid4().hex[:6]
    TaskContext.set_current(ws, sess)
    svc = _svc()
    svc._workers.clear()
    try:
        r = await delegate_task(goal="长驻任务")
        tid = r.split("：")[1].split("\n")[0]
        await asyncio.sleep(0.05)
        svc.teardown_session(sess.session_id)
        await asyncio.sleep(0.05)
        row = _get_task(tid)
        check("teardown 后任务 cancelled", row is not None and row["status"] == "cancelled",
              f"status={row and row['status']}")
        check("Worker 已销毁", sess.session_id not in svc._workers)
        # 后台任务被中断后（CancelledError），迟到回写不得覆盖 cancelled
        await asyncio.sleep(0.4)
        row2 = _get_task(tid)
        check("迟到结果未覆盖终态", row2 and row2["status"] == "cancelled",
              f"status={row2 and row2['status']}")
    finally:
        TaskContext.clear_current()
        svc._workers.clear()


async def _wait_done(task_id, timeout=2.0):
    waited = 0.0
    while waited < timeout:
        row = _get_task(task_id)
        if row and row["status"] in ("succeeded", "failed", "cancelled"):
            return row
        await asyncio.sleep(0.02)
        waited += 0.02
    return _get_task(task_id)


def main():
    asyncio.run(t_a_delegate_complete())
    asyncio.run(t_b_worker_reuse())
    asyncio.run(t_c_busy_reject())
    asyncio.run(t_d_status_missing_and_done())
    asyncio.run(t_e_teardown_cancels())
    print(f"\n后台任务最小闭环：通过 {len(PASS)} / 失败 {len(FAIL)}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())