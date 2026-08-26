"""真实大模型识别工具验证：qwen 解析当前 tools schema，生成 tool_calls（不执行）

覆盖 chat 白名单 5 工具 + work 关键工具，逐一核对「期望工具 + 期望关键参数」。
不执行工具，只收集 LLM 返回的 tool_calls 文本。
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ".")

os.environ["LLM_PROVIDER"] = "qwen"

from providers.llm import QwenLLM  # noqa: E402
from tools.loader import build_tools_list  # noqa: E402

CASES = [
    # (模式, 用户问题, 期望工具, 期望参数子串)
    ("chat", "帮我搜索一下最近的人工智能新闻", "web_search", "query"),
    ("chat", "计算一下 (3+5)*2 等于多少", "calculator", "expression"),
    ("chat", "记住我喜欢喝无糖美式咖啡", "memory_add", "preference"),
    ("chat", "忘掉我之前说的关于咖啡的记忆", "memory_forget", "text"),
    ("chat", "读取一下项目里的 README 文件", "read", "path"),
    ("chat", "北京明天天气怎么样", "get_weather", "city"),
    ("work", "北京明天天气怎么样", "get_weather", "city"),
    ("work", "帮我查一下今天的气温（深圳）", "get_weather", "city"),
    ("work", "新建一个文件 notes.txt，内容写 hello", "write", "content"),
]


async def run_case(llm, tools, q, expect_name, expect_arg):
    messages = [
        {"role": "system", "content": "你是语音助手，需要依据用户请求调用工具完成任务；工具参数必须按 schema 填写。"},
        {"role": "user", "content": q},
    ]
    content = ""
    stream_tools = {}  # index -> {name, args}（流式 arguments 分片累积，与 agent_runtime 同法）
    try:
        stream = await llm.client.chat.completions.create(
            model=llm.model, messages=messages, temperature=0.9, max_tokens=15000,
            tools=tools, stream=True, timeout=40,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta
            if delta and getattr(delta, "content", None):
                content += delta.content
            if delta and getattr(delta, "tool_calls", None):
                for tc in delta.tool_calls:
                    entry = stream_tools.setdefault(tc.index, {"name": "", "args": ""})
                    if tc.function and getattr(tc.function, "name", None):
                        entry["name"] += tc.function.name
                    if tc.function and getattr(tc.function, "arguments", None):
                        entry["args"] += tc.function.arguments
    except Exception as e:
        print(f"  [异常] {type(e).__name__}: {e}")
        return False

    calls = [{"name": v["name"], "args": v["args"]} for v in stream_tools.values() if v["name"]]

    got = calls[0] if calls else None
    ok_name = got and got["name"] == expect_name
    ok_arg = ok_name and expect_arg in (got["args"] or "")
    try:
        args_pretty = json.dumps(json.loads(got["args"]), ensure_ascii=False) if got and got["args"] else ""
    except Exception:
        args_pretty = got["args"] if got else ""
    status = "✅" if (ok_name and ok_arg) else "❌"
    print(f"{status} [{q[:22]}] 期望={expect_name}({expect_arg}=…) 实际={got['name'] if got else '无'} {args_pretty[:90]}")
    if not ok_name:
        print(f"     正文开头: {content[:50]!r}")
    return ok_name and ok_arg


async def main():
    llm = QwenLLM()
    print(f"LLM={type(llm).__name__} model={llm.model}")
    pass_n = 0
    fail = []
    for mode, q, en, ea in CASES:
        tools = build_tools_list(mode)
        names = [t["function"]["name"] for t in tools]
        print(f"--- [{mode}] 可用工具 {names}")
        if await run_case(llm, tools, q, en, ea):
            pass_n += 1
        else:
            fail.append((mode, q, en))
    print(f"\n结果: {pass_n}/{len(CASES)} 通过")
    if fail:
        print("未通过:", fail)


if __name__ == "__main__":
    asyncio.run(main())