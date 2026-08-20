import sys, tempfile, time
LOG = r"G:\hello\agent-ai语音\backend\tests\_rt3.log"
def p(*a):
    with open(LOG,"a",encoding="utf-8") as f:
        print(time.time(), *a, file=f, flush=True)
p("start")
sys.path.insert(0, r"G:\hello\agent-ai语音\backend")
p("path set")
from memory_store import MemoryStore
p("mem import ok")
from context_builder import build_model_context
p("ctx import ok")
class C: pass
mem = MemoryStore(memories_dir=tempfile.mkdtemp(prefix="rt3_"))
mem.add_l2("用户叫小陈","identity")
p("mem add ok")
b = mem.recall_blocks()
p("recall ok, keys=", list(b.keys()))
cfg = type("Cfg",(),{"drop_old_tool_results":False,"keep_complete_turns":5})
r = build_model_context("SYS", [], cfg(), memory_blocks=b, memory_max_tokens=200)
p("build ok, msgs=", len(r.model_context))
p("DONE")
