"""工具包入口：转发工具加载器（loader.py）

工具定义、目录生成、调用解析、执行都在 tools/loader.py（声明式映射表，新增工具加一条即可）。
这里保留兼容导出（get_tools/execute_tool），并导出渐进式披露相关接口。
"""

from .loader import (
    TOOL_DEFINITIONS,
    CALL_BEGIN,
    CALL_END,
    get_tool_names,
    get_schema,
    build_catalog_md,
    parse_tool_calls,
    strip_tool_call_block,
    execute_tool,
)

# ── 兼容旧接口（方案 A 全量注入用；渐进式披露后不再需要全量 schema）──
# 按域分组（留口：将来域路由用）
DOMAIN_WEATHER = ["get_weather"]
DOMAIN_SEARCH = ["web_search"]
DOMAIN_GENERAL = ["calculator"]


def get_tools(domains: list | None = None) -> list:
    """返回工具 schema 列表（OpenAI function calling 格式）。

    渐进式披露后：第一级不再全量注入 API schema（改注入 build_catalog_md 文本目录），
    此函数保留给「第二级按需取 schema」或外部调用使用。
    """
    names = set()
    if domains:
        for d in domains:
            names.update(d)
    elif not domains:
        names = set(get_tool_names())
    return [
        {"type": "function", "function": get_schema(n)}
        for n in names if get_schema(n)
    ]
