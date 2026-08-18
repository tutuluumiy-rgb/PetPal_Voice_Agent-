"""工具加载器：声明式工具映射表 + 目录生成 + 调用解析 + 执行 + 模式白名单

两级渐进式披露（progressive disclosure）：
- 第一级：system prompt 注入「可用工具目录」（按当前模式过滤，见 build_catalog_md(mode)）
- 第二级：LLM 输出 TOOL_CALL 声明（JSON 块）→ parse_tool_calls 解析 → execute_tool 执行
  → 结果回填 → 继续。execute_tool 按模式白名单在做调用前校验。

双模式 × 工具权限（语音宠物需求）：
    闲聊模式（CHAT_MODE）：只开放 web_search / read / calculator（搜索、读取、计算）
    工作模式（WORK_MODE）：全部工具开放（含 get_weather、bash、write、edit、ask_user_questions）
    白名单由调用方传入 mode 决定；越权调用直接拒绝，不执行。

新增工具：
    - 简单工具：在 TOOL_DEFINITIONS 直加一条（type/name/description/parameters/executor）
    - Harness 工具（read/bash/write/edit/ask_user_questions）：自带 ToolSpec，
      在 _HARNESS_SPECS 里登记即可（schema/executor/审批声明自动合并）
"""

import json
import re

from .calculator import calculator
from .search import web_search
from .weather import get_weather

# ── 模式常量（与 mode_state 保持一致）────────────────────────
CHAT_MODE = "chat"
WORK_MODE = "work"

# 闲聊模式工具白名单（用户指定：搜索、读取、计算）
CHAT_MODE_TOOLS = {"web_search", "read", "calculator"}
# 工作模式工具白名单（全开）
WORK_MODE_TOOLS = None  # None = 全量


def _tools_for_mode(mode: str) -> set | None:
    """返回某模式允许的工具名集合；None 表示全量放开。"""
    if mode == CHAT_MODE:
        return CHAT_MODE_TOOLS
    return WORK_MODE_TOOLS  # work 或未知 → 全开


# ── 声明式工具映射表 ─────────────────────────────────────────
# 每个工具：OpenAI function calling schema（type/name/description/parameters）+ executor（执行函数）
# executor 可能是同步（返回 str/ToolOutput）或 async（返回 str），execute_tool 统一处理
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

# ── Harness 工具（自带 ToolSpec：schema/executor/审批声明）───
from .read import TOOL_SPEC as _READ_SPEC
from .bash import TOOL_SPEC as _BASH_SPEC
from .write import TOOL_SPEC as _WRITE_SPEC
from .edit import TOOL_SPEC as _EDIT_SPEC
from .ask_user_questions import TOOL_SPEC as _ASK_SPEC

_HARNESS_SPECS = [_READ_SPEC, _BASH_SPEC, _WRITE_SPEC, _EDIT_SPEC, _ASK_SPEC]

for _spec in _HARNESS_SPECS:
    # 工具文件里的 definition（如 READ_TOOL）本身是 {"type":"function","function":{name..}}
    # 取其中的 name 层作为 function 字段，避免外层再包一层导致缺 name
    _fn = _spec.definition
    if isinstance(_fn, dict) and isinstance(_fn.get("function"), dict):
        _fn = _fn["function"]
    TOOL_DEFINITIONS[_spec.name] = {
        "type": "function",
        "function": _fn or _spec.definition,
        "executor": _spec.implementation,
        # 记录审批声明，供 execute_tool 展示/授权（语⾳全自动时仅元数据）
        "approval": getattr(_spec, "approval_mode", "REQUIRE_APPROVAL"),
        "execution_mode": getattr(_spec, "execution_mode", "SEQUENTIAL"),
    }

# 工具调用声明块分隔符（LLM 按此输出，解析按此切分）
CALL_BEGIN = "<<<TOOL_CALL>>>"
CALL_END = "<<<END_TOOL_CALL>>>"

_CALL_RE = re.compile(
    re.escape(CALL_BEGIN) + r"\s*(.*?)\s*" + re.escape(CALL_END), re.S
)


