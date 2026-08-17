"""工具注册表：全部工具 schema（OpenAI function calling 格式）+ 执行分发

- TOOLS：全部工具列表（方案 A：全量注入，LLM 工具自路由）
- execute_tool(name, args)：按名执行，返回给 LLM 的文本结果
- 按域分组（DOMAIN_*）留口：将来工具 >15 个时做「域路由」只注入本域工具

新增工具：写一个 async 函数 → 加进 _TOOL_DEFS → 注册 execute 分发即可。
"""

from .calculator import calculator
from .search import web_search
from .weather import get_weather

# ── 工具定义（OpenAI function calling schema）──
_TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询城市天气，支持今天/明天/后天。用户问天气时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "城市名，如 北京、上海"},
                    "date": {"type": "string", "description": "可选：今天/明天/后天", "enum": ["今天", "明天", "后天"]},
                },
                "required": ["city"],
            },
        },
    },
    {
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
    },
    {
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
    },
]

# ── 域分组（留口：将来域路由用）──
DOMAIN_WEATHER = ["get_weather"]
DOMAIN_SEARCH = ["web_search"]
DOMAIN_GENERAL = ["calculator"]

# 工具名 → 执行函数映射
_EXECUTORS = {
    "get_weather": get_weather,
    "web_search": web_search,
    "calculator": calculator,
}


def get_tools(domains: list | None = None) -> list:
    """返回工具 schema 列表；domains 为空返回全部（方案 A 默认全量注入）"""
    if not domains:
        return list(_TOOL_DEFS)
    names = set()
    for d in domains:
        names.update(d)
    return [t for t in _TOOL_DEFS if t["function"]["name"] in names]


async def execute_tool(name: str, args: dict) -> str:
    """执行工具，返回给 LLM 的文本结果。未知工具/异常返回错误说明。"""
    fn = _EXECUTORS.get(name)
    if fn is None:
        return f"错误：未知工具 {name}"
    try:
        return await fn(**args)
    except TypeError as e:
        return f"错误：工具 {name} 参数不正确（{e}）"
    except Exception as e:
        return f"错误：工具 {name} 执行失败（{e.__class__.__name__}: {e}）"
