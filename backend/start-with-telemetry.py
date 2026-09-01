"""
启动脚本：在 telemetry.activate() 之后真正跑 main
用 runpy 跑 main.py，让 __name__ == "__main__" 成立
"""
import os
import sys
import runpy

os.environ.setdefault('EVAL_TELEMETRY', '1')
os.environ.setdefault('EVAL_TELEMETRY_PORT', '3738')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 1) 先激活 telemetry（必须在 import main 之前，否则 hook 不到 ConversationSession 类）
print("[start] activating telemetry...", flush=True)
import telemetry
telemetry.activate()

# 2) 跑 main.py 当 __main__，触发 if __name__ == "__main__" 分支
print("[start] running main.py...", flush=True)
runpy.run_path('main.py', run_name='__main__')