"""Agent 模式配置：上下文策略 / 轮次上限 / 压缩阈值（按模式独立）

两模式（闲聊/工作）共享同一份会话历史，只是【送模型的上下文视图】和【轮次上限】不同。
切换模式 → 读取当前模式的这套配置。

压缩：按 qwen-flash 上下文窗口（1M token）设 max_context_tokens，
     估算 token 达到 max_context_tokens × 0.7 时自动触发压缩。
"""

from __future__ import annotations

from dataclasses import dataclass

from mode_state import CHAT_MODE, WORK_MODE

# qwen-flash 上下文窗口（1M token）
QWEN_CONTEXT_TOKENS = 1_000_000

# 压缩触发阈值比例：估算 token ≥ max_context_tokens × 0.7 触发压缩
COMPACTION_THRESHOLD_RATIO = 0.7


@dataclass(frozen=True)
class ModeAgentConfig:
    mode: str
    keep_complete_turns: int          # 完整保留的用户轮（Turn）数
    max_sub_turns: int                # 一次 run 内最大模型调用次数（sub_turn 上限）
    drop_old_tool_results: bool       # 是否删除超保留轮以前的工具结果（用占位符）
    context_max_tokens: int           # 上下文预算（送模型的估算上限）
    compaction_threshold: int         # 触发压缩的估算 token 阈值


def _threshold(max_tokens: int) -> int:
    return int(max_tokens * COMPACTION_THRESHOLD_RATIO)


# 两套模式配置（qwen-flash：max 1M，压缩触发 700k）
MODE_CONFIGS = {
    CHAT_MODE: ModeAgentConfig(
        mode=CHAT_MODE,
        keep_complete_turns=20,       # 闲聊完整保留 20 轮
        max_sub_turns=10,             # 闲聊 sub_turn 上限 10
        drop_old_tool_results=False,  # 闲聊完整保留工具结果
        context_max_tokens=QWEN_CONTEXT_TOKENS,
        compaction_threshold=_threshold(QWEN_CONTEXT_TOKENS),
    ),
    WORK_MODE: ModeAgentConfig(
        mode=WORK_MODE,
        keep_complete_turns=10,       # 工作完整保留 10 轮
        max_sub_turns=30,             # 工作 sub_turn 上限 30
        drop_old_tool_results=True,   # 工作模式：更早轮次的工具结果删掉、用占位符
        context_max_tokens=QWEN_CONTEXT_TOKENS,
        compaction_threshold=_threshold(QWEN_CONTEXT_TOKENS),
    ),
}


def get_mode_config(mode: str | None) -> ModeAgentConfig:
    """取指定模式的配置；未知/None 回退到闲聊。"""
    return MODE_CONFIGS.get(mode) or MODE_CONFIGS[CHAT_MODE]
