"""通用 Agent 工具循环（两级渐进式披露）—— 独立文件，便于修改

职责：处理「LLM 决定调用工具 → 执行 → 结果回填 → 继续」的多轮循环。
与具体 LLM provider 解耦：调用方（providers/llm.py）传入 OpenAI 兼容 client + 组装好的 messages。

两级渐进式（progressive disclosure）：
- 第一级：system prompt 注入「可用工具目录」（build_catalog_md，含每个工具的
  name/description/parameters 结构化描述）——LLM 第一轮就能输出正确的调用字段
- 第二级：LLM 输出 TOOL_CALL 声明块（JSON）→ parse_tool_calls 解析 → 执行工具
  → 结果回填（role=user）→ 继续循环，直到无工具声明进入最终回复

要改工具调用策略（循环上限、错误重试、进度文本生成、声明格式），只改本文件，不动 provider。
"""

from tools import (
    execute_tool,
    parse_tool_calls,
    strip_tool_call_block,
)

# 工具轮 LLM 温度（低一点保证调用参数准确）
TOOL_TEMPERATURE = 0.7
# 工具轮无伴随文本时的默认进度占位句
DEFAULT_PROGRESS_TEXT = "好的，我来看看~"


async def run_tool_loop(client, model, messages: list,
                        max_loops: int, on_progress=None, is_cancelled=None,
                        extra_body: dict | None = None) -> tuple[list, int]:
    """执行工具自路由循环，直到 LLM 不再输出工具声明或达到上限。

    参数:
        client: OpenAI 兼容 AsyncClient
        model: 模型名
        messages: 已组装的 messages（含工具目录的 system prompt），会被追加 assistant/user 消息
        max_loops: 工具轮上限（防死循环）
        on_progress: async 回调，工具调用前收到进度文本（用于 TTS 播报）
        is_cancelled: callable，返回 True 时（打断）提前退出循环
        extra_body: 厂商特殊请求参数（如 DeepSeek 关闭思考），透传给 API

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

        # 第一级：不带 API tools 参数请求，靠 prompt 里的工具目录，LLM 文本输出调用声明
        resp = await client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=TOOL_TEMPERATURE,
            max_tokens=15000,
            stream=False,
            extra_body=extra_body,
        )
        content = resp.choices[0].message.content or ""

        # 解析工具声明（第二级触发条件）
        calls = parse_tool_calls(content)
        if not calls:
            # 没有工具声明 → 进入最终回复
            return messages, tool_round

        tool_round += 1
        if tool_round > max_loops:
            print(f"[Agent] 超过 max_loops={max_loops}，强制进入最终回复")
            return messages, tool_round

        # 进度播报：只播【第一轮】调用前的伴随文本（去掉声明块后的纯文本）
        if not progress_done:
            progress_text = strip_tool_call_block(content) or DEFAULT_PROGRESS_TEXT
            if on_progress:
                try:
                    await on_progress(progress_text)
                except Exception:
                    pass
            progress_done = True

        # 回填 assistant 纯文本（去掉声明块，避免把 JSON 声明当对话内容）→ 执行工具 → 回填结果
        messages.append({"role": "assistant", "content": strip_tool_call_block(content) or None})
        for call in calls:
            name = call.get("tool", "")
            args = call.get("args") or {}
            if not name:
                continue
            result = await execute_tool(name, args)
            print(f"[Agent] 工具 {name}({args}) → {result[:60]}...")
            # 工具结果作为 user 消息回填（不带 API tool_call_id，兼容文本声明模式）
            messages.append({"role": "user", "content": f"【工具 {name} 结果】\n{result}"})
