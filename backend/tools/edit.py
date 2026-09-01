"""按精确 old_text/new_text 块修改工作区内的文本文件。"""

from __future__ import annotations

import json

from approval_policy import REQUIRE_APPROVAL
from tool_recovery import REPLAY_NEVER
from tool_registry import ToolSpec
from tool_runtime import ToolHostEvent, ToolOutput
from tool_scheduler import SEQUENTIAL

from tools._file_utils import (
    MAX_WRITE_CHARS,
    atomic_write_utf8,
    count_line_changes,
    display_workspace_path,
    read_utf8_text,
    resolve_workspace_path,
    workspace_diff,
)


def _read_edit_value(edit: dict, *names: str) -> str | None:
    for name in names:
        value = edit.get(name)
        if value is not None:
            return value
    return None


def edit(path: str, edits: list[dict]) -> ToolOutput:
    if not isinstance(edits, list) or not edits:
        raise ValueError("edits 必须是非空数组")
    file_path = resolve_workspace_path(path)
    if not file_path.is_file():
        raise ValueError(f"文件不存在或不是普通文件：{path}")

    original = read_utf8_text(file_path)
    updated = original
    for index, item in enumerate(edits, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index} 个 edit 必须是对象")
        old_text = _read_edit_value(item, "old_text", "oldText")
        new_text = _read_edit_value(item, "new_text", "newText")
        if not isinstance(old_text, str) or not old_text:
            raise ValueError(f"第 {index} 个 edit 缺少非空 old_text")
        if not isinstance(new_text, str):
            raise ValueError(f"第 {index} 个 edit 缺少 new_text")
        occurrences = updated.count(old_text)
        if occurrences == 0:
            raise ValueError(f"第 {index} 个 old_text 在文件中不存在")
        if occurrences > 1:
            raise ValueError(f"第 {index} 个 old_text 匹配到 {occurrences} 处，拒绝不确定修改")
        updated = updated.replace(old_text, new_text, 1)

    if len(updated) > MAX_WRITE_CHARS:
        raise ValueError(f"修改后的文件不能超过 {MAX_WRITE_CHARS} 个字符")
    atomic_write_utf8(file_path, updated)
    added_lines, removed_lines = count_line_changes(original, updated)
    diff, diff_truncated = workspace_diff(
        original,
        updated,
        display_workspace_path(file_path),
    )
    result = {
        "path": display_workspace_path(file_path),
        "edits_applied": len(edits),
        "diff": diff,
    }
    return ToolOutput(
        json.dumps(result, ensure_ascii=False),
        host_events=(ToolHostEvent(
            name="workspace_changed",
            payload={
                "path": result["path"],
                "action": "edited",
                "added_lines": added_lines,
                "removed_lines": removed_lines,
                "diff": diff,
                "diff_truncated": diff_truncated,
                "source_tool": "edit",
            },
        ),),
    )


EDIT_TOOL = {
    "type": "function",
    "function": {
        "name": "edit",
        "description": "对工作区内的文件做精确文本替换；每个 old_text 必须在文件中唯一匹配，命中多处将拒绝执行。需要审批。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "工作区相对路径或绝对路径"},
                "edits": {
                    "type": "array",
                    "minItems": 1,
                    "description": "要执行的一组精确文本替换；每个元素用 old_text（现有原文）+ new_text（新文本）描述一处替换，old_text 必须在文件中唯一匹配",
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_text": {"type": "string", "description": "要替换的现有原文"},
                            "new_text": {"type": "string", "description": "替换后的新文本"},
                        },
                        "required": ["old_text", "new_text"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["path", "edits"],
            "additionalProperties": False,
        },
    },
}


TOOL_SPEC = ToolSpec(
    name="edit",
    definition=EDIT_TOOL,
    implementation=edit,
    execution_mode=SEQUENTIAL,
    approval_mode=REQUIRE_APPROVAL,
    replay_policy=REPLAY_NEVER,
)
