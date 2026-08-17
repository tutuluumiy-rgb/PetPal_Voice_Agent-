"""天气工具：Open-Meteo 免费 API（无需 key）

两步：
1. geocoding：城市名 → 经纬度（api.open-meteo.com/v1/search）
2. forecast：经纬度 → 温度/天气（api.open-meteo.com/v1/forecast）
返回中文天气摘要（今天/明天/后天的最高最低温度 + 天气代码描述）。
"""

import httpx

GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# 天气代码 → 中文描述（WMO 标准代码简化版）
_WMO = {
    0: "晴", 1: "基本晴朗", 2: "局部多云", 3: "阴天",
    45: "雾", 48: "雾凇",
    51: "毛毛雨", 53: "毛毛雨", 55: "毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    71: "小雪", 73: "中雪", 75: "大雪",
    80: "阵雨", 81: "阵雨", 82: "强阵雨",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "雷阵雨伴强冰雹",
}


def _wmo_desc(code: int) -> str:
    return _WMO.get(code, f"代码{code}")


async def get_weather(city: str, date: str = "") -> str:
    """查询城市天气。

    参数:
        city: 城市名，如 "北京"、"上海"
        date: 可选，今天/明天/后天（不传默认今天）
    """
    try:
        # 1. 城市 → 坐标
        async with httpx.AsyncClient(timeout=8) as client:
            geo_resp = await client.get(GEO_URL, params={"name": city, "count": 1, "language": "zh", "format": "json"})
            geo_resp.raise_for_status()
            geo = geo_resp.json().get("results")
            if not geo:
                return f"没找到城市「{city}」，请确认城市名"

            loc = geo[0]
            lat, lon = loc["latitude"], loc["longitude"]
            name = loc.get("name", city)
            admin = loc.get("admin1", "")

            # 2. 坐标 → 未来 3 天预报
            fc_resp = await client.get(FORECAST_URL, params={
                "latitude": lat,
                "longitude": lon,
                "daily": "weather_code,temperature_2m_max,temperature_2m_min",
                "forecast_days": 3,
                "timezone": "auto",
            })
            fc_resp.raise_for_status()
            daily = fc_resp.json().get("daily", {})

        # 3. 组装摘要
        dates = daily.get("time", [])
        codes = daily.get("weather_code", [])
        tmax = daily.get("temperature_2m_max", [])
        tmin = daily.get("temperature_2m_min", [])

        if not dates:
            return f"{name}（{admin}）天气查询失败：无数据"

        lines = [f"{name}（{admin}）最近三天天气："]
        for i in range(len(dates)):
            day_label = {0: "今天", 1: "明天", 2: "后天"}.get(i, dates[i])
            desc = _wmo_desc(codes[i]) if i < len(codes) else "未知"
            hi = tmax[i] if i < len(tmax) else "?"
            lo = tmin[i] if i < len(tmin) else "?"
            lines.append(f"{day_label}：{desc}，{lo}~{hi}℃")

        # 若指定了日期，聚焦对应天
        if date:
            date_idx = {"今天": 0, "明天": 1, "后天": 2}.get(date.strip(), -1)
            if date_idx == 0:
                return lines[0].replace(f"{name}（{admin}）最近三天天气：", f"{name}（{admin}）今天天气：")
            elif date_idx in (1, 2):
                return f"{name}（{admin}）{date}：{_wmo_desc(codes[date_idx])}，{tmin[date_idx]}~{tmax[date_idx]}℃"

        return "\n".join(lines)
    except httpx.HTTPError as e:
        return f"天气服务暂时不可用（{e.__class__.__name__}），请稍后再试"
    except Exception as e:
        return f"天气查询出错：{e}"
