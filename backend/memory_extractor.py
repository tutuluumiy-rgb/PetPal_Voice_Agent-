# -*- coding: utf-8 -*-
"""记忆抽取器：LLM 从会话/事件生成记忆，并管理层间流动

职责：
- 主动写入：memory_add(text, category) —— 工具调用入口，按类别规则落 L1/L2/L3
- 会话结束抽取：extract_session(...) —— 把一次会话（多 run）抽成 L1 事件候选
- 层间流动：classify_event（L1→L2 去重/合并/丢弃）、build_l3_narrative（L2→L3 聚合）

LLM 调用通过注入的 summarizer / extract_fn（async (prompt)->str）间接完成，
本模块不绑定具体 provider，便于测试（mock 回调）。
"""

from __future__ import annotations

import json

from agent_config import DEFAULT_MEMORY_CONFIG
from memory_store import (
    MemoryStore,
    LAYER_L1,
    LAYER_L2,
    LAYER_L3,
    CATEGORIES,
    CATEGORY_TO_LAYER,
)

# ── 会话 → L1 事件抽取指令 ──────────────────────────────
SESSION_EXTRACT_SYSTEM_PROMPT = """你是对话记忆抽取器。
下面是"一次完整会话"里用户说的话与西西（助手）的交流。请抽出这一会话中
【长期、稳定、关于用户或共同历史、值得未来参考】的信息，作为事件记忆条目。

原则：
- 只抽真正稳定/重要的：用户身份、偏好、习惯、家庭/关系、长期目标、重要事件。
- 不抽一次性任务细节（如"帮用户查了今天的天气"这种临时任务，不要记）。
- 不重复输出用户已经明显表达过的常识。
- 每条记忆用一句话（中文），简短具体。

输出严格为 JSON 数组，每个元素：
{"text": "<一句话>", "category": "identity|preference|fact|event|goal"}
若本次会话没有值得记的，输出空数组 [] 。
不要输出 JSON 以外的任何文字。"""


# ── L2 → L3 聚合指令 ───────────────────────────────────
L3_BUILD_SYSTEM_PROMPT = """你是用户长期记忆的整理者。
下面是已确认的长期事实记忆（L2）。请把它们整合成一段【对用户的整体画像】叙事，
用于长期参考。要求：
- 按主题组织（身份→偏好/习惯→家庭/关系→长期目标→重要经历），用短自然句子。
- 只基于给定事实，不要编造；冲突的以后出现的为准。
- 输出为一段连续中文文本（200~400 字），不要用 JSON、不要用列表符号。
若事实为空，直接输出"（暂无长期记忆）"。"""


# ── L1 条目去重/合并判定指令（可选，extract_fn 实现）───
CONSOLIDATE_SYSTEM_PROMPT = """你在把"事件记忆（L1）"沉淀为"长期事实（L2）"。
对每条事件，参考已有事实列表，判定它该：
- new      ：有价值且未覆盖，追加为事实
- merge:<已有事实id>：与某条已有事实同义/重复，合并（累加引用）
- drop     ：一次性/临时/已过时，丢弃
严格输出一行 JSON 数组，每个元素 {"id":"<事件id>","decision":"new|merge:<id>|drop"}。"""


def _as_json_array(s: str):
    """从 LLM 输出里折取 JSON 数组（容错：去掉首尾多余文字）。"""
    s = s.strip()
    # 找出第一个 [ 到最后一个 ]
    start = s.find("[")
    end = s.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        data = json.loads(s[start:end + 1])
        return data if isinstance(data, list) else []
    except (ValueError, json.JSONDecodeError):
        return []


