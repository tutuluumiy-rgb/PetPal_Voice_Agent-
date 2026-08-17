"""通用 Agent 工具循环（方案 A：工具自路由）—— 独立文件，便于修改

职责：处理「LLM 决定调用工具 → 执行 → 结果回填 → 继续」的多轮循环。
与具体 LLM provider 解耦：调用方（providers/llm.py）传入 OpenAI 兼容 client + 组装好的 messages + tools。

要改工具调用策略（循环上限、错误重试、进度文本生成、工具拼接顺序），只改本文件，不动 provider。
"""

import json

from tools import execute_tool

# 工具使用指南（注入 system prompt，引导 LLM 工具自路由）
# 想调整 LLM 对工具的「使用分寸」（什么时候该调、调几个、怎么播报进度），改这里
TOOL_GUIDE = """
【工具使用指南】
你可以调用工具来帮主人办事（查天气、联网搜索、计算等），但要聪明地使用：
- 简单问题：不用调工具，直接回答。
- 需要实时/外部信息（天气、新闻、知识查证）：调用对应工具。
- 复杂任务（多步查询、比较、计算组合）：可以连续调用多个工具，每轮调用前用一两句话告诉主人在做什么（如"好的，我来查一下~"）。
- 工具返回后，用口语化的方式把结果讲给主人，不要罗列原始数据。
- 如果工具返回错误或查不到，如实说，不要编造。
"""

# 工具轮 LLM 温度（低一点保证工具调用参数准确）
TOOL_TEMPERATURE = 0.7
# 工具轮无伴随文本时的默认进度占位句
DEFAULT_PROGRESS_TEXT = "好的，我来看看~"


async def run_tool_loop(client, model, messages: list, tools: list,
                        max_loops: int, on_progress=None, is_cancelled=None) -> tuple[list, int]:
    """执行工具自路由循环，直到 LLM 不再调用工具或达到上限。

    参数:
        client: OpenAI 兼容 AsyncClient
        model: 模型名
        messages: 已组装的 messages（末尾是用户消息），会被追加 assistant/tool 消息
        tools: 工具 schema 列表
        max_loops: 工具轮上限（防死循环）
        on_progress: async 回调，每轮工具调用前收到进度文本（用于 TTS 播报）
        is_cancelled: callable，返回 True 时（打断）提前退出循环

    返回:
        (messages, tool_round)：messages 末尾是最终轮前的完整上下文（含工具结果）
    """
    tool_round = 0
    progress_done = False  # 进度播报只播第一轮
    while True:
        # 打断检查：每轮开始前
        if is_cancelled and is_cancelled():
            print("[Agent] 检测到打断，退出工具循环")
            return messages, tool_round

        resp = await client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=TOOL_TEMPERATURE,
            max_tokens=15000,
            stream=False,
            tools=tools,
            tool_choice="auto",
            extra_body={"thinking": {"type": "disabled"}},
        )
        message = resp.choices[0].message
        tool_calls = getattr(message, "tool_calls", None)

        if not tool_calls:
            # LLM 不再调工具 → 进入最终回复
            return messages, tool_round

        tool_round += 1
        if tool_round > max_loops:
            print(f"[Agent] 超过 max_loops={max_loops}，强制进入最终回复")
            return messages, tool_round

        # 进度播报：只播【第一轮】工具调用前的伴随文本（如"好的，我帮你查一下天气"）。
        # 多轮工具调用时，中间轮的伴随文本不播报（静默执行工具），避免"每一轮都说话"。
        if not progress_done:
            progress_text = (message.content or "").strip() or DEFAULT_PROGRESS_TEXT
            if on_progress:
                try:
                    await on_progress(progress_text)
                except Exception:
                    pass
            progress_done = True

        # 回填 assistant 消息（含 tool_calls）→ 执行工具 → 回填 tool 结果
        messages.append({
            "role": "assistant",
            "content": message.content or None,
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in tool_calls
            ],
        })
        for tc in tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = await execute_tool(tc.function.name, args)
            print(f"[Agent] 工具 {tc.function.name}({args}) → {result[:60]}...")
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
