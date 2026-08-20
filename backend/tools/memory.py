"""主动记忆工具：memory_add / memory_forget（用户主动要求"记住/忘掉"时由 LLM 调用）

- memory_add(text, category)：把用户要求记住的信息按类别规则落到对应层
  （identity/preference/fact → L2；event → L1；goal → L3 语义归 L2 并标记 goal）。
  同时按 v2 三分工写穿：identity/preference/fact/goal → MEMORY.md（长期主干），
  event → 每日日志 memory/YYYY-MM-DD.md。
- memory_forget(id 或 keyword)：删除匹配的记忆条目（L1/L2；v2 中按关键词从 MEMORY.md 移除）。

运行时依赖 MemoryStore/MemoryExtractor 单例，由 main.py 初始化后 bind_memory() 注入，
避免与 main/agent_runtime 循环依赖。未绑定时工具返回错误：记忆未启用。
"""

from __future__ import annotations

from memory_store import CATEGORIES

# 全局单例（由 bind_memory 注入）
_store = None
_extractor = None
_fs = None  # v2 扁平记忆文件系统（写穿目标）


def bind_memory(store, extractor, memory_fs=None):
    """main.py 初始化记忆模块后调用，注入全局引用（memory_fs 可选，启用 v2 写穿）。"""
    global _store, _extractor, _fs
    _store = store
    _extractor = extractor
    _fs = memory_fs


def _v2_write(text: str, category: str):
    """按三分工把主动记忆写穿到 v2 扁平文件（不阻塞，失败仅告警）。"""
    if _fs is None:
        return
    try:
        if category == "event":
            _fs.append_daily_md(text[:300])
        else:
            _fs.append_memory_md(text[:200])
    except Exception as e:
        print(f"[memory] v2 写穿失败（不阻塞）: {e}")


def memory_add(text: str, category: str = "fact") -> str:
    """记录一条长期记忆。用户主动说"记住/你记住……"时调用。"""
    if not text or not text.strip():
        return "错误：记忆内容为空"
    if _extractor is None or _store is None:
        return "错误：记忆模块未启用"
    if category not in CATEGORIES:
        category = "fact"
    item = _extractor.memory_add(text.strip(), category)
    if item is None:
        return "错误：记忆写入失败"
    _v2_write(text.strip(), category)
    return f"已记住（{category}）：{text.strip()}"


def memory_forget(text: str) -> str:
    """忘记一条记忆。可以按内容关键词或记忆 id 指定。"""
    if not text or not text.strip():
        return "错误：请指定要忘记的记忆"
    q = text.strip()
    if _store is None:
        return "错误：记忆模块未启用"
    # 按 id 精确匹配优先
    for layer in ("l1", "l2"):
        for it in _store.list_layer(layer):
            if it.get("id") == q:
                _store.delete(it["id"], layer)
                return f"已忘记：{it.get('id')}"
    # 按内容关键词匹配（删除所有包含该词的 L1/L2 条目）
    removed = 0
    for layer in ("l1", "l2"):
        for it in list(_store.list_layer(layer)):
            if q in (it.get("text") or ""):
                if _store.delete(it["id"], layer):
                    removed += 1
    if removed:
        return f"已忘记 {removed} 条包含「{q}」的记忆"
    return f"未找到包含「{q}」的记忆"


MEMORY_ADD_TOOL = {
    "type": "function",
    "function": {
        "name": "memory_add",
        "description": (
            "把用户明确要求记住的信息写入长期记忆。当用户说\u201c记住/你记住/以后记得\u201d或"
            "主动告知自己的身份、偏好、习惯、关系、长期目标等信息时应调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要记住的一句话（整理后，简短具体）"},
                "category": {
                    "type": "string",
                    "enum": list(CATEGORIES),
                    "description": "记忆类别：identity 身份 / preference 偏好 / fact 事实 / event 事件 / goal 长期目标",
                },
            },
            "required": ["text", "category"],
            "additionalProperties": False,
        },
    },
}

MEMORY_FORGET_TOOL = {
    "type": "function",
    "function": {
        "name": "memory_forget",
        "description": (
            "删除长期记忆。当用户说\u201c忘掉/不需要记住/删掉\u201d某条记忆时调用。"
            "传入记忆 id 或内容关键词。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "记忆 id 或内容关键词"},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
}
