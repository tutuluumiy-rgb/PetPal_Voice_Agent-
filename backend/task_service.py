"""会话级后台任务（Worker Agent）—— TaskService + SessionWorker + TaskContext

实现「voice-continuable-agent-refactor-plan.md」最小闭环：
  - 主 Agent 通过 delegate_task 委派耗时任务，**立即返回 task_id，不等待 Worker**；
  - 一个语音会话一个会话级 Worker（首次委派创建、后续复用），独立 SessionStore；
  - Worker 后台执行（复用 agent_runtime.run_agent_loop，纯执行不播报），完成/失败/取消
    通过语音通知回调（main._announce_task_done，不抢播当前 turn）；
  - TaskService 用 SQLite 保存任务摘要/状态/结果（data/tasks.db），支持 get_task_status 查询。

本版本边界（对应计划的非目标 / 后续阶段）：
  - 不做 task_update 进度流、waiting_input/waiting_permission、request_permission、
    cancel_task、两天无对话销毁 —— 留待后续阶段。
  - Worker 同一时刻只执行一个任务；会话断开（cleanup_session）时取消活动任务并保留记录。
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
import time
import uuid

from session_store import SessionStore

# ── 任务超时（秒）：整个 Worker 任务（多轮工具）的执行上限 ──
TASK_TIMEOUT_S = float(os.getenv("TASK_TIMEOUT_S", "600"))

# ── worker 角色说明（拼在 build_system_prompt("work") 之后）──
WORKER_ROLE = (
    "## 角色\n"
    "你现在是一名【后台任务执行 Agent】：只执行委派给你的任务并输出最终结果，"
    "不需要与用户对话、不需要寒暄、不要反问。直接动手做，做完用简洁的中文输出结果。\n"
    "## 工具使用\n"
    "- 文件操作用 write（写入）/ edit（修改）/ read（读取），**禁止使用 bash**（后台不可执行命令，"
    "bash 不在你的工具列表里）。\n"
    "- 联网查信息用 web_search，查天气用 get_weather，算数用 calculator。\n"
    "- 只输出最终结果文本（供语音播报），不要输出过程日志。"
)

# ── Worker 可用工具集：工作模式全量，但剔除 bash（后台不执行命令）──
from tools.loader import build_tools_list as _build_tools_list
WORKER_TOOLS = [t for t in _build_tools_list("work") if t["function"]["name"] != "bash"]


async def _send_ctx(ws, text: str):
    """向前端消息区发一条上下文旁注（不阻塞，失败静默）。"""
    if ws is None:
        return
    try:
        await ws.send_json({"type": "context_text", "text": text})
    except Exception:
        pass


import re as _re

# 结果里出现这些信号 → 任务未真正完成（不标 succeeded）
_FAIL_SIGNALS = _re.compile(
    r"(错误|失败|无法|不能|不可用|被拒|拒绝|不在工作区|超出工作区|白名单|权限不足|无权限|未找到)"
)


def _looks_failed(text: str) -> bool:
    """判断 Worker 最终结果是否暗示任务未真正完成。

    先剥掉"没有错误/未失败"这类否定语境，再查信号词，降低误判。
    """
    t = _re.sub(r"(没有|无|未|不)[^\s，。；,.!?]{0,4}(错误|失败|异常)", "", text or "")
    return bool(_FAIL_SIGNALS.search(t))


async def _short_summary(goal: str, result: str, client=None, model=None) -> str:
    """把后台任务的完整结果压缩成一句 ≤40 字的播报句（LLM 加工；失败回退截断）。

    用户要求：结果给主 Agent 过一遍、简单回复、不要长篇大论进 TTS。
    完整结果仍保存在 tasks.result（get_task_status 可查全文）。
    """
    prompt = (
        "下面是一条后台任务的执行结果。请用一句（不超过 40 字）口语化中文总结"
        "「完成了什么 + 关键结论/位置」，适合语音播报，不要复述过程细节，不要输出序号列表：\n"
        f"任务目标：{goal}\n执行结果：{result}"
    )
    if client is not None:
        try:
            resp = await client.chat.completions.create(
                model=model, messages=[{"role": "user", "content": prompt}],
                max_tokens=80, temperature=0.4, stream=False,
            )
            choices = getattr(resp, "choices", None) or []
            if choices:
                content = getattr(getattr(choices[0], "message", None), "content", None)
                if isinstance(content, str) and content.strip():
                    return content.strip()[:40]
        except Exception:
            pass
    flat = result.replace("\n", " ").strip()
    return flat[:40]


# ── 当前会话上下文钩子 ─────────────────────────────
# 语音产品单会话：main.py 在 run_agent_loop 前后设置/清理，工具执行期间可读。
class TaskContext:
    _current: dict | None = None
    _lock = threading.Lock()

    @classmethod
    def set_current(cls, ws, session) -> None:
        cls._current = {"ws": ws, "session": session}

    @classmethod
    def clear_current(cls) -> None:
        cls._current = None

    @classmethod
    def get_current(cls) -> dict | None:
        return cls._current


# ── SQLite 持久化 ──────────────────────────────────
_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(_DATA_DIR, "tasks.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    taskId          TEXT PRIMARY KEY,
    sessionId       TEXT NOT NULL,
    workerId        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'accepted', -- accepted|running|succeeded|failed|cancelled
    goal            TEXT NOT NULL,
    summary         TEXT,
    result          TEXT,
    spokenSummary   TEXT,
    error           TEXT,
    revision        INTEGER NOT NULL DEFAULT 1,
    createdAt       REAL NOT NULL,
    updatedAt       REAL NOT NULL
);
"""

