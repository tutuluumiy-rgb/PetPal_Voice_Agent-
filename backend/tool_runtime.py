"""Harness 薄框架层：工具输出类型（ToolOutput / ToolHostEvent）

从 DSH Harness 移植的精简版，仅供 backend 的工具（read/bash/write 等）
保持原实现可用所需的最小 API 面。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(eq=False)
class ToolHostEvent:
    """宿主事件：工具副作用（如文件变更）向外部广播的载荷。"""

    name: str
    payload: dict = field(default_factory=dict)


class ToolOutput:
    """工具执行结果。

    兼容三种构造方式（对齐原工具文件的用法）：
        ToolOutput(transcript)                        # 仅正文
        ToolOutput(result_json, delivery_json)        # 正文 + 展示用
        ToolOutput(result_json, delivery_json, host_events=(...))
        ToolOutput(transcript_content="...")          # 仅正文（关键字形式）

    属性：
        content          正文（给模型回填用，__str__ 返回它）
        display_content  展示用内容（delivery，可省略）
        host_events      副作用事件元组（可省略）
    """

    __slots__ = ("content", "display_content", "host_events")

    def __init__(self, *args, transcript_content=None, host_events=None):
        self.content = ""
        self.display_content = None
        self.host_events = host_events
        if transcript_content is not None:
            self.content = str(transcript_content)
        else:
            n = len(args)
            if n >= 1:
                self.content = args[0] if args[0] is not None else ""
            if n >= 2:
                self.display_content = args[1]
            # n >= 3（位置给定 host_events）在旧签名里不出现，忽略

    def __str__(self) -> str:
        return str(self.content)

    # 兼容期望 .text / .transcript_content 的读取方
    @property
    def transcript_content(self) -> str:
        return str(self.content)
