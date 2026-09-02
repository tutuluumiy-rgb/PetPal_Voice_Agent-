"""工具文件辅助：安全路径解析 / 读写 / diff / 工作区权限状态

从 DSH Harness 移植的精简版，供 tools/read|write|edit|bash 共用。

工作区权限状态（可手动切换，语音宠物双模式的一部分）：
    WORKSPACE_RESTRICTED = True（默认）→ 所有路径解析被限制在 WORKSPACE_ROOT 内，
        防止语音场景下误读写/误执行到工作区之外。
    set_workspace_restricted(False)    → 放开限制，允许解析任意绝对路径。
切换贯穿运行期，可在模式切换或用户手动时调用。
"""

from __future__ import annotations

import os
import difflib
import tempfile
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()  # 读取 backend/.env（WORKSPACE_ALLOWLIST 等）
except Exception:
    pass

# ── 工作区根：本项目根目录（backend 的上一级）────────────────
_WORKSPACE_ROOT = Path(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
).resolve()
WORKSPACE_ROOT = _WORKSPACE_ROOT

# ── 额外工作区白名单（WORKSPACE_ALLOWLIST，; 分隔多个目录）────
# 默认空 = 行为不变（仅项目工作区）。手动指定如：
#   WORKSPACE_ALLOWLIST=G:\petpal测试
# Worker/工具（read/write/edit/bash 路径解析）在这些目录内同样放行。
_WORKSPACE_ALLOWLIST: list[Path] = []
for _raw in os.getenv("WORKSPACE_ALLOWLIST", "").split(os.pathsep):
    _raw = _raw.strip()
    if _raw:
        try:
            _WORKSPACE_ALLOWLIST.append(Path(_raw).expanduser().resolve())
        except Exception:
            pass


def workspace_allowed_roots() -> list[Path]:
    """返回允许的全部根目录（项目工作区 + 白名单）。"""
    return [WORKSPACE_ROOT, *_WORKSPACE_ALLOWLIST]


def is_path_allowed(candidate: Path) -> bool:
    """candidate 是否落在项目工作区或任一白名单目录内。"""
    for root in workspace_allowed_roots():
        try:
            candidate.relative_to(root)
            return True
        except ValueError:
            continue
    return False


# ── 运行时动态管理（set_workspace 工具调用，无需重启）──────


def _coerce_root(path: str) -> Path:
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path 不能为空")
    return Path(path.strip()).expanduser().resolve()


def add_workspace_root(path: str) -> str:
    """把目录加入白名单并确保目录存在（不存在则创建，避免后续 write/read 报错）。"""
    p = _coerce_root(path)
    try:
        p.mkdir(parents=True, exist_ok=True)  # 目录不存在 → 自动创建
    except OSError as e:
        return f"无法创建工作区目录 {p}：{e}"
    if p not in _WORKSPACE_ALLOWLIST:
        _WORKSPACE_ALLOWLIST.append(p)
    return "已添加工作区：" + str(p) + "。当前白名单：" + "；".join(str(r) for r in workspace_allowed_roots())


def remove_workspace_root(path: str) -> str:
    """把目录移出白名单（不影响项目工作区本身）。返回当前白名单文本。"""
    p = _coerce_root(path)
    _WORKSPACE_ALLOWLIST[:] = [r for r in _WORKSPACE_ALLOWLIST if r != p]
    return "已移除：" + str(p) + "。当前白名单：" + "；".join(str(r) for r in workspace_allowed_roots())

# ── 工作区权限状态（可手动切换）──────────────────────────────
_WORKSPACE_RESTRICTED = True


def is_workspace_restricted() -> bool:
    return _WORKSPACE_RESTRICTED


def set_workspace_restricted(restricted: bool) -> None:
    """切换工作区锁定状态：True=仅限工作区，False=放开到全磁盘。"""
    global _WORKSPACE_RESTRICTED
    _WORKSPACE_RESTRICTED = bool(restricted)


# ── 文件/输出上限常量 ────────────────────────────────────────
DEFAULT_MAX_LINES = 400          # 文本预览默认最多行数
DEFAULT_MAX_BYTES = 30000        # 预览默认最多字节
MAX_BASH_OUTPUT_BYTES = 500000   # bash 输出捕获上限（超过即终止）
MAX_WRITE_CHARS = 1_000_000      # write/edit 单文件最大字符数
TOOL_RESULTS_DIR = WORKSPACE_ROOT / ".tool_results"


