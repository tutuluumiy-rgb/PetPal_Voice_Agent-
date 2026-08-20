import sys, asyncio, tempfile
from types import SimpleNamespace
sys.path.insert(0, r"G:\hello\agent-ai语音\backend")
from session_store import SessionStore
from compaction import CompactionState
from memory_store import MemoryStore
from agent_runtime import run_agent_loop

class Cfg:
    enabled=True; session_idle_timeout_s=5; session_archive_text_threshold=100
    memory_max_tokens=200; l1_max_entries=20; l2_max_entries=20
    l2_consolidate_every_n_sessions=2; l3_rebuild_every_n_consolidations=2; tool_chat_enabled=True

log = open(r"G:\hello\agent-ai语音\backend\tests\_rt_diag.log","w",encoding="utf-8")
def p(*a):
    print(*a, file=log, flush=True)

async def _stream():
    for piece in ["你好","呀"]:
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=piece))])

class C:
    async def create(self, **kwargs):
        p("  create called, stream=", kwargs.get("stream"))
        if kwargs.get("stream"):
            return _stream()
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="你好呀~", tool_calls=None))])
client = SimpleNamespace(chat=SimpleNamespace(completions=C()))

async def main():
    p("start")
    s = SessionStore()
    mem = MemoryStore(memories_dir=tempfile.mkdtemp(prefix="diag3_"), config=Cfg())
    mem.add_l2("用户叫小陈","identity")
    p("before run_agent_loop")
    async for ev in run_agent_loop(client,"m", "chat", "SYSTEM", s, run_id="r", memory_store=mem, compaction_state=CompactionState()):
        p("  event:", ev[0])
    p("DONE")

asyncio.run(main())
log.close()
