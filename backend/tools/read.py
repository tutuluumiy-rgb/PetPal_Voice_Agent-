"""在学习项目工作区内读取 UTF-8 文本文件。"""

from __future__ import annotations

import json

from tool_registry import ToolSpec
from tool_scheduler import PARALLEL_READONLY
from approval_policy import AUTO_APPROVE
from tool_recovery import REPLAY_SAFE
from tool_runtime import ToolOutput

from tools._file_utils import (
    display_workspace_path,
    read_utf8_lines,
    resolve_workspace_path,
    truncate_head,
)


def read(path: str, offset: int | None = None, limit: int | None = None) -> ToolOutput:
    if offset is not None and (not isinstance(offset, int) or isinstance(offset, bool) or offset < 1):
        raise ValueError("offset 必须是从 1 开始的正整数")
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 1):
        raise ValueError("limit 必须是正整数")

    file_path = resolve_workspace_path(path)
    if not file_path.is_file():
        raise ValueError(f"文件不存在或不是普通文件：{path}")
    start = (offset - 1) if offset is not None else 0
    selected_lines, has_more = read_utf8_lines(file_path, start, limit)
    selected_text = "".join(selected_lines)
    preview, truncation = truncate_head(selected_text)
    if has_more:
        next_offset = start + len(selected_lines) + 1
        preview += f"\n\n[文件还有内容，请使用 offset={next_offset} 继续读取]"
    result = {
        "path": display_workspace_path(file_path),
        "offset": offset or 1,
        "content": selected_text,
        "truncation": truncation,
    }
    delivery = {
        "path": display_workspace_path(file_path),
        "offset": offset or 1,
        "content": preview,
        "truncation": truncation,
    }
    return ToolOutput(
        json.dumps(result, ensure_ascii=False),
        json.dumps(delivery, ensure_ascii=False),
    )


READ_TOOL = {
    "type": "function",
    "function": {
        "name": "read",
        "description": "读取工作区内的 UTF-8 文本文件；大文件可用 offset/limit 分段读取。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "工作区相对路径或绝对路径"},
                "offset": {"type": "integer", "description": "起始行号（从 1 开始）"},
                "limit": {"type": "integer", "description": "最大读取行数"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
}


TOOL_SPEC = ToolSpec(
    name="read",
    definition=READ_TOOL,
    implementation=read,
    execution_mode=PARALLEL_READONLY,
    approval_mode=AUTO_APPROVE,
    replay_policy=REPLAY_SAFE,
    worker_visible=True,
    plan_mode_visible=True,
)
