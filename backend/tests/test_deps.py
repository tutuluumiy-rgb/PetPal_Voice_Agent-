# test_deps.py — 验证 LLM judge / ASR / barge 服务的 import 依赖
import importlib

deps = ["openai", "dotenv", "jieba", "numpy", "wave", "queue"]
for d in deps:
    try:
        m = importlib.import_module(d)
        print(f"[OK]   {d}")
    except ImportError as e:
        print(f"[MISS] {d}: {e}")

print("--- backend services import 检查 ---")
import sys, os
# test_deps.py 在 backend/tests/，service 文件在 backend/ 根
backend_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, backend_root)
# judge_service 顶部无 import 依赖（函数内 import）；asr_service 同
for svc in ["judge_service", "asr_service", "barge_service"]:
    try:
        importlib.import_module(svc)
        print(f"[OK]   {svc}")
    except Exception as e:
        print(f"[FAIL] {svc}: {type(e).__name__}: {e}")