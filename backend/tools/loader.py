"""工具加载器：声明式工具映射表 + 目录生成 + 调用解析 + 执行

两级渐进式披露（progressive disclosure）：
- 第一级：system prompt 注入「可用工具目录」（每个工具的 name/description/parameters 结构化描述，
  见 build_catalog_md）——LLM 第一轮就能输出正确的调用字段，无需 API tools 参数
- 第二级：LLM 输出 TOOL_CALL 声明（JSON 块）→ parse_tool_calls 解析 → 执行 → 结果回填 → 继续

新增工具：在 TOOL_DEFINITIONS 加一条（type/name/description/parameters/executor 五要素）即可，
目录、解析、执行全部自动生效。
"""

import json
import re

from .calculator import calculator
from .search import web_search
from .weather import get_weather

# ── 声明式工具映射表 ──────────────────────────────────────
# 每个工具：OpenAI function calling schema（type/name/description/parameters）+ executor（执行函数）
TOOL_DEFINITIONS = {
    "get_weather": {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询城市天气，支持今天/明天/后天。用户问天气时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "城市名，如 北京、上海"},
                    "date": {"type": "string", "description": "可选：今天/明天/后天",
                             "enum": ["今天", "明天", "后天"]},
                },
                "required": ["city"],
            },
        },
        "executor": get_weather,
    },
    "web_search": {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "联网搜索最新信息。用户问时事、知识、需要实时数据时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "max_results": {"type": "integer", "description": "结果条数，默认5"},
                },
                "required": ["query"],
            },
        },
        "executor": web_search,
    },
    "calculator": {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": "安全计算数学表达式，如 (3+5)*2、sqrt(144)+10。用户需要算数时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "数学表达式"},
                },
                "required": ["expression"],
            },
        },
        "executor": calculator,
    },
}

# 工具调用声明块分隔符（LLM 按此输出，解析按此切分）
CALL_BEGIN = "<<<TOOL_CALL>>>"
CALL_END = "<<<END_TOOL_CALL>>>"

_CALL_RE = re.compile(
    re.escape(CALL_BEGIN) + r"\s*(.*?)\s*" + re.escape(CALL_END), re.S
)


# ── 加载器 API ────────────────────────────────────────────
def get_tool_names() -> list:
    """全部工具名"""
    return list(TOOL_DEFINITIONS.keys())


def get_schema(name: str) -> dict:
    """单个工具的 OpenAI schema（type/name/description/parameters）"""
    defn = TOOL_DEFINITIONS.get(name)
    return defn["function"] if defn else None


def build_catalog_md() -> str:
    """生成注入 system prompt 的「可用工具目录」（结构化描述：名称/描述/参数）

    第一级披露：把这份目录文本注入 prompt，LLM 第一轮就能输出正确的调用字段。
    """
    lines = ["## 可用工具", "以下是你可以调用的工具。需要时按【工具调用格式】输出调用声明。"]
    for name, defn in TOOL_DEFINITIONS.items():
        f = defn["function"]
        lines.append(f"\n### {name}")
        lines.append(f"- 描述：{f.get('description', '')}")
        params = f.get("parameters", {})
        props = params.get("properties", {})
        required = params.get("required", [])
        if props:
            lines.append("- 参数：")
            for pname, pinfo in props.items():
                ptype = pinfo.get("type", "?")
                pdesc = pinfo.get("description", "")
                tag = "必填" if pname in required else "可选"
                lines.append(f"  - {pname}（{ptype}，{tag}）：{pdesc}")
        elif required:
            lines.append(f"- 必填参数：{'、'.join(required)}")
    return "\n".join(lines)


def parse_tool_calls(text: str) -> list:
    """从 LLM 输出解析工具调用声明，返回 [{"tool": name, "args": {...}}]

    支持块内多行（多个工具并行调用）。
    """
    calls = []
    m = _CALL_RE.search(text)
    if not m:
        return calls
    for line in m.group(1).strip().splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("tool"):
            calls.append({"tool": obj["tool"], "args": obj.get("args") or {}})
    return calls


def strip_tool_call_block(text: str) -> str:
    """移除 LLM 输出中的 TOOL_CALL 声明块，保留纯文本部分（进度播报/回填用）"""
    return _CALL_RE.sub("", text).strip()


async def execute_tool(name: str, args: dict) -> str:
    """执行工具，返回给 LLM 的文本结果。未知工具/异常返回错误说明。"""
    defn = TOOL_DEFINITIONS.get(name)
    if defn is None:
        return f"错误：未知工具 {name}"
    try:
        return await defn["executor"](**args)
    except TypeError as e:
        return f"错误：工具 {name} 参数不正确（{e}）"
    except Exception as e:
        return f"错误：工具 {name} 执行失败（{e.__class__.__name__}: {e}）"
