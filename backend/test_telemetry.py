"""快速测试 telemetry 模块是否工作"""
import sys
import os

# 设置 env
os.environ['EVAL_TELEMETRY'] = '1'
os.environ['EVAL_TELEMETRY_PORT'] = '3738'

print("[test] importing telemetry...")
sys.path.insert(0, '.')
import telemetry

print(f"[test] telemetry.ENABLED = {telemetry.ENABLED}")
print(f"[test] calling activate()...")

telemetry.activate()

print("[test] done. If you see '[telemetry] activated' above, hook installed.")