_conn: sqlite3.Connection | None = None
_conn_lock = threading.Lock()


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        os.makedirs(_DATA_DIR, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL;")
        _conn.executescript(_SCHEMA)
        _conn.commit()
    return _conn


def _now() -> float:
    return time.time()


def _insert_task(taskId, sessionId, workerId, goal, status="accepted"):
    t = _now()
    with _conn_lock:
        _db().execute(
            "INSERT INTO tasks(taskId,sessionId,workerId,status,goal,createdAt,updatedAt) "
            "VALUES(?,?,?,?,?,?,?)",
            (taskId, sessionId, workerId, status, goal, t, t),
        )
        _db().commit()


def _update_task(taskId, **fields):
    fields["updatedAt"] = _now()
    # 终态不可变（计划 §7：取消后迟到的结果不得覆盖 cancelled；也防止
    # succeeded 之后又被 failed/cancelled 改写）。一旦入终态，后续任何更新都拒绝。
    cur = _db().execute("SELECT status, revision FROM tasks WHERE taskId=?", (taskId,))
    row = cur.fetchone()
    if row is None:
        return
    if row["status"] in ("succeeded", "failed", "cancelled"):
        return
    fields.setdefault("revision", row["revision"] + 1)
    sets = ", ".join(f"{k}=?" for k in fields)
    with _conn_lock:
        _db().execute(f"UPDATE tasks SET {sets} WHERE taskId=?", (*fields.values(), taskId))
        _db().commit()


def _get_task(taskId) -> dict | None:
    row = _db().execute("SELECT * FROM tasks WHERE taskId=?", (taskId,)).fetchone()
    return dict(row) if row else None


def _tasks_of_session(sessionId) -> list[dict]:
    rows = _db().execute(
        "SELECT * FROM tasks WHERE sessionId=? ORDER BY createdAt", (sessionId,)
    ).fetchall()
    return [dict(r) for r in rows]


# ── 会话级 Worker ──────────────────────────────────


class SessionWorker:
    """一个语音会话对应一个后台 Worker；同一时刻只执行一个任务。"""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.worker_id = "taskw-" + uuid.uuid4().hex[:10]
        self.store = SessionStore(self.worker_id)  # 独立会话存档（sessions/taskw-*.jsonl）
        self.current_task_id: str | None = None
        self._run_task: asyncio.Task | None = None

    @property
    def busy(self) -> bool:
        return self.current_task_id is not None

    async def run_task(self, taskId: str, goal: str, detail: str | None,
                       output_format: str | None, client, model, timeout: float = None) -> tuple[str, bool]:
        """在 worker 会话内执行任务。

        返回 (最终文本, had_error)：had_error=True 表示执行中出现过工具错误
        （供完成判定：真正完成才标 succeeded）。
        """
        import sys as _sys
        from prompt_loader import build_system_prompt
        from agent_runtime import run_agent_loop

        print(f"[任务][Worker] run start  task={taskId} worker={self.worker_id} "
              f"goal={goal[:60]}", file=_sys.stderr, flush=True)
        system_prompt = build_system_prompt("work") + "\n\n" + WORKER_ROLE
        user = goal
        if detail:
            user += f"\n补充说明：{detail}"
        if output_format:
            user += f"\n输出要求：{output_format}"
        user += "\n请执行任务并直接输出最终结果（简洁中文，供语音播报）。"

        import time as _time
        had_error = False

        async def _worker_on_tool(stage: str, name: str, call_id: str, text=None):
            nonlocal had_error
            if stage == "start":
                print(f"[任务][Worker][工具] >>> 开始 {name}  # call_id={call_id}  "
                      f"t={_time.strftime('%H:%M:%S')}", file=_sys.stderr, flush=True)
            elif stage == "end":
                res = str(text or "").replace("\n", " ").strip()[:80]
                print(f"[任务][Worker][工具] <<< 结束 {name}  # call_id={call_id}",
                      file=_sys.stderr, flush=True)
                print(f"[任务][Worker][工具]     结果: {res}", file=_sys.stderr, flush=True)
                if res.startswith("错误") or "错误：" in res[:10]:
                    had_error = True

        # ⚠️ 任务内容必须写入 Worker 自己的会话存档：run_agent_loop 不接收外部
        # messages，而是从 session.transcript() 重建上下文（build_model_context）。
        # 之前把 user 任务只放在局部变量里 → Worker 看不到目标 → 回"未收到任务内容"。
        self.store.add("user", user, run_id=f"task-{taskId}", sub_turn=1)
        parts: list[str] = []
        sub_turn_seen = 0
        async for ev in run_agent_loop(
            client, model, "work", system_prompt, self.store,
            run_id=f"task-{taskId}", timeout=timeout, on_tool=_worker_on_tool,
            tools_override=WORKER_TOOLS,
        ):
            kind = ev[0]
            if kind == "sub_turn":
                sub_turn_seen += 1
                print(f"[任务][Worker] sub_turn {ev[1]}  task={taskId}", file=_sys.stderr, flush=True)
            elif kind == "reply":
                parts.append(ev[1])
            elif kind == "tool":
                pass  # 工具调用已由 on_tool 打印
        final = "".join(parts).strip() or "（任务执行完毕，未产生文本结果）"
        print(f"[任务][Worker] run done  task={taskId} sub_turns={sub_turn_seen} "
              f"had_error={had_error} 结果({len(final)}字): {final[:60]}",
              file=_sys.stderr, flush=True)
        return final, had_error


# ── TaskService ────────────────────────────────────


class TaskService:
    def __init__(self):
        self._workers: dict[str, SessionWorker] = {}  # sessionId → worker
        self._jobs: dict[str, asyncio.Task] = {}      # sessionId → 后台执行任务（teardown 时中断）

    def _worker_for(self, session_id: str) -> SessionWorker:
        w = self._workers.get(session_id)
        if w is None:
            w = SessionWorker(session_id)
            self._workers[session_id] = w
        return w

    def create_task(self, goal: str, detail: str | None = None,
                    output_format: str | None = None) -> str:
        import sys as _sys
        ctx = TaskContext.get_current()
        if ctx is None:
            return "错误：当前没有可用的语音会话，无法创建后台任务。"
        session = ctx["session"]
        ws = ctx["ws"]
        session_id = session.session_id
        worker = self._worker_for(session_id)
        if worker.busy:
            print(f"[任务] delegate 被拒（已繁忙） session={session_id} "
                  f"current={worker.current_task_id}", file=_sys.stderr, flush=True)
            return (f"当前已有后台任务（{worker.current_task_id}）正在执行，"
                    "请等它完成或之后再说。")
        task_id = "task-" + uuid.uuid4().hex[:10]
        worker.current_task_id = task_id
        _insert_task(task_id, session_id, worker.worker_id, goal, "accepted")
        print(f"[任务] delegate_task  session={session_id} worker={worker.worker_id} "
              f"task={task_id} goal={goal[:60]} → accepted", file=_sys.stderr, flush=True)
        # 后台执行；完成/失败后由 _run_and_notify 收尾。
        # ⚠️ 快照 ws/session：Worker 完成时主 turn 的 TaskContext 可能已清空，
        #    通知必须基于创建时的引用，不能完成时再读全局钩子。
        self._jobs[session_id] = asyncio.create_task(self._run_and_notify(
            task_id, worker, goal, detail, output_format, ws, session
        ))
        return f"任务已创建：{task_id}\n我会在后台处理，完成后告诉你结果。"

    async def _run_and_notify(self, taskId: str, worker: SessionWorker, goal: str,
                              detail: str | None, output_format: str | None,
                              ws, session):
        import sys as _sys
        import main as _main
        from providers import get_llm_for_mode
        try:
            _update_task(taskId, status="running")
            print(f"[任务] {taskId} → running（Worker 后台执行中）",
                  file=_sys.stderr, flush=True)
            await _send_ctx(ws, "已在后台开始处理，完成后我会告诉你结果。")
            # Worker 固定按工作模式选模型（默认 DeepSeek）；测试可替换 main.worker_llm
            _worker_llm = getattr(_main, "worker_llm", None) or get_llm_for_mode("work")
            client = _worker_llm.client
            model = _worker_llm.model
            timeout = getattr(_worker_llm, "timeout", None)
            text, had_error = await asyncio.wait_for(
                worker.run_task(taskId, goal, detail, output_format, client, model, timeout),
                timeout=TASK_TIMEOUT_S,
            )
            short = await _short_summary(goal, text, client, model)
            if had_error or _looks_failed(text):
                # 未真正完成（工具出错/目标不可达等）：标 failed，不播"已完成"
                _update_task(taskId, status="failed", error=short, result=text)
                print(f"[任务] {taskId} → failed（真正完成判定未通过）: {short}",
                      file=_sys.stderr, flush=True)
                await _announce(ws, session, "failed", short)
            else:
                summary = text.replace("\n", " ").strip()[:80]
                _update_task(taskId, status="succeeded", result=text,
                             spokenSummary=short, summary=summary)
                print(f"[任务] {taskId} → succeeded，播报({len(short)}字): {short}",
                      file=_sys.stderr, flush=True)
                await _announce(ws, session, "succeeded", short)
            print(f"[任务] {taskId} 完成通知已投递", file=_sys.stderr, flush=True)
        except asyncio.CancelledError:
            _update_task(taskId, status="cancelled", error="任务被取消或会话已结束")
            print(f"[任务] {taskId} → cancelled（取消/会话结束）",
                  file=_sys.stderr, flush=True)
        except asyncio.TimeoutError:
            _update_task(taskId, status="failed", error=f"执行超时（{TASK_TIMEOUT_S}s）")
            print(f"[任务] {taskId} → failed（超时 {TASK_TIMEOUT_S}s）",
                  file=_sys.stderr, flush=True)
            await _announce(ws, session, "failed", "后台任务执行超时，已中止。")
        except Exception as e:
            _update_task(taskId, status="failed", error=f"{type(e).__name__}: {e}")
            print(f"[任务] {taskId} → failed（{type(e).__name__}: {e}）",
                  file=_sys.stderr, flush=True)
            await _announce(ws, session, "failed", "后台任务执行失败，请稍后再试。")
        finally:
            if worker.current_task_id == taskId:
                worker.current_task_id = None

    def get_status(self, taskId: str) -> str:
        import sys as _sys
        print(f"[任务] get_task_status task={taskId}", file=_sys.stderr, flush=True)
        row = _get_task(taskId)
        if row is None:
            return f"任务不存在：{taskId}"
        lines = [
            f"任务 {row['taskId']}：状态 {row['status']}",
            f"目标：{row['goal'][:60]}",
        ]
        if row["status"] == "succeeded" and row["spokenSummary"]:
            lines.append(f"结果：{row['spokenSummary'][:120]}")
        elif row["error"]:
            lines.append(f"错误：{row['error'][:100]}")
        return "\n".join(lines)

    def teardown_session(self, session_id: str) -> None:
        """会话断开/删除：中断后台执行、销毁会话级 Worker 并保留任务记录。

        计划 §10.2：删除会话只销毁该 sessionId 对应的 Worker，不影响其他会话。
        中断后由 _run_and_notify 的 CancelledError 分支收尾（终态保护确保不覆盖）。
        """
        import sys as _sys
        print(f"[任务] teardown_session session={session_id}（销毁 Worker/中断任务）",
              file=_sys.stderr, flush=True)
        job = self._jobs.pop(session_id, None)
        if job is not None and not job.done():
            job.cancel()
        worker = self._workers.pop(session_id, None)
        if worker is None:
            return
        if worker.current_task_id:
            _update_task(worker.current_task_id, status="cancelled",
                         error="会话已结束，任务取消")
            print(f"[任务] {worker.current_task_id} 置 cancelled（会话销毁）",
                  file=_sys.stderr, flush=True)
            worker.current_task_id = None


async def _announce(ws, session, status: str, text: str):
    """任务完成/失败语音通知：不抢播——当前正在播报/生成时不打扰，短暂等待后重试。"""
    try:
        import main as _main
        # ws 传入时携带创建时刻的引用；若连接已断开，_announce_task_done 内部 send 抛错即吞
        await _main._announce_task_done(ws, session, status, text)
    except Exception:
        pass


# ── 主 Agent 工具 executor（tools/loader 注册）──


async def delegate_task(goal: str, detail: str | None = None,
                        output_format: str | None = None) -> str:
    """delegate_task 工具实现：创建后台任务，立即返回。"""
    return _svc().create_task(goal, detail=detail, output_format=output_format)


async def get_task_status(task_id: str) -> str:
    """get_task_status 工具实现：查询任务状态/结果。"""
    return _svc().get_status(task_id)


_service: TaskService | None = None


def _svc() -> TaskService:
    global _service
    if _service is None:
        _service = TaskService()
    return _service