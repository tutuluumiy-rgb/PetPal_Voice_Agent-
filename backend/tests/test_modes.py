# -*- coding: utf-8 -*-
"""双模式（闲聊/工作）功能冒烟测试：不联网，验证模式状态、提示词、工具白名单、工作区边界。"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mode_state import ModeState, parse_mode_command, build_switch_context, CHAT_MODE, WORK_MODE
from prompt_loader import build_system_prompt
from tools import loader, execute_tool, is_tool_allowed
from tools import _file_utils as fu


def test_mode_state():
    s = ModeState()
    assert s.get_mode() == CHAT_MODE, "默认应为闲聊"
    assert s.switch(WORK_MODE) == WORK_MODE
    assert s.get_mode() == WORK_MODE
    assert s.switch(CHAT_MODE) == CHAT_MODE
    assert s.get_mode() == CHAT_MODE
    print("[OK] mode_state 默认闲聊 + 手工 switch")


def test_mode_command():
    # 方案 A：必须「切换动词 + 目标模式词」同时命中才切换
    assert parse_mode_command("打开工作模式") == (True, WORK_MODE)
    assert parse_mode_command("切到闲聊模式") == (True, CHAT_MODE)
    assert parse_mode_command("打开一下工作模式") == (True, WORK_MODE)
    assert parse_mode_command("开启一下工作模式") == (True, WORK_MODE)
    assert parse_mode_command("帮我切换成工作模式，写一个ppt") == (True, WORK_MODE)
    assert parse_mode_command("帮我把模式切到闲聊模式") == (True, CHAT_MODE)
    assert parse_mode_command("我想切换到工作模式") == (True, WORK_MODE)
    # 新增动词：改为/改成/改到
    assert parse_mode_command("改成闲聊模式") == (True, CHAT_MODE)
    assert parse_mode_command("改为工作模式") == (True, WORK_MODE)
    assert parse_mode_command("改到工作模式") == (True, WORK_MODE)
    # 取消 toggle：只说"切换模式"没指明目标 → 不动作
    assert parse_mode_command("切换模式") == (False, None)
    # 状态查询：无切换动词，即使含模式词也不切换（走 LLM 回答）
    assert parse_mode_command("你现在是闲聊模式还是工作模式") == (False, None)
    assert parse_mode_command("你现在是什么模式") == (False, None)
    assert parse_mode_command("你是工作模式吗") == (False, None)
    # 普通对话不应触发
    assert parse_mode_command("今天天气怎么样") == (False, None)
    assert parse_mode_command("你喜欢吃什么") == (False, None)
    # 切换状态上下文：不重复播报、继续处理请求
    ctx = build_switch_context(WORK_MODE)
    assert "已经成功切换到工作模式" in ctx and ("不要回复" in ctx or "不用回复" in ctx)
    print("[OK] 语音指令解析（方案A：动词+模式同时命中/状态查询不切/toggle取消）+ 切换上下文")


def test_system_prompts_differ():
    chat_p = build_system_prompt(CHAT_MODE)
    work_p = build_system_prompt(WORK_MODE)
    assert "工作模式" in work_p
    # 闲聊工具目录只有3个，工作全量
    assert "### read" in chat_p and "### calculator" in chat_p
    assert "### bash" not in chat_p, "闲聊模式不该暴露 bash"
    assert "### bash" in work_p
    assert chat_p != work_p
    print("[OK] 两套系统提示词按模式生成（闲聊3工具/工作全量）")


def test_whitelist():
    assert is_tool_allowed("web_search", CHAT_MODE) is True
    assert is_tool_allowed("read", CHAT_MODE) is True
    assert is_tool_allowed("calculator", CHAT_MODE) is True
    assert is_tool_allowed("bash", CHAT_MODE) is False, "闲聊不该允许 bash"
    assert is_tool_allowed("write", CHAT_MODE) is False
    assert is_tool_allowed("bash", WORK_MODE) is True
    print("[OK] 工具白名单（闲聊=搜索/读取/计算，工作=全开）")


async def test_execute_whitelist():
    # 闲聊：calculator 可用，bash 被白名单拦
    r_calc = await execute_tool("calculator", {"expression": "3+5*2"}, mode=CHAT_MODE)
    assert "13" in r_calc, f"calculator 闲聊应可用: {r_calc}"
    r_bash = await execute_tool("bash", {"command": "echo hi"}, mode=CHAT_MODE)
    assert "不可用" in r_bash, f"bash 闲聊应被拒: {r_bash}"
    # 工作：bash 允许（但这里不真跑，仅验证白名单放行逻辑走到执行层）
    assert is_tool_allowed("bash", WORK_MODE)
    print("[OK] 执行层白名单校验（calculator 通过 / bash 闲聊被拒）")


def test_workspace_restriction():
    # 默认锁定工作区
    assert fu.is_workspace_restricted() is True
    # 工作区内文件可读
    in_path = fu.resolve_workspace_path("backend/prompts/personality.md")
    assert in_path.is_file()
    # 越界路径（锁定态）应拒绝
    try:
        fu.resolve_workspace_path("C:/Windows/System32/notepad.exe")
        raise AssertionError("锁定态下越界路径应被拒绝")
    except ValueError:
        pass
    # 放开后可读任意路径
    fu.set_workspace_restricted(False)
    out_path = fu.resolve_workspace_path("C:/Windows/System32/notepad.exe")
    assert out_path.is_file()
    # 恢复锁定
    fu.set_workspace_restricted(True)
    assert fu.is_workspace_restricted() is True
    print("[OK] 工作区权限状态：默认锁定工作区，可手动放开")


def test_read_tool_calculation():
    # read 工具：读工作区内文件返回 ToolOutput → 字符串
    from tools.read import read as read_fn
    out = read_fn("backend/prompts/personality.md", limit=5)
    s = str(out)
    assert "personality" in s or "content" in s, f"read 应返回文件内容: {s[:80]}"
    print("[OK] read 工具在锁工作区内正常执行")


if __name__ == "__main__":
    test_mode_state()
    test_mode_command()
    test_system_prompts_differ()
    test_whitelist()
    asyncio.run(test_execute_whitelist())
    test_workspace_restriction()
    test_read_tool_calculation()
    print("\n全部双模式冒烟测试通过 ✅")
