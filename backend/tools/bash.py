"""在学习项目工作区内执行 Bash 命令。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from approval_policy import REQUIRE_APPROVAL
from security import environment_without_secrets
from tool_recovery import REPLAY_NEVER
from tool_registry import ToolSpec
from tool_runtime import ToolOutput
from tool_scheduler import SEQUENTIAL

from tools._file_utils import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_LINES,
    MAX_BASH_OUTPUT_BYTES,
    TOOL_RESULTS_DIR,
    WORKSPACE_ROOT,
    truncate_tail,
)


BASH_EXECUTABLE = shutil.which("bash.exe") or shutil.which("bash")
BASH_TIMEOUT_SECONDS = 20
BASH_MAX_TIMEOUT_SECONDS = 120
BASH_MAX_COMMAND_CHARS = 4000


def _terminate_process(process) -> None:
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def _read_captured_output(path: Path, max_bytes: int) -> tuple[str, int]:
    total_bytes = path.stat().st_size
    with path.open("rb") as file:
        if total_bytes > max_bytes:
            file.seek(total_bytes - max_bytes)
        output = file.read().decode("utf-8", errors="replace")
    return output, total_bytes


def _collect_process_output(process, output_file, overflow_event) -> None:
    stream = process.stdout
    if stream is None:
        return
    captured_bytes = 0
    while True:
        chunk = stream.read(64 * 1024)
        if not chunk:
            break
        remaining = MAX_BASH_OUTPUT_BYTES - captured_bytes
        if remaining > 0:
            output_file.write(chunk[:remaining])
            captured_bytes += min(len(chunk), remaining)
        if len(chunk) > remaining:
            overflow_event.set()
            try:
                process.kill()
            except ProcessLookupError:
                pass
            break
    output_file.flush()


def bash(command: str, timeout: int | float | None = None) -> ToolOutput:
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command 不能为空")
    if len(command) > BASH_MAX_COMMAND_CHARS:
        raise ValueError(f"command 不能超过 {BASH_MAX_COMMAND_CHARS} 个字符")
    if timeout is not None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("timeout 必须是正数")
        if timeout > BASH_MAX_TIMEOUT_SECONDS:
            raise ValueError(f"timeout 不能超过 {BASH_MAX_TIMEOUT_SECONDS} 秒")
    if not BASH_EXECUTABLE:
        raise RuntimeError("未找到 bash；请安装 Git Bash 或启用 WSL")

    timeout_seconds = timeout or BASH_TIMEOUT_SECONDS
    TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    resource_limited = False
    try:
        with tempfile.NamedTemporaryFile(
            mode="w+b",
            delete=False,
            dir=TOOL_RESULTS_DIR,
            prefix="agent-bash-",
            suffix=".log",
        ) as output_file:
            temporary_path = Path(output_file.name)
            process = subprocess.Popen(
                [BASH_EXECUTABLE, "-lc", command],
                shell=False,
                cwd=str(WORKSPACE_ROOT),
                env=environment_without_secrets(os.environ),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            overflow_event = threading.Event()
            collector = threading.Thread(
                target=_collect_process_output,
                args=(process, output_file, overflow_event),
                daemon=True,
            )
            collector.start()
            try:
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired as error:
                _terminate_process(process)
                collector.join(timeout=2)
                raise RuntimeError(
                    f"bash 命令执行超过 {timeout_seconds} 秒"
                ) from error
            collector.join(timeout=2)
            if collector.is_alive():
                _terminate_process(process)
                collector.join(timeout=2)
            resource_limited = overflow_event.is_set()
            return_code = process.returncode
    except RuntimeError:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
        raise
    except OSError as error:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
        raise RuntimeError(f"bash 命令无法启动：{error}") from error

    output, total_bytes = _read_captured_output(
        temporary_path,
        MAX_BASH_OUTPUT_BYTES,
    )
    preview, truncation = truncate_tail(output)
    truncation["total_bytes"] = total_bytes
    truncation["capture_limit_bytes"] = MAX_BASH_OUTPUT_BYTES
    truncation["capture_limited"] = resource_limited
    full_output_path = None
    if resource_limited or truncation["truncated"]:
        full_output_path = str(temporary_path)
    else:
        temporary_path.unlink()
    result_payload = {
        "command": command,
        "exit_code": return_code,
        "output": output,
        "truncation": truncation,
        "full_output_path": full_output_path,
    }
    delivery_payload = {
        "command": command,
        "exit_code": return_code,
        "output": preview,
        "truncation": truncation,
        "full_output_path": full_output_path,
    }
    return ToolOutput(
        json.dumps(result_payload, ensure_ascii=False),
        json.dumps(delivery_payload, ensure_ascii=False),
    )


BASH_TOOL = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": (
            "Execute a Bash command in the learning workspace. "
            f"Output is truncated to the last {DEFAULT_MAX_LINES} lines or {DEFAULT_MAX_BYTES} bytes; "
            f"the process is stopped after {MAX_BASH_OUTPUT_BYTES} captured bytes, "
            "and captured output is saved for later inspection. This action requires approval."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Bash command to execute"},
                "timeout": {"type": "number", "description": "Timeout in seconds, default 20, maximum 120"},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
}


TOOL_SPEC = ToolSpec(
    name="bash",
    definition=BASH_TOOL,
    implementation=bash,
    execution_mode=SEQUENTIAL,
    approval_mode=REQUIRE_APPROVAL,
    replay_policy=REPLAY_NEVER,
)
