# -*- coding: utf-8 -*-
"""记忆分层存储：L1 事件 / L2 语义事实 / L3 自传关系

三层各存独立 JSONL 到 backend/memories/，线程安全（语音后端并发 sub_turn/会话）。

- L1 events.jsonl  : 事件/情景记忆（一次会话结束时抽一次，短存活）
- L2 facts.jsonl   : 语义/事实记忆（跨会话稳定，由 L1 沉淀，原子条目）
- L3 self.md       : 自传/关系记忆（整体画像，由 L1+L2 周期重写，可重建产物）

层间流动（memory_extractor/本模块触发方法）：
    L0 会话 →(会话结束抽取)→ L1
    L1 →(consolidate_l1_to_l2, 去重合并/淘汰)→ L2
    L1+L2 →(rebuild_l3)→ L3
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid

from agent_config import DEFAULT_MEMORY_CONFIG

# ── 目录与文件名 ─────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MEMORIES_DIR = os.path.join(BASE_DIR, "memories")

L1_FILE = "events.jsonl"
L2_FILE = "facts.jsonl"
L3_FILE = "self.md"

# 层名常量（对外/工具/接口统一）
LAYER_L1 = "l1"
LAYER_L2 = "l2"
LAYER_L3 = "l3"
LAYERS = (LAYER_L1, LAYER_L2, LAYER_L3)

# 语义类别（工具入参/抽取器用）
CATEGORY_IDENTITY = "identity"      # 用户身份
CATEGORY_PREFERENCE = "preference"  # 偏好/习惯
CATEGORY_FACT = "fact"              # 普通稳定事实
CATEGORY_EVENT = "event"            # 事件
CATEGORY_GOAL = "goal"              # 长期目标
CATEGORIES = (CATEGORY_IDENTITY, CATEGORY_PREFERENCE, CATEGORY_FACT, CATEGORY_EVENT, CATEGORY_GOAL)

# category → 默认落层（工具主动写入时，层由类别规则映射，避免 LLM 乱填层）
CATEGORY_TO_LAYER = {
    CATEGORY_IDENTITY: LAYER_L2,
    CATEGORY_PREFERENCE: LAYER_L2,
    CATEGORY_FACT: LAYER_L2,
    CATEGORY_EVENT: LAYER_L1,
    CATEGORY_GOAL: LAYER_L3,
}


def _now() -> float:
    return time.time()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _estimate_tokens(value) -> int:
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return max(1, (len(s) + 3) // 4)


class _Layer:
    """单层持久化列表（JSONL append + 全量重写淘汰）。"""

    def __init__(self, path: str):
        self._path = path
        self._items: list[dict] = []
        self._load()

    def _load(self):
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            self._items.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except OSError:
            pass

    # 追加：写文件 + 内存
    def append(self, item: dict):
        self._items.append(item)
        self._flush([item])

    def _flush(self, items):
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                for it in items:
                    f.write(json.dumps(it, ensure_ascii=False) + "\n")
        except OSError as e:
            print(f"[memory_store] 追加失败: {e}")

    def _rewrite(self):
        # 全量重写（用于删除/淘汰）
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                for it in self._items:
                    f.write(json.dumps(it, ensure_ascii=False) + "\n")
        except OSError as e:
            print(f"[memory_store] 重写失败: {e}")

    def all(self) -> list[dict]:
        return list(self._items)

    def upsert(self, item: dict):
        """按 id 替换或追加（沉淀/更新用）。"""
        idx = next((i for i, x in enumerate(self._items) if x.get("id") == item.get("id")), None)
        if idx is None:
            self._items.append(item)
        else:
            self._items[idx] = item
        self._rewrite()

    def delete(self, mid: str) -> bool:
        before = len(self._items)
        self._items = [x for x in self._items if x.get("id") != mid]
        if len(self._items) == before:
            return False
        self._rewrite()
        return True

    def clear(self):
        self._items = []
        try:
            if os.path.exists(self._path):
                os.remove(self._path)
        except OSError:
            pass


class MemoryStore:
    """记忆分层存储（进程常驻单例）。

    按 user 分目录：memories_dir 显式传入时用指定目录；
    否则若传 user_id → memories/<user_id>/；都不传 → memories/（默认用户）。
    """

    def __init__(self, memories_dir: str | None = None, config=None, user_id: str | None = None):
        self.config = config or DEFAULT_MEMORY_CONFIG
        if memories_dir:
            self.dir = memories_dir
        elif user_id:
            self.dir = os.path.join(MEMORIES_DIR, user_id)
        else:
            self.dir = MEMORIES_DIR
        self.user_id = user_id
        os.makedirs(self.dir, exist_ok=True)
        self._lock = threading.Lock()
        self.l1 = _Layer(os.path.join(self.dir, L1_FILE))
        self.l2 = _Layer(os.path.join(self.dir, L2_FILE))
        # L3 是文本文件（可重建），单独处理
        self._l3_path = os.path.join(self.dir, L3_FILE)
        self._l3: dict | None = None
        self._load_l3()
        # 会话计数（用于 L1→L2 / L2→L3 节流）
        self._session_count = self._load_counter()

    @classmethod
    def for_user(cls, user_id: str, config=None) -> "MemoryStore":
        """按用户创建/定位记忆存储（目录 memories/<user_id>/）。"""
        return cls(memories_dir=None, config=config, user_id=user_id)

    # ── L3 文本载入/保存 ─────────────────────────────────
    def _load_l3(self):
        if os.path.exists(self._l3_path):
            try:
                with open(self._l3_path, encoding="utf-8") as f:
                    raw = f.read().strip()
                if raw:
                    self._l3 = {
                        "id": "l3",
                        "narrative": raw,
                        "updated_ts": os.path.getmtime(self._l3_path),
                    }
            except OSError:
                pass

    def _save_l3(self):
        if not self._l3:
            return
        try:
            with open(self._l3_path, "w", encoding="utf-8") as f:
                f.write(self._l3.get("narrative", ""))
        except OSError as e:
            print(f"[memory_store] L3 保存失败: {e}")

    def _counter_path(self) -> str:
        return os.path.join(self.dir, ".counter.json")

    def _load_counter(self) -> int:
        try:
            with open(self._counter_path(), encoding="utf-8") as f:
                return int(json.load(f).get("sessions", 0))
        except (OSError, ValueError, json.JSONDecodeError):
            return 0

    def _save_counter(self):
        try:
            with open(self._counter_path(), "w", encoding="utf-8") as f:
                json.dump({"sessions": self._session_count}, f)
        except OSError:
            pass

    # ── 写入 ──────────────────────────────────────────────
    def add_l1(self, text: str, category: str = CATEGORY_EVENT, source: dict | None = None) -> dict:
        with self._lock:
            item = {
                "id": _new_id("e"),
                "layer": LAYER_L1,
                "text": text,
                "category": category,
                "source": source or {},
                "ts": round(_now(), 3),
            }
            self.l1.append(item)
            return item

    def add_l2(self, text: str, category: str = CATEGORY_FACT, source: dict | None = None,
               confirmed: bool = True) -> dict:
        with self._lock:
            item = {
                "id": _new_id("f"),
                "layer": LAYER_L2,
                "text": text,
                "category": category,
                "source": source or {},
                "ts": round(_now(), 3),
                "confirmed": confirmed,
                "ref_count": 1,
            }
            self.l2.append(item)
            return item

    def set_l3(self, narrative: str):
        with self._lock:
            self._l3 = {"id": "l3", "narrative": narrative, "updated_ts": round(_now(), 3)}
            self._save_l3()

    # ── 查询 / 召回 ──────────────────────────────────────
    def list_layer(self, layer: str) -> list:
        with self._lock:
            if layer == LAYER_L1:
                return self.l1.all()
            if layer == LAYER_L2:
                return self.l2.all()
            if layer == LAYER_L3:
                return [dict(self._l3)] if self._l3 else []
            return []

    def l1_entries(self) -> list:
        with self._lock:
            return self.l1.all()

    def l2_entries(self) -> list:
        with self._lock:
            return self.l2.all()

    def l3_narrative(self) -> str | None:
        with self._lock:
            return self._l3.get("narrative") if self._l3 else None

    def recall_blocks(self) -> dict:
        """供 build_model_context 注入的记忆块（按 L3>L2>L1 排序）。

        注意：必须单层锁内直接读内部结构，不能在此锁内再调 l3_narrative()/
        l2.all()/l1.all()（它们各自拿同一把锁，threading.Lock 不可重入会死锁）。
        """
        with self._lock:
            l3 = self._l3.get("narrative") if self._l3 else None
            l2 = list(self.l2._items)
            l1 = list(self.l1._items)
        return {"l3": l3, "l2": l2, "l1": l1}

    def estimate_tokens(self) -> int:
        with self._lock:
            t = 0
            for it in self.l2.all():
                t += _estimate_tokens(it.get("text", ""))
            for it in self.l1.all():
                t += _estimate_tokens(it.get("text", ""))
            if self._l3:
                t += _estimate_tokens(self._l3.get("narrative", ""))
        return t

    # ── 删除 / 清理 ──────────────────────────────────────
    def delete(self, mid: str, layer: str | None = None) -> bool:
        """跨层删（找不到就按 id 在所有 L1/L2 找）。"""
        with self._lock:
            if layer in (None, LAYER_L1) and self.l1.delete(mid):
                return True
            if layer in (None, LAYER_L2) and self.l2.delete(mid):
                return True
            if layer == LAYER_L3:
                self._l3 = None
                try:
                    if os.path.exists(self._l3_path):
                        os.remove(self._l3_path)
                except OSError:
                    pass
                return True
            return False

    def clear(self, layer: str | None = None):
        with self._lock:
            if layer in (None, LAYER_L1):
                self.l1.clear()
            if layer in (None, LAYER_L2):
                self.l2.clear()
            if layer in (None, LAYER_L3):
                self._l3 = None
                try:
                    if os.path.exists(self._l3_path):
                        os.remove(self._l3_path)
                except OSError:
                    pass

    # ── 淘汰（超上限）────────────────────────────────────
    def _evict_l1(self):
        # 淘汰最旧；未保留任何"不被刷掉"逻辑（事件层本身就短存活）
        while len(self.l1._items) > self.config.l1_max_entries:
            self.l1._items.pop(0)
        self.l1._rewrite()

    def _evict_l2(self):
        # 按 确认>引用数 淘汰（confirmed 优先保留，其次高 ref_count；同样则旧）
        items = sorted(
            self.l2._items,
            key=lambda x: (x.get("confirmed", False), x.get("ref_count", 0), x.get("ts", 0)),
            reverse=True,
        )
        keep = items[: self.config.l2_max_entries]
        self.l2._items = keep
        self.l2._rewrite()

    # ── 层间流动（由 extractor/scheduler 调用）──────────────
    def on_session_archived(self) -> bool:
        """一次会话结束归档时调用：计数 + 返回是否需要做 L1→L2 沉淀。"""
        self._session_count += 1
        self._save_counter()
        return self._session_count % self.config.l2_consolidate_every_n_sessions == 0

    def consolidate(self, ext):  # ext: MemoryConsolidator（见 extractor）
        """L1 → L2 沉淀 + 淘汰；返回新写入条数。

        注意：这里不能用"整个函数持锁"——ext.classify_event / add_l2 内部各自
        拿 self._lock（threading.Lock 不可重入），外层再持锁会死锁。
        因此只在短临界区内做原子操作。
        """
        l1 = self.l1.all()          # 短拿锁读
        l2 = self.l2.all()          # 短拿锁读
        n = 0
        for ev in l1:
            status = ext.classify_event(ev, l2)  # 传入 l2，不再重取
            if status == "drop":
                continue
            if status == "new":
                # add_l2 内部自带锁（勿再包 lock，防重入死锁）
                self.add_l2(ev["text"], ev.get("category", "fact"),
                            source=ev.get("source"), confirmed=ev.get("confirmed", True))
                n += 1
            elif status.startswith("merge:"):
                mid = status.split(":", 1)[1]
                with self._lock:
                    for it in self.l2._items:
                        if it.get("id") == mid:
                            it["ref_count"] = it.get("ref_count", 1) + 1
                            it["ts"] = round(_now(), 3)
                            self.l2._rewrite()
                            break
        # 沉淀后清空 L1；淘汰 L2（都含短锁操作）
        with self._lock:
            self.l1.clear()
            self._evict_l2()
        return n

    def rebuild_l3(self, narrative: str):
        self.set_l3(narrative)
        self._evict_l1()
        self._evict_l2()
