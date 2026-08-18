"""Harness 薄框架层：工具规格（ToolSpec）

从 DSH Harness 移植的精简版，承载工具的执行方式、审批模式、回放策略等元数据。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ToolSpec:
    """单个工具的规格声明。

    字段与 DSH Harness 对齐，工具文件只需原样提供：
        name              工具名
        definition        OpenAI function calling schema（TOOL 字典）
        implementation    执行函数（返回 ToolOutput）
        execution_mode    SEQUENTIAL / PARALLEL_READONLY
        approval_mode     AUTO_APPROVE / REQUIRE_APPROVAL
        replay_policy     REPLAY_SAFE / REPLAY_NEVER
        worker_visible    是否对 worker 可见（默认 True）
        plan_mode_visible 是否在规划模式可见（默认 False）
    """

    name: str
    definition: dict
    implementation: callable
    execution_mode: str = "SEQUENTIAL"
    approval_mode: str = "REQUIRE_APPROVAL"
    replay_policy: str = "REPLAY_NEVER"
    worker_visible: bool = True
    plan_mode_visible: bool = False
