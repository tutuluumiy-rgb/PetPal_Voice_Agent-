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
# 压缩预算公式余量：history_budget = max_input * ratio * reserve - system - summary
COMPACT_RATIO = 0.7
RESERVE_RATIO = 0.95
# 压缩摘要比例（compact_memory：摘要长度约为原历史的 0.1）
COMPACT_SUMMARY_RATIO = 0.1
# chat 模式轮数触发压缩：完整轮超过该值即触发（兜底，因闲聊 token 难达阈值）
CHAT_MAX_ROUNDS = 15

# ── 记忆模块配置 ─────────────────────────────────────────
# 总开关：False 时不抽取/不注入（保留存储，供查看）
MEMORY_ENABLED = True
# 会话结束判定：回到 idle 后超过该秒数无任何新进展 → 判为一次会话结束，触发 L1 抽取
SESSION_IDLE_TIMEOUT_S = 45
# 会话内容量兜底：本次会话估算 token 达该阈值立即归档抽取（不等静默超时），防超长会话不落
SESSION_ARCHIVE_TEXT_THRESHOLD = 4000
# 注入预算：每轮上下文里给记忆的最大估算 token（超了按 L3>L2>L1 截断）
MEMORY_MAX_TOKENS = 1800
# 各层条目上限
MEMORY_L1_MAX_ENTRIES = 200
MEMORY_L2_MAX_ENTRIES = 200
# 层间流动触发：
#   L1→L2 沉淀：每 N 次会话结束触发一次
MEMORY_L2_CONSOLIDATE_EVERY_N_SESSIONS = 5
#   L2→L3 重写：每 N 次沉淀触发一次
MEMORY_L3_REBUILD_EVERY_N_CONSOLIDATIONS = 5
# 主动记忆工具默认开关（闲聊模式是否开放 memory_add / memory_forget）
MEMORY_TOOL_CHAT_ENABLED = True


@dataclass(frozen=True)
class MemoryConfig:
    """记忆模块运行配置（单一默认实例，可在 main.py 覆盖）。"""
    enabled: bool = MEMORY_ENABLED
    session_idle_timeout_s: float = SESSION_IDLE_TIMEOUT_S
    session_archive_text_threshold: int = SESSION_ARCHIVE_TEXT_THRESHOLD
    memory_max_tokens: int = MEMORY_MAX_TOKENS
    l1_max_entries: int = MEMORY_L1_MAX_ENTRIES
    l2_max_entries: int = MEMORY_L2_MAX_ENTRIES
    l2_consolidate_every_n_sessions: int = MEMORY_L2_CONSOLIDATE_EVERY_N_SESSIONS
    l3_rebuild_every_n_consolidations: int = MEMORY_L3_REBUILD_EVERY_N_CONSOLIDATIONS
    tool_chat_enabled: bool = MEMORY_TOOL_CHAT_ENABLED


# 全局默认记忆配置实例
DEFAULT_MEMORY_CONFIG = MemoryConfig()


def history_budget_tokens(system_prompt_tokens: int, summary_tokens: int,
                          max_input_tokens: int = QWEN_CONTEXT_TOKENS) -> int:
    """上下文拆分预算：可用给「对话历史」的 token 上限。

    公式：max_input * compact_ratio * reserve_ratio - system_prompt_tokens - summary_tokens
    当已占用历史 token > 该值 → 触发拆分/压缩。
    """
    total = max_input_tokens * COMPACT_RATIO * RESERVE_RATIO
    return max(0, int(total - system_prompt_tokens - summary_tokens))


@dataclass(frozen=True)
class ModeAgentConfig:
    mode: str
    keep_complete_turns: int          # 完整保留的用户轮（Turn）数
    max_sub_turns: int                # 一次 run 内最大模型调用次数（sub_turn 上限）
    drop_old_tool_results: bool       # 是否删除超保留轮以前的工具结果（用占位符）
    context_max_tokens: int           # 上下文预算（送模型的估算上限）
    compaction_threshold: int         # 触发压缩的估算 token 阈值
    chat_max_rounds: int = 0          # chat 轮数触发压缩阈值（0 = 不启用轮数触发）


def _threshold(max_tokens: int) -> int:
    return int(max_tokens * COMPACTION_THRESHOLD_RATIO)


# 两套模式配置（qwen-flash：max 1M，压缩触发 700k）
# 用户方案：work 完整保留 5 轮、chat 完整保留 15 轮；chat 超过 CHAT_MAX_ROUNDS 触发压缩
MODE_CONFIGS = {
    CHAT_MODE: ModeAgentConfig(
        mode=CHAT_MODE,
        keep_complete_turns=15,       # 闲聊完整保留 15 轮
        max_sub_turns=10,             # 闲聊 sub_turn 上限 10
        drop_old_tool_results=False,  # 闲聊完整保留工具结果
        context_max_tokens=QWEN_CONTEXT_TOKENS,
        compaction_threshold=_threshold(QWEN_CONTEXT_TOKENS),
        chat_max_rounds=CHAT_MAX_ROUNDS,  # 15 轮触发压缩
    ),
    WORK_MODE: ModeAgentConfig(
        mode=WORK_MODE,
        keep_complete_turns=5,        # 工作完整保留 5 轮
        max_sub_turns=30,             # 工作 sub_turn 上限 30
        drop_old_tool_results=True,   # 工作模式：更早轮次的工具结果删掉、用占位符
        context_max_tokens=QWEN_CONTEXT_TOKENS,
        compaction_threshold=_threshold(QWEN_CONTEXT_TOKENS),
        chat_max_rounds=0,            # work 不启用轮数触发
    ),
}


def get_mode_config(mode: str | None) -> ModeAgentConfig:
    """取指定模式的配置；未知/None 回退到闲聊。"""
    return MODE_CONFIGS.get(mode) or MODE_CONFIGS[CHAT_MODE]
