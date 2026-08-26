"""深度验证：qwen-flash 是否支持 function calling

多场景测试：
A. 温和措辞："帮我搜索一下近AI新闻"（之前不调）
B. 强制式："你必须调用 web_search 工具"
C. 显式工具名："调用 web_search 搜索 AI 新闻"
D. 完全空消息（只依赖 tools schema）
同时打印：tools 是否正确传给客户端、返回是否有 tool_calls

结论判断：qwen 是"不支持"还是"这次没调"
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ".")

os.environ["LLM_PROVIDER"] = "qwen"
os.environ["QWEN_LLM_MODEL"] = "qwen-flash"

from providers.llm import QwenLLM  # noqa: E402


def make_tools():
    return [{
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "联网搜索，获取实时/外部信息（天气、新闻、百科等）。当用户询问最新资讯、实时数据、或不确定的事实时应调用此工具。",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "搜索关键词"}},
                "required": ["query"],
            },
        },
    }]


async def run_case(llm, tools, case_name, messages):
    print(f"\n=== 场景 {case_name} ===", flush=True)
    try:
        stream = await llm.client.chat.completions.create(
            model=llm.model, messages=messages, temperature=0.9, max_tokens=15000,
            tools=tools, stream=True, timeout=30,
        )
        content = ""
        tool_names = set()
        tool_args = []
        async for chunk in stream:
            delta = chunk.choices[0].delta
            if delta and getattr(delta, "content", None):
                content += delta.content
            if delta and getattr(delta, "tool_calls", None):
                for tc in delta.tool_calls:
                    if tc.function and tc.function.name:
                        tool_names.add(tc.function.name)
                        if tc.function.arguments:
                            tool_args.append(tc.function.arguments)
        print(f"  正文: {content[:60]!r}", flush=True)
        print(f"  工具: {tool_names if tool_names else '无'}", flush=True)
        if tool_args:
            print(f"  参数: {tool_args}", flush=True)
    except Exception as e:
        print(f"  [异常] {type(e).__name__}: {e}", flush=True)


async def main():
    llm = QwenLLM()
    print(f"LLM={type(llm).__name__}, model={llm.model}, base={llm.client.base_url}", flush=True)
    tools = make_tools()
    print(f"tools 数量: {len(tools)}", flush=True)

    # A. 温和
    await run_case(llm, tools, "A 温和（之前不调）", [
        {"role": "system", "content": "你是一个语音助手，可以用工具"},
        {"role": "user", "content": "帮我搜索一下最近的人工智能新闻"},
    ])
    # B. 强制
    await run_case(llm, tools, "B 强制必须调", [
        {"role": "system", "content": "你需要实时信息时必须调用 web_search 工具，禁止编造。"},
        {"role": "user", "content": "搜索最近的人工智能新闻（必须调用 web_search）"},
    ])
    # C. 显式工具名
    await run_case(llm, tools, "C 显式调 web_search", [
        {"role": "system", "content": "你是助手。"},
        {"role": "user", "content": "调用 web_search 工具，query='AI新闻'"},
    ])
    # D. 极简（测试 tools 是否生效）
    await run_case(llm, tools, "D 极简", [
        {"role": "user", "content": "搜AI新闻"},
    ])

    # E. 用项目真实 system prompt + 真实 tools（复现之前不调的场景）
    print("\n=== E 项目真实配置 ===", flush=True)
    os.environ["LLM_PROVIDER"] = "qwen"
    from main import build_system_prompt, mode_state  # noqa: E402
    from tools.loader import build_tools_list  # noqa: E402
    import importlib
    mode = mode_state.get_mode()
    real_tools = build_tools_list(mode)
    real_sys = build_system_prompt(mode)
    print(f"真实 tools: {[t['function']['name'] for t in real_tools]}", flush=True)
    await run_case(llm, real_tools, "E 项目配置（温和）", [
        {"role": "system", "content": real_sys},
        {"role": "user", "content": "帮我搜索一下最近的人工智能新闻"},
    ])
    await run_case(llm, real_tools, "E2 项目配置（强制）", [
        {"role": "system", "content": real_sys + "\n\n用户请求实时信息时，你必须调用 web_search 工具，禁止编造结果。"},
        {"role": "user", "content": "搜索最近的人工智能新闻"},
    ])

    # F. 5 个工具全传，但 system 用简单话（去掉项目超长提示词）—— 隔离是"多工具"还是"长提示词"
    print("\n=== F 5 工具 + 简单 system ===", flush=True)
    await run_case(llm, real_tools, "F 5工具简单系统", [
        {"role": "system", "content": "你是助手，需要时用工具。"},
        {"role": "user", "content": "帮我搜索一下最近的人工智能新闻"},
    ])

    # G. 只传 web_search + calculator 两个（去掉 memory/read）
    print("\n=== G 只传 web_search+calculator ===", flush=True)
    subset = [t for t in real_tools if t["function"]["name"] in ("web_search", "calculator")]
    await run_case(llm, subset, "G 子集2工具", [
        {"role": "system", "content": "你是助手，需要时用工具。"},
        {"role": "user", "content": "帮我搜索一下最近的人工智能新闻"},
    ])

    # H. 项目 system + 项目 tools + 强指令（模拟修复后的 agent.md）
    print("\n=== H 项目配置 + 强指令（候选修复）===", flush=True)
    strong_rule = (
        "\n\n## 工具调用铁律（必须遵守）\n"
        "- 用户请求【实时/最新/外部信息】（新闻、天气、实时价格、百科查证等）时，"
        "【必须】且【只能】调用 web_search 工具获取，严禁凭空编造。\n"
        "- 调工具前先输出一句简短前言（如\"好的，我来查一下~\"）。\n"
        "- 工具返回后基于真实结果回复。"
    )
    await run_case(llm, real_tools, "H 强指令(agent.md式)", [
        {"role": "system", "content": real_sys + strong_rule},
        {"role": "user", "content": "帮我搜索一下最近的人工智能新闻"},
    ])

    # I. 只保留 web_search 相关 tools（运行时按需筛选——候选修复2）
    print("\n=== I 项目 system + 仅 web_search tools ===", flush=True)
    only_search = [t for t in real_tools if t["function"]["name"] == "web_search"]
    await run_case(llm, only_search, "I 项目系统+单工具", [
        {"role": "system", "content": real_sys},
        {"role": "user", "content": "帮我搜索一下最近的人工智能新闻"},
    ])

    print("\n[done]", flush=True)


if __name__ == "__main__":
    asyncio.run(main())