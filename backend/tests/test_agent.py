"""Agent 工具自路由测试（方案 A）

验证：
1. calculator 安全（正常计算 + 注入拒绝）
2. 简单聊天 → 无工具调用、无进度句，直接流式回复
3. 查天气 → 产生进度播报 + 工具调用 + 最终回复

用法：
  cd backend
  python tests\test_agent.py
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.calculator import calculator  # noqa: E402


async def test_calculator():
    """计算器：正常计算 + 安全拒绝"""
    print("── test_calculator ──")
    r1 = await calculator("(3+5)*2")
    ok1 = "16" in r1
    print(f"  (3+5)*2 -> {r1!r} {'✓' if ok1 else '✗'}")

    r2 = await calculator("sqrt(144)+10")
    ok2 = "22" in r2
    print(f"  sqrt(144)+10 -> {r2!r} {'✓' if ok2 else '✗'}")

    r3 = await calculator("__import__('os').system('dir')")
    ok3 = "错误" in r3
    print(f"  注入 __import__('os') -> {r3!r} {'✓ 已拒绝' if ok3 else '✗ 危险!'}")

    r4 = await calculator("open('x')")
    ok4 = "错误" in r4
    print(f"  注入 open() -> {r4!r} {'✓ 已拒绝' if ok4 else '✗ 危险!'}")

    return all([ok1, ok2, ok3, ok4])


async def test_chat_no_tools():
    """简单聊天：应无工具调用、无进度句，直接流式回复"""
    print("── test_chat_no_tools ──")
    from providers.llm import DeepSeekLLM

    llm = DeepSeekLLM()
    kinds = []
    replies = []
    async for item in llm.agent_chat("你好呀", []):
        kinds.append(item[0])
        if item[0] == "reply":
            replies.append(item[1])
    got_progress = "progress" in kinds
    got_reply = len(replies) > 0
    print(f"  yield 类型: {set(kinds)}")
    print(f"  回复开头: {(''.join(replies))[:40]!r}")
    print(f"  无进度句: {'✓' if not got_progress else '✗ 意外出现进度'} | 有回复: {'✓' if got_reply else '✗'}")
    return (not got_progress) and got_reply


async def test_weather_tool():
    """查天气：应产生进度播报 + 最终回复（隐含工具调用）"""
    print("── test_weather_tool ──")
    from providers.llm import DeepSeekLLM

    llm = DeepSeekLLM()

    progress_called = {"v": False}

    async def _on_progress(t):
        progress_called["v"] = True
        print(f"  [进度] {t}")

    got_reply = False
    replies = []
    async for item in llm.agent_chat(
        "帮我查一下北京明天的天气", [],
        on_progress=_on_progress,
    ):
        if item[0] == "reply":
            got_reply = True
            replies.append(item[1])

    text = "".join(replies)
    print(f"  回复: {text[:80]!r}")
    print(f"  进度播报: {'✓' if progress_called['v'] else '✗'} | 最终回复: {'✓' if got_reply else '✗'}")
    return progress_called["v"] and got_reply


async def main():
    results = {}
    results["calculator"] = await test_calculator()
    results["chat_no_tools"] = await test_chat_no_tools()
    results["weather_tool"] = await test_weather_tool()

    print("=" * 56)
    all_ok = True
    for name, ok in results.items():
        print(f"  {name:<16} {'通过 ✓' if ok else '未通过 ✗'}")
        all_ok = all_ok and ok
    print(f"  综合: {'全部通过' if all_ok else '存在失败'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
