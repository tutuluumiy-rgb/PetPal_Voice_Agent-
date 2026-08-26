"""探针：验证 LLM 是否会输出 web_search 的 tool_calls

直接调 llm.client.chat.completions.create 带 tools，看第一轮输出。
排查：为什么"帮我搜索新闻"只回"好的我来查一下"而不调工具。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ".")

os.environ["TTS_PROVIDER"] = "ali"

from main import llm, build_system_prompt, mode_state  # noqa: E402
from tools.loader import build_tools_list  # noqa: E402


async def main():
    mode = mode_state.get_mode()
    print(f"mode={mode}, llm={type(llm).__name__}, model={llm.model}", flush=True)

    tools = build_tools_list(mode)
    print(f"tools 列表: {[t['function']['name'] for t in tools]}", flush=True)

    system_prompt = build_system_prompt(mode)
    # 打印系统提示词里工具相关部分（前 300 字符含工具目录?）
    print(f"\nsystem prompt 长度: {len(system_prompt)}", flush=True)
    if "web_search" in system_prompt:
        print("system prompt 含 web_search ✓", flush=True)
    else:
        print("system prompt 不含 web_search（可能工具目录未注入）!", flush=True)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "帮我搜索一下最近的人工智能新闻"},
    ]
    print("\n发送 LLM 请求（带 tools）...", flush=True)
    stream = await llm.client.chat.completions.create(
        model=llm.model,
        messages=messages,
        temperature=0.9,
        max_tokens=15000,
        tools=tools or None,
        stream=True,
        timeout=30,
    )
    content = ""
    tool_names = set()
    async for chunk in stream:
        delta = chunk.choices[0].delta
        if delta and getattr(delta, "content", None):
            content += delta.content
        if delta and getattr(delta, "tool_calls", None):
            for tc in delta.tool_calls:
                if tc.function and tc.function.name:
                    tool_names.add(tc.function.name)
    print(f"\n正文输出: {content!r}", flush=True)
    print(f"工具调用: {tool_names if tool_names else '无'}", flush=True)

    # 第二轮：把工具结果（mock）回填后，看 LLM 是否正常收尾
    print("\n--- 第二轮（带工具结果回填）---", flush=True)
    messages2 = messages + [
        {"role": "assistant", "content": "好的，我来查一下~", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "web_search", "arguments": "{\"query\": \"人工智能新闻\"}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "【mock 搜索】1. AI 新闻：示例"},
    ]
    stream2 = await llm.client.chat.completions.create(
        model=llm.model, messages=messages2, temperature=0.9, max_tokens=15000,
        tools=tools or None, stream=True, timeout=30,
    )
    content2 = ""
    async for chunk in stream2:
        delta = chunk.choices[0].delta
        if delta and getattr(delta, "content", None):
            content2 += delta.content
    print(f"第二轮正文输出: {content2!r}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())