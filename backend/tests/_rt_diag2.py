import sys, asyncio, tempfile
from types import SimpleNamespace
sys.path.insert(0, r"G:\hello\agent-ai语音\backend")
from session_store import SessionStore
from compaction import CompactionState
from memory_store import MemoryStore
from agent_runtime import run_agent_loop

LOG = r"G:\hello\agent-ai语音\backend\tests\_rt_diag2.log"
def p(*a):
    with open(LOG,"a",encoding="utf-8") as f:
        print(*a, file=f, flush=True)

class Cfg:
    enabled=True; session_idle_timeout_s=5; session_archive_text_threshold=100
    memory_max_tokens=200; l1_max_entries=20; l2_max_entries=20
    l2_consolidate_every_n_sessions=2; l3_rebuild_every_n_consolidations=2; tool_chat_enabled=True

async def _stream():
    p("    stream generator start")
    for piece in ["你好","呀"]:
        p("    yield", piece)
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=piece))])

class C:
    async def create(self, **kwargs):
        p("  create called stream=", kwargs.get("stream"))
        if kwargs.get("stream"):
            return _stream()
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="你好呀~", tool_calls=None))])

client = SimpleNamespace(chat=SimpleNamespace(completions=C()))

async def main():
    p("start")
    s = SessionStore()
    mem = MemoryStore(memories_dir=tempfile.mkdtemp(prefix="diag4_"), config=Cfg())
    mem.add_l2("用户叫小陈","identity")
    p("before run_agent_loop")
    async def runner():
        async for ev in run_agent_loop(client,"m", "chat", "SYSTEM", s, run_id="r", memory_store=mem, compaction_state=CompactionState()):
            p("  event:", ev[0])
        p("runner done")
    try:
        await asyncio.wait_for(runner(), timeout=5)
    except asyncio.TimeoutError:
        p("TIMEOUT after 5s")
    p("main done")

asyncio.run(main())