# ── 路径解析 ─────────────────────────────────────────────────
def resolve_workspace_path(path: str) -> Path:
    """把用户给的路径解析为绝对路径。

    - 相对路径 → 相对 WORKSPACE_ROOT 解析
    - 绝对路径 → 锁定态下若不在 WORKSPACE_ROOT 内则拒绝；放开态允许
    - 未转义越界（如 ../ 逃逸到工作区外）在锁定态同样拒绝
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path 不能为空")
    p = Path(path).expanduser()
    if not p.is_absolute():
        candidate = (WORKSPACE_ROOT / p).resolve()
    else:
        candidate = p.resolve()

    if _WORKSPACE_RESTRICTED:
        if not is_path_allowed(candidate):
            raise ValueError(
                f"路径超出工作区白名单（工作区锁定中）：{path}"
                f"（允许：{WORKSPACE_ROOT}；额外白名单：{_WORKSPACE_ALLOWLIST}）"
            )
    return candidate


def display_workspace_path(path: Path) -> str:
    """展示用：优先显示相对工作区的相对路径，可读性好。"""
    try:
        return str(path.relative_to(WORKSPACE_ROOT))
    except ValueError:
        return str(path)


# ── 读取 ─────────────────────────────────────────────────────
def read_utf8_lines(path: Path, start: int = 0, limit: int | None = None) -> tuple[list, bool]:
    """按行读取 UTF-8 文本；start 为 0 基行号。

    返回 (selected_lines, has_more)。
    """
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    selected = lines[start:]
    if limit is not None:
        selected = selected[:limit]
        has_more = len(lines) > start + limit
    else:
        has_more = False
    return selected, has_more


def read_utf8_text(path: Path, max_bytes: int | None = None) -> str:
    """读取整个文本文件；可选字节上限（超出截断）。"""
    if max_bytes is not None:
        with open(path, "rb") as f:
            raw = f.read(max_bytes + 1)
        truncated = len(raw) > max_bytes
        text = raw[:max_bytes].decode("utf-8", errors="replace")
        if truncated:
            text += "\n[内容超出预览上限，已截断]"
        return text
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def truncate_head(text: str, max_chars: int = 2000) -> tuple[str, dict]:
    """取文本开头预览；返回 (预览文本, truncation 元数据)。"""
    truncated = len(text) > max_chars
    preview = text[:max_chars]
    if truncated:
        preview += "\n[…]（已截断）"
    return preview, {"truncated": truncated, "capture_limit_chars": max_chars}


def truncate_tail(text: str, max_lines: int = None, max_chars: int = None) -> tuple[str, dict]:
    """取文本末尾预览（bash 用）；返回 (预览, truncation 元数据)。"""
    max_lines = max_lines or DEFAULT_MAX_LINES
    max_chars = max_chars or DEFAULT_MAX_BYTES
    text = text.rstrip("\n")
    lines = text.split("\n")
    head_skipped = len(lines) > max_lines
    if head_skipped:
        lines = lines[-max_lines:]
    joined = "\n".join(lines)
    chars_skipped = len(joined) > max_chars
    if chars_skipped:
        joined = joined[-max_chars:]
    preview = joined
    if head_skipped or chars_skipped:
        preview = "\n…（以上内容截断）\n" + preview
    truncation = {
        "truncated": head_skipped or chars_skipped,
        "head_skipped": head_skipped,
        "chars_skipped": chars_skipped,
        "preview_lines": len(lines),
    }
    return preview, truncation


# ── 写入 / diff ──────────────────────────────────────────────
def atomic_write_utf8(path: Path, content: str) -> None:
    """原子写入 UTF-8（写临时文件后改名，避免写坏）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".tmp-", suffix=".atomic"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def count_line_changes(original: str, updated: str) -> tuple[int, int]:
    """统计 (新增行数, 删除行数)。"""
    a = original.splitlines()
    b = updated.splitlines()
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    added = removed = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "replace":
            removed += i2 - i1
            added += j2 - j1
        elif tag == "delete":
            removed += i2 - i1
        elif tag == "insert":
            added += j2 - j1
    return added, removed


def workspace_diff(original: str, updated: str, label: str = "file", max_chars: int = 4000) -> tuple[str, bool]:
    """生成 unified diff 文本，供 write/edit 的 host_events 载荷。
    返回 (diff 文本, 是否被截断)。"""
    diff = "\n".join(
        difflib.unified_diff(
            original.splitlines(),
            updated.splitlines(),
            fromfile=label,
            tofile=label,
            lineterm="",
        )
    )
    truncated = len(diff) > max_chars
    if truncated:
        diff = diff[:max_chars] + "\n[…diff 截断]"
    return diff, truncated