# ── 加载器 API ────────────────────────────────────────────
def get_tool_names(mode: str | None = None) -> list:
    """工具名列表；mode 指定时按白名单过滤。"""
    if mode is None:
        return list(TOOL_DEFINITIONS.keys())
    allowed = _tools_for_mode(mode)
    if allowed is None:
        return list(TOOL_DEFINITIONS.keys())
    return [n for n in TOOL_DEFINITIONS if n in allowed]


def get_schema(name: str) -> dict:
    """单个工具的 OpenAI schema（type/name/description/parameters）"""
    defn = TOOL_DEFINITIONS.get(name)
    return defn["function"] if defn else None


def build_tools_list(mode: str | None = None) -> list:
    """按模式构建传给 LLM 的 API tools 参数（OpenAI function calling 格式）。

    只包含该模式白名单内的工具。这是原生 function calling 的 tools 列表来源。
    """
    tools = []
    for name in get_tool_names(mode):
        defn = TOOL_DEFINITIONS.get(name)
        if defn and defn.get("function"):
            tools.append({"type": "function", "function": defn["function"]})
    return tools


def get_execution_mode(name: str) -> str:
    """工具执行并发模式：SEQUENTIAL（副作用/串行）或 PARALLEL_READONLY（只读/并行）。

    Harness 工具自带 execution_mode；基础只读工具默认 PARALLEL_READONLY。
    """
    defn = TOOL_DEFINITIONS.get(name, {})
    mode = defn.get("execution_mode")
    if mode:
        return mode
    return "PARALLEL_READONLY"



def is_tool_allowed(name: str, mode: str | None) -> bool:
    """判断某工具在当前模式是否允许。mode=None 表示全量放开。"""
    if name not in TOOL_DEFINITIONS:
        return False
    allowed = _tools_for_mode(mode)
    if allowed is None:
        return True
    return name in allowed


def build_catalog_md(mode: str | None = None) -> str:
    """生成注入 system prompt 的「可用工具目录」（按 mode 过滤）

    第一级披露：把这份目录文本注入 prompt，LLM 第一轮就能输出正确的调用字段。
    mode=None 时默认全量（向后兼容无参调用）。
    """
    names = get_tool_names(mode)
    lines = ["## 可用工具", "以下是你可以调用的工具。需要时按【工具调用格式】输出调用声明。"]
    if mode is not None:
        lines.append(f"（当前模式 {('工作' if mode == WORK_MODE else '闲聊')}，仅以下工具可用）")
    for name in names:
        defn = TOOL_DEFINITIONS[name]
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


def _tool_result_to_str(result) -> str:
    """把 executor 的返回值规格化为字符串（兼容 str / ToolOutput / 其他）。"""
    if result is None:
        return "（无返回）"
    if isinstance(result, str):
        return result
    # ToolOutput 及含 .content 的对象
    content = getattr(result, "content", None)
    if content is None:
        content = getattr(result, "transcript_content", None)
    if content is not None:
        return str(content)
    return str(result)


async def execute_tool(name: str, args: dict, mode: str | None = None) -> str:
    """执行工具，返回给 LLM 的文本结果。

    调用前做模式白名单校验（越权直接拒绝，不执行）。
    mode=None 表示全量放开；传入模式则按该模式白名单过滤。
    """
    defn = TOOL_DEFINITIONS.get(name)
    if defn is None:
        return f"错误：未知工具 {name}"
    if not is_tool_allowed(name, mode):
        return (
            f"错误：工具 {name} 在当前{'闲聊' if mode == CHAT_MODE else '工作'}模式下不可用"
            "（权限未开放），请不要调用它。"
        )
    try:
        result = defn["executor"](**args)
        if hasattr(result, "__await__"):
            result = await result
        return _tool_result_to_str(result)
    except TypeError as e:
        return f"错误：工具 {name} 参数不正确（{e}）"
    except Exception as e:
        return f"错误：工具 {name} 执行失败（{e.__class__.__name__}: {e}）"
