# -*- coding: utf-8 -*-
"""记忆文件系统层（v2，替代 v1 的多 jsonl 分层存储）

用户方案 working_dir（默认 backend/memories/，已 gitignore）：
    MEMORY.md                  # 长期记忆主干（≤1000 字，token 判断）
    memory/YYYY-MM-DD.md       # 每日日志（L1 语义/事实记忆）
    tool_result/<uuid>.txt     # 工具调用完整结果（3 天过期自动清理）
    dialog/YYYY-MM-DD.json     # 每日对话存档（结构化）

职责：文件读写 + token 估算 + tool_result 落盘/过期清理。不负责 LLM 抽取/整合
（那属于 memory_extractor / context 层）。
"""

from __future__ import annotations

import json
import os
import time
import uuid

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_WORKING_DIR = os.path.join(BASE_DIR, "memories")

MEMORY_MD = "MEMORY.md"
DAILY_MD_DIR = "memory"
TOOL_RESULT_DIR = "tool_result"
DIALOG_DIR = "dialog"

# MEMORY.md token 上限（用 token 估算判断，非导航摘要）
MEMORY_MD_MAX_TOKENS = 1000
# tool_result 过期天数
TOOL_RESULT_MAX_DAYS = 3


def _today() -> str:
    import datetime
    return datetime.date.today().isoformat()


def _now_ts() -> float:
    return time.time()


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


