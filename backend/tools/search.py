"""联网搜索工具：Tavily / Bing 可插拔，默认 mock

- SEARCH_PROVIDER=mock（默认）：返回固定示例结果，保证链路可跑（无需 key）
- SEARCH_PROVIDER=tavily：需 SEARCH_API_KEY（https://tavily.com 免费额度）
- SEARCH_PROVIDER=bing：需 SEARCH_API_KEY + SEARCH_BING_ENDPOINT
"""

import os

import httpx

from dotenv import load_dotenv

load_dotenv()

SEARCH_PROVIDER = os.getenv("SEARCH_PROVIDER", "mock")
SEARCH_API_KEY = os.getenv("SEARCH_API_KEY", "")


async def web_search(query: str, max_results: int = 5) -> str:
    """联网搜索并返回结果摘要。

    参数:
        query: 搜索关键词
        max_results: 返回结果条数（默认 5）
    """
    provider = SEARCH_PROVIDER
    if provider == "mock" or not SEARCH_API_KEY:
        return (
            f"【mock 搜索模式】关于「{query}」的搜索结果（示例）：\n"
            f"1. {query} - 相关介绍：这是演示用搜索结果，配置 SEARCH_API_KEY 后返回真实结果\n"
            f"2. {query} 最新动态：示例条目二\n"
            f"3. {query} 百科：示例条目三"
        )

    if provider == "tavily":
        return await _search_tavily(query, max_results)
    if provider == "bing":
        return await _search_bing(query, max_results)
    return f"未知 SEARCH_PROVIDER: {provider}"


async def _search_tavily(query: str, max_results: int) -> str:
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.post(
                "https://api.tavily.com/search",
                json={"api_key": SEARCH_API_KEY, "query": query, "max_results": max_results},
            )
            resp.raise_for_status()
            data = resp.json()
        results = data.get("results", [])
        if not results:
            return f"没有搜到「{query}」的相关结果"
        lines = [f"关于「{query}」的搜索结果："]
        for i, r in enumerate(results[:max_results], 1):
            lines.append(f"{i}. {r.get('title', '')}：{r.get('content', '')[:120]}")
        return "\n".join(lines)
    except Exception as e:
        return f"搜索服务暂时不可用（{e.__class__.__name__}）"


async def _search_bing(query: str, max_results: int) -> str:
    endpoint = os.getenv("SEARCH_BING_ENDPOINT", "https://api.bing.microsoft.com/v7.0/search")
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(
                endpoint,
                params={"q": query, "count": max_results},
                headers={"Ocp-Apim-Subscription-Key": SEARCH_API_KEY},
            )
            resp.raise_for_status()
            data = resp.json()
        pages = data.get("webPages", {}).get("value", [])
        if not pages:
            return f"没有搜到「{query}」的相关结果"
        lines = [f"关于「{query}」的搜索结果："]
        for i, p in enumerate(pages[:max_results], 1):
            lines.append(f"{i}. {p.get('name', '')}：{p.get('snippet', '')[:120]}")
        return "\n".join(lines)
    except Exception as e:
        return f"搜索服务暂时不可用（{e.__class__.__name__}）"
