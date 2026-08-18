"""在学习项目工作区内创建或覆盖 UTF-8 文本文件。"""

from __future__ import annotations

import json

from approval_policy import REQUIRE_APPROVAL
from tool_recovery import REPLAY_NEVER
from tool_registry import ToolSpec
from tool_scheduler import SEQUENTIAL
from tool_runtime import ToolHostEvent, ToolOutput

from tools._file_utils import (
    MAX_WRITE_CHARS,
    atomic_write_utf8,
    count_line_changes,
    display_workspace_path,
    read_utf8_text,
    resolve_workspace_path,
    workspace_diff,
)


def write(path: str, content: str) -> ToolOutput:
    if not isinstance(content, str):
        raise ValueError("content 必须是字符串")
    if len(content) > MAX_WRITE_CHARS:
        raise ValueError(f"content 不能超过 {MAX_WRITE_CHARS} 个字符")
    file_path = resolve_workspace_path(path)
    existed = file_path.exists()
    if existed and not file_path.is_file():
        raise ValueError(f"目标路径不是普通文件：{path}")
    original = read_utf8_text(file_path, max_bytes=None) if existed else ""
    atomic_write_utf8(file_path, content)
    added_lines, removed_lines = count_line_changes(original, content)
    diff, diff_truncated = workspace_diff(
        original,
        content,
        display_workspace_path(file_path),
    )
    result = {
        "path": display_workspace_path(file_path),
        "action": "written" if existed else "created",
        "characters": len(content),
        "bytes": len(content.encode("utf-8")),
        "added_lines": added_lines,
        "removed_lines": removed_lines,
    }
    return ToolOutput(
        json.dumps(result, ensure_ascii=False),
        host_events=(ToolHostEvent(
            name="workspace_changed",
            payload={
                "path": result["path"],
                "action": result["action"],
                "added_lines": added_lines,
                "removed_lines": removed_lines,
                "diff": diff,
                "diff_truncated": diff_truncated,
                "source_tool": "write",
            },
        ),),
    )


WRITE_TOOL = {
    "type": "function",
    "function": {
        "name": "write",
        "description": (
            "Write UTF-8 text to a file in the learning workspace. "
            "Creates parent directories and overwrites an existing file."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative or absolute file path"},
                "content": {"type": "string", "description": "Complete UTF-8 file content"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
}


TOOL_SPEC = ToolSpec(
    name="write",
    definition=WRITE_TOOL,
    implementation=write,
    execution_mode=SEQUENTIAL,
    approval_mode=REQUIRE_APPROVAL,
    replay_policy=REPLAY_NEVER,
)
