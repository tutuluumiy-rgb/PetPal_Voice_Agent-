# -*- coding: utf-8 -*-
"""一键修复：清理占用 8001 端口的残留进程。

用法（管理员 PowerShell 或普通终端）：
    python fix_port.py

原理：
    1. 用 netstat 找到所有监听 127.0.0.1:8001 / 0.0.0.0:8001 的 PID
    2. 用 taskkill 强制结束这些进程（释放被旧后端占用的端口）
    3. 复查端口是否已释放
"""
import re
import subprocess
import sys


def run(cmd):
    """执行系统命令并返回 (returncode, stdout, stderr)。"""
    p = subprocess.run(cmd, capture_output=True, text=True, shell=True)
    return p.returncode, p.stdout, p.stderr


def find_port_pids(port):
    """netstat 找到监听指定端口的所有 PID。返回 set[int]；失败返回 None 表示权限不足。"""
    rc, out, err = run(f'netstat -ano | findstr :{port}')
    if rc != 0:
        return None
    pids = set()
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        # 只要处于 LISTENING 的行
        if 'LISTENING' not in line:
            continue
        # 行尾是 PID
        parts = line.split()
        if parts and parts[-1].isdigit():
            pids.add(int(parts[-1]))
    return pids


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "8001"
    print(f"[1/3] 扫描端口 {port} 的监听进程 ...")
    pids = find_port_pids(port)
    if pids is None:
        print("[!] netstat 执行失败（可能无权限）。请用管理员 PowerShell 重试：")
        print(f"    netstat -ano | findstr :{port}")
        return 1
    if not pids:
        print(f"[OK] 端口 {port} 无人占用，无需清理。直接用 python main.py 启动后端即可。")
        return 0

    print(f"[2/3] 发现占用进程 PID: {sorted(pids)}，正在强制结束 ...")
    for pid in pids:
        rc, out, err = run(f'taskkill /PID {pid} /F')
        if rc == 0:
            print(f"  已结束 PID {pid}")
        else:
            print(f"  结束 PID {pid} 失败（{err.strip()}）。可能需要管理员权限。")

    print("[3/3] 复查端口是否已释放 ...")
    pids2 = find_port_pids(port)
    if pids2:
        print(f"[!] 仍有进程占用 {port}: PID {sorted(pids2)}")
        print("    请以【管理员身份】打开 PowerShell，然后运行：")
        print(f"    taskkill /PID {', '.join(map(str, pids2))} /F")
        return 1
    print(f"[OK] 端口 {port} 已释放！现在可启动后端：")
    print("    cd g:/hello/agent-ai语音/backend")
    print("    python main.py")
    print("    启动后浏览器访问 http://127.0.0.1:8001/health 应返回 {\"status\":\"ok\"}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