class MemoryFs:
    """扁平记忆文件系统层（进程常驻单例，线程安全）。"""

    def __init__(self, working_dir: str | None = None):
        self.dir = working_dir or DEFAULT_WORKING_DIR
        self._mk(self.dir)
        self._mem_md_path = os.path.join(self.dir, MEMORY_MD)
        self._mk(os.path.join(self.dir, DAILY_MD_DIR))
        self._mk(os.path.join(self.dir, TOOL_RESULT_DIR))
        self._mk(os.path.join(self.dir, DIALOG_DIR))
        self._lock = __import__("threading").Lock()

    @staticmethod
    def _mk(p: str):
        os.makedirs(p, exist_ok=True)

    # ── 路径 helpers ──────────────────────────────────
    def daily_md_path(self, date: str | None = None) -> str:
        return os.path.join(self.dir, DAILY_MD_DIR, f"{date or _today()}.md")

    def dialog_json_path(self, date: str | None = None) -> str:
        return os.path.join(self.dir, DIALOG_DIR, f"{date or _today()}.json")

    def tool_path(self, tool_id: str) -> str:
        return os.path.join(self.dir, TOOL_RESULT_DIR, f"{tool_id}.txt")

    # ── MEMORY.md（长期记忆主干，≤1000 token）──────────
    def read_memory_md(self) -> str:
        with self._lock:
            if not os.path.exists(self._mem_md_path):
                return ""
            try:
                with open(self._mem_md_path, encoding="utf-8") as f:
                    return f.read().strip()
            except OSError:
                return ""

    def write_memory_md(self, content: str):
        """整体覆写 MEMORY.md（调用方负责控制 ≤1000 token）。"""
        with self._lock:
            self._mk(self.dir)
            with open(self._mem_md_path, "w", encoding="utf-8") as f:
                f.write(content.strip() + "\n")

    def _read_memory_md(self) -> str:
        """无锁读取 MEMORY.md（供持锁方法内部调用，防锁重入死锁）。"""
        if not os.path.exists(self._mem_md_path):
            return ""
        try:
            with open(self._mem_md_path, encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    def append_memory_md(self, text: str):
        """追加一段；超出上限时保留头部（可后续改为更聪明裁剪）。

        注意：全程单层锁，末尾直接写文件（不能调用持锁的 write_memory_md，
        threading.Lock 不可重入会死锁）。
        """
        with self._lock:
            current = self._read_memory_md()
            section = f"\n- {text.strip()}" if text.strip() else ""
            merged = current + section
            if _estimate_tokens(merged) > MEMORY_MD_MAX_TOKENS:
                budget = MEMORY_MD_MAX_TOKENS
                out = []
                acc = 0
                for ln in (current + "\n").splitlines():
                    t = _estimate_tokens(ln)
                    if acc + t > budget:
                        break
                    out.append(ln)
                    acc += t
                merged = "\n".join(out)
            self._mk(self.dir)
            with open(self._mem_md_path, "w", encoding="utf-8") as f:
                f.write(merged.strip() + "\n")

    def memory_md_tokens(self) -> int:
        return _estimate_tokens(self.read_memory_md())

    # ── memory/YYYY-MM-DD.md（每日日志，L1 语义/事实）──
    def read_daily_md(self, date: str | None = None) -> str:
        p = self.daily_md_path(date)
        if not os.path.exists(p):
            return ""
        try:
            with open(p, encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    def append_daily_md(self, text: str, date: str | None = None):
        with self._lock:
            p = self.daily_md_path(date)
            self._mk(os.path.dirname(p))
            with open(p, "a", encoding="utf-8") as f:
                f.write(f"- {text.strip()}\n")

    # ── dialog/YYYY-MM-DD.json（每日对话存档）──────────
    def read_dialog(self, date: str | None = None) -> dict:
        p = self.dialog_json_path(date)
        if not os.path.exists(p):
            return {"date": date or _today(), "entries": []}
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {"date": date or _today(), "entries": []}

    def upsert_dialog(self, entry: dict, date: str | None = None) -> dict:
        """给当日 dialog 追加/合并一条结构化条目。"""
        with self._lock:
            p = self.dialog_json_path(date)
            self._mk(os.path.dirname(p))
            data = self._read_dialog_raw(date)
            if not isinstance(data.get("entries"), list):
                data["entries"] = []
            key = entry.get("id") or entry.get("round_id")
            if key:
                for i, e in enumerate(data["entries"]):
                    if e.get("id") == key or e.get("round_id") == key:
                        data["entries"][i] = entry
                        break
                else:
                    data["entries"].append(entry)
            else:
                data["entries"].append(entry)
            data["date"] = date or _today()
            with open(p, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return data

    def _read_dialog_raw(self, date: str | None = None) -> dict:
        """无锁读取当日 dialog（供持锁方法内部调用，防锁重入死锁）。"""
        p = self.dialog_json_path(date)
        if not os.path.exists(p):
            return {"date": date or _today(), "entries": []}
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {"date": date or _today(), "entries": []}

    # ── tool_result/<uuid>.txt ─────────────────────────
    def save_tool_result(self, content: str) -> str:
        """保存工具完整结果到 tool_result/<uuid>.txt，返回 tool_id。"""
        with self._lock:
            tool_id = uuid.uuid4().hex[:12]
            with open(self.tool_path(tool_id), "w", encoding="utf-8") as f:
                f.write(content)
            return tool_id

    def read_tool_result(self, tool_id: str) -> str | None:
        p = self.tool_path(tool_id)
        if not os.path.exists(p):
            return None
        try:
            with open(p, encoding="utf-8") as f:
                return f.read()
        except OSError:
            return None

    def cleanup_tool_results(self, max_days: int = TOOL_RESULT_MAX_DAYS) -> int:
        """删除超过 max_days 的 tool_result 文件，返回删除数量。"""
        removed = 0
        with self._lock:
            d = os.path.join(self.dir, TOOL_RESULT_DIR)
            cutoff = _now_ts() - max_days * 86400
            if os.path.isdir(d):
                for name in os.listdir(d):
                    if not name.endswith(".txt"):
                        continue
                    p = os.path.join(d, name)
                    try:
                        if os.path.getmtime(p) < cutoff:
                            os.remove(p)
                            removed += 1
                    except OSError:
                        continue
        return removed

    # ── 推理注入文本（design 2.3：MEMORY.md + 昨日日志，预算内截断）──
    def build_inject_text(self, max_tokens: int = 1800, include_yesterday: bool = True) -> str:
        """组成送模型的记忆注入文本（不含 user_profile，那由用户档案单独注入）。

        幂等：只读，不写文件。组成：
        - MEMORY.md（长期记忆主干）
        - memory/昨日.md（=L1 昨日事件，仅注入昨天）
        预算用 token 估算截断（MEMORY.md 优先，昨日日志次之）。
        """
        import datetime
        acc = 0
        parts: list[tuple[int, str]] = []

        def add(body: str, title: str, weight: int):
            nonlocal acc
            if not body:
                return
            text = f"## {title}\n{body}"
            t = _estimate_tokens(text)
            if acc + t > max_tokens:
                return
            parts.append((weight, text))
            acc += t

        add(self.read_memory_md(), "长期记忆（MEMORY.md）", 1)
        if include_yesterday:
            y = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
            add(self.read_daily_md(y), "昨日事件", 2)
        if not parts:
            return ""
        parts.sort(key=lambda p: p[0])
        return "\n\n".join(text for _, text in parts)