class MemoryExtractor:
    """记忆抽取与层间流动控制器。

    summarizer: async (messages: list[dict]) -> str，messages 形如
                [{"role":"system",...}, {"role":"user",...}]，调用方负责带对应指令；
                None 时抽取/聚合为空（记忆关闭）。
    extract_fn: 可选覆盖（测试用），否则用 summarizer。
    """

    def __init__(self, store: MemoryStore, summarizer=None, config=None):
        self.store = store
        self.summarizer = summarizer
        self.config = config or DEFAULT_MEMORY_CONFIG
        self.enabled = self.config.enabled and summarizer is not None

    async def _summ(self, system_prompt: str, user_prompt: str) -> str:
        """带 system 指令调 LLM；返回文本。异常时抛给上层。"""
        result = await self.summarizer([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ])
        return result or ""

    # ── 主动写入（工具 memory_add 入口）────────────────── ─
    def memory_add(self, text: str, category: str) -> dict | None:
        """按类别规则落层。category 非法时回退 fact。返回写入条目或 None。"""
        cat = category if category in CATEGORIES else "fact"
        layer = CATEGORY_TO_LAYER.get(cat, LAYER_L2)
        if layer == LAYER_L3:
            # 主动写入长期目标：直接进 L2（L3 是聚合产物），并标记 goal 类
            return self.store.add_l2(text, cat, confirmed=True)
        if layer == LAYER_L1:
            return self.store.add_l1(text, cat)
        return self.store.add_l2(text, cat, confirmed=True)

    # ── 会话结束抽取（L0 会话 → L1）─────────────────────
    async def extract_session(self, transcript_pairs, source: dict | None = None) -> list:
        """transcript_pairs: [(role, text), ...]（一次会话的全部用户/助手文本）。
        返回新写入的 L1 条目。失败/关闭时返回 []（不阻塞调用方）。"""
        if not self.enabled:
            return []
        conv = "\n".join(f"[{r}]: {t}" for r, t in transcript_pairs if t)
        if not conv.strip():
            return []
        user_prompt = f"<conversation>\n{conv}\n</conversation>"
        try:
            raw = await self._summ(SESSION_EXTRACT_SYSTEM_PROMPT, user_prompt)
        except Exception as e:
            print(f"[memory] 会话抽取失败（不阻塞）: {e}")
            return []
        entries = _as_json_array(raw)
        written = []
        for e in entries:
            text = str(e.get("text", "")).strip()
            cat = e.get("category", "event")
            if not text:
                continue
            written.append(self.store.add_l1(text, cat, source=source))
        return written

    # ── L1→L2 判定（consolidate 用）─────────────────────
    def classify_event(self, entry: dict, l2: list | None = None):
        """对单条 L1 事件判定 new/merge:<id>/drop。默认无 LLM 时走规则。

        传入 l2 列表可避免在 consolidate 持"长锁"期间重取（防锁重入死锁）；
        None 时自行读取。
        """
        text = (entry.get("text") or "").strip()
        if not text:
            return "drop"
        if l2 is None:
            l2 = self.store.l2_entries()
        for it in l2:
            t2 = (it.get("text") or "").strip()
            if t2 and (t2 == text or (len(text) > 4 and text in t2)):
                return f"merge:{it['id']}"
        return "new"

    async def classify_all(self, events, l2: list | None = None) -> list:
        """批量判定（可用 LLM）；失败回退到规则。返回 decision 列表。"""
        if not self.enabled or not events:
            return [self.classify_event(e, l2) for e in events]
        facts = [(it.get("id"), it.get("text")) for it in (l2 or self.store.l2_entries())]
        ev_lines = "\n".join(f"{e['id']}: {e.get('text')}" for e in events)
        fact_lines = "\n".join(f"{i}: {t}" for i, t in facts) or "（无）"
        user_prompt = (
            "<events>\n" + ev_lines + "\n</events>\n"
            "<facts>\n" + fact_lines + "\n</facts>\n"
            "按规则逐条输出 JSON 数组。"
        )
        try:
            raw = await self._summ(CONSOLIDATE_SYSTEM_PROMPT, user_prompt)
            dec = _as_json_array(raw)
            by_id = {d.get("id"): d.get("decision") for d in dec if isinstance(d, dict)}
        except Exception:
            by_id = {}
        return [by_id.get(e["id"], self.classify_event(e, l2)) for e in events]

    # ── L2 → L3 聚合 ────────────────────────────────────
    async def build_l3_narrative(self) -> str | None:
        if not self.enabled:
            return None
        facts = self.store.l2_entries()
        if not facts:
            return "（暂无长期记忆）"
        fact_lines = "\n".join("- " + it.get("text", "") for it in facts)
        try:
            raw = await self._summ(L3_BUILD_SYSTEM_PROMPT, "<facts>\n" + fact_lines + "\n</facts>")
            return (raw or "").strip() or "（暂无长期记忆）"
        except Exception as e:
            print(f"[memory] L3 聚合失败: {e}")
            return None

    # ── 会话归档统一入口（由 main/agent_state 触发）──────
    async def on_session_end(self, transcript_pairs, source=None) -> int:
        """会话结束：抽取 L1；按节流判断是否 L1→L2 沉淀 / L2→L3 重写。
        返回本会话新增 L1 条数。"""
        written = await self.extract_session(transcript_pairs, source)
        need_consolidate = self.store.on_session_archived()
        if need_consolidate:
            self.store.consolidate(self)
        # 每 N 次沉淀后重写 L3
        if (self.store._session_count % (
                self.config.l2_consolidate_every_n_sessions * self.config.l3_rebuild_every_n_consolidations)) == 0:
            nar = await self.build_l3_narrative()
            if nar:
                self.store.rebuild_l3(nar)
        return len(written)
