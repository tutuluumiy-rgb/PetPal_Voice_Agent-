"""模型配置（model:get / model:set / model:check / model:list）— 5 个配置组

控制面板「模型配置」页（参照「图像设置」移动端版式）提供 5 组配置，每组：
    API 地址 / API Key / 模型 ID / ↻ 获取可用模型
    大语言模型(LLM) / ASR / TTS / 识图模型(vision) / 视频模型(video)

- model:get   → 返回当前各组的 url / 模型 / 密钥就绪状态（不回传明文 key，只给掩码）
- model:set   → 把用户改动的各组写回 backend/.env（保留注释；缺失项追加）
- model:check → 校验各组必需密钥是否就绪 + best-effort LLM 连通性
- model:list  → 按组返回「可用模型」目录（best-effort 列表，供前端点「获取可用模型」）

映射到 .env（真实管线可读取的键，保存后重启后端生效）：
    LLM  ：LLM_PROVIDER 决定 active 分支（deepseek/qwen）→ DEEPSEEK_MODEL/API_KEY/BASE_URL
          或 QWEN_LLM_MODEL/API_KEY/BASE_URL
    ASR  ：ASR_BASE_URL / ASR_API_KEY / ASR_MODEL（仅支持 *-realtime 流式模型）
    TTS  ：TTS_BASE_URL / DASHSCOPE_API_KEY / TTS_MODEL / TTS_VOICE（仅支持 *-realtime 流式模型）
    vision：VISION_BASE_URL / VISION_API_KEY / VISION_MODEL
    video ：VIDEO_BASE_URL / VIDEO_API_KEY / VIDEO_MODEL
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()  # 读取 backend/.env

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")

SECTION_LABELS = {
    "llm": "大语言模型",
    "asr": "ASR",
    "tts": "TTS",
    "vision": "识图模型",
    "video": "视频模型",
}

SECTION_HINTS = {
    "llm": "",
    "asr": "仅支持 *-realtime 流式识别模型",
    "tts": "仅支持 *-realtime 流式合成模型",
    "vision": "用于普通图片消息的识图服务",
    "video": "视频 Base URL 需以 /v1 结尾，例如 https://api.example.com/v1",
}

# 可用模型目录（best-effort，点「获取可用模型」时返回）
AVAILABLE_MODELS = {
    "llm": [
        {"id": "deepseek-v4-flash", "label": "DeepSeek · deepseek-v4-flash"},
        {"id": "deepseek-chat", "label": "DeepSeek · deepseek-chat"},
        {"id": "qwen-flash", "label": "Qwen · qwen-flash（百炼）"},
        {"id": "qwen-plus", "label": "Qwen · qwen-plus（百炼）"},
        {"id": "qwen-max", "label": "Qwen · qwen-max（百炼）"},
    ],
    "asr": [
        {"id": "qwen3-asr-flash-realtime", "label": "qwen3-asr-flash-realtime（流式）"},
    ],
    "tts": [
        {"id": "qwen3-tts-instruct-flash-realtime", "label": "qwen3-tts-instruct-flash-realtime（流式·指令）"},
        {"id": "qwen3-tts-flash", "label": "qwen3-tts-flash"},
    ],
    "vision": [
        {"id": "glm-4.6v-flash", "label": "GLM-4.6V-Flash（免费）"},
        {"id": "glm-4v-plus", "label": "GLM-4V-Plus"},
    ],
    "video": [],
}


def _mask(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 10:
        return key[:2] + "****"
    return f"{key[:6]}****{key[-4:]}"


def _get_env(k: str, default: str = "") -> str:
    return os.getenv(k, default)


def _active_llm_provider() -> str:
    p = (_get_env("LLM_PROVIDER", "deepseek") or "deepseek").strip().lower()
    return p if p in ("deepseek", "qwen") else "deepseek"


# ── 单组读取 ─────────────────────────────────


def _section_llm() -> dict:
    provider = _active_llm_provider()
    if provider == "deepseek":
        url = _get_env("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        key = _get_env("DEEPSEEK_API_KEY")
        key_env = "DEEPSEEK_API_KEY"
        model = _get_env("DEEPSEEK_MODEL", "deepseek-chat")
        label = "DeepSeek"
    else:
        url = _get_env("QWEN_LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        key = _get_env("QWEN_LLM_API_KEY") or _get_env("DASHSCOPE_API_KEY")
        key_env = "QWEN_LLM_API_KEY" if _get_env("QWEN_LLM_API_KEY") else "DASHSCOPE_API_KEY"
        model = _get_env("QWEN_LLM_MODEL", "qwen-flash")
        label = "Qwen（百炼）"
    return {
        "type": "llm", "label": SECTION_LABELS["llm"], "hint": SECTION_HINTS["llm"],
        "sub": label,
        "url": url, "model": model,
        "api_key_set": bool(key), "api_key_env": key_env, "api_key_masked": _mask(key),
    }


def _section_asr() -> dict:
    key = _get_env("ASR_API_KEY") or _get_env("DASHSCOPE_API_KEY")
    return {
        "type": "asr", "label": SECTION_LABELS["asr"], "hint": SECTION_HINTS["asr"],
        "sub": "阿里云 Qwen3-ASR",
        "url": _get_env("ASR_BASE_URL", "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"),
        "model": _get_env("ASR_MODEL", "qwen3-asr-flash-realtime"),
        "api_key_set": bool(key), "api_key_env": "ASR_API_KEY 或 DASHSCOPE_API_KEY",
        "api_key_masked": _mask(key),
    }


def _section_tts() -> dict:
    key = _get_env("DASHSCOPE_API_KEY")
    return {
        "type": "tts", "label": SECTION_LABELS["tts"], "hint": SECTION_HINTS["tts"],
        "sub": "阿里云 Qwen3-TTS",
        "url": _get_env("TTS_BASE_URL", "https://dashscope.aliyuncs.com"),
        "model": _get_env("TTS_MODEL", "qwen3-tts-instruct-flash-realtime"),
        "voice": _get_env("TTS_VOICE", "Mochi"),
        "api_key_set": bool(key), "api_key_env": "DASHSCOPE_API_KEY",
        "api_key_masked": _mask(key),
    }


def _section_vision() -> dict:
    key = _get_env("VISION_API_KEY")
    return {
        "type": "vision", "label": SECTION_LABELS["vision"], "hint": SECTION_HINTS["vision"],
        "sub": "智谱 GLM 视觉",
        "url": _get_env("VISION_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"),
        "model": _get_env("VISION_MODEL", "glm-4.6v-flash"),
        "api_key_set": bool(key), "api_key_env": "VISION_API_KEY",
        "api_key_masked": _mask(key),
    }


def _section_video() -> dict:
    key = _get_env("VIDEO_API_KEY")
    return {
        "type": "video", "label": SECTION_LABELS["video"], "hint": SECTION_HINTS["video"],
        "sub": "",
        "url": _get_env("VIDEO_BASE_URL", "https://api.example.com/v1"),
        "model": _get_env("VIDEO_MODEL", ""),
        "api_key_set": bool(key), "api_key_env": "VIDEO_API_KEY",
        "api_key_masked": _mask(key),
    }


def get_model_config() -> dict:
    """返回 5 组配置（密钥只给掩码）。"""
    return {
        "llm": _section_llm(),
        "asr": _section_asr(),
        "tts": _section_tts(),
        "vision": _section_vision(),
        "video": _section_video(),
    }


# ── 保存：把各组写回 .env ─────────────────────────────────


def _load_lines() -> list[str]:
    try:
        with open(ENV_PATH, encoding="utf-8") as f:
            return f.readlines()
    except OSError:
        return []


def _update_env(updates: dict[str, str]) -> None:
    """按 KEY=value 更新 .env：保留注释；不存在则追加。值回写忽略空串（表示不变）。"""
    lines = _load_lines()
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped and not line.lstrip().startswith("#"):
            key = stripped.split("=", 1)[0].strip()
            if key in updates and updates[key] != "":
                out.append(f"{key}={updates[key]}\n")
                seen.add(key)
                continue
        out.append(line)
    for k, v in updates.items():
        if v and k not in seen:
            out.append(f"{k}={v}\n")
    try:
        with open(ENV_PATH, "w", encoding="utf-8") as f:
            f.writelines(out)
    except OSError as e:
        print(f"[model_config] 写 .env 失败: {e}")


def save_model_config(payload: dict) -> dict:
    """保存用户提交的各组配置（payload.sections = {llm|asr|tts|vision|video: {url?,api_key?,model?,voice?}}）。"""
    sec = payload.get("sections") if isinstance(payload.get("sections"), dict) else payload
    updates: dict[str, str] = {}

    def u(s: dict, env_key: str, field: str):
        v = str((s or {}).get(field) or "").strip()
        if v:
            updates[env_key] = v

    if isinstance(sec.get("llm"), dict):
        provider = _active_llm_provider()
        s = sec["llm"]
        u(s, "DEEPSEEK_MODEL" if provider == "deepseek" else "QWEN_LLM_MODEL", "model")
        u(s, "DEEPSEEK_API_KEY" if provider == "deepseek" else "QWEN_LLM_API_KEY", "api_key")
        u(s, "DEEPSEEK_BASE_URL" if provider == "deepseek" else "QWEN_LLM_BASE_URL", "url")
    for typ, mapping in (
        ("asr", {"url": "ASR_BASE_URL", "api_key": "ASR_API_KEY", "model": "ASR_MODEL"}),
        ("tts", {"url": "TTS_BASE_URL", "api_key": "DASHSCOPE_API_KEY", "model": "TTS_MODEL", "voice": "TTS_VOICE"}),
        ("vision", {"url": "VISION_BASE_URL", "api_key": "VISION_API_KEY", "model": "VISION_MODEL"}),
        ("video", {"url": "VIDEO_BASE_URL", "api_key": "VIDEO_API_KEY", "model": "VIDEO_MODEL"}),
    ):
        s = sec.get(typ)
        if isinstance(s, dict):
            for field, env_key in mapping.items():
                u(s, env_key, field)

    if updates:
        _update_env(updates)
        print(f"[model_config] 已更新 .env: {list(updates.keys())}")
    return get_model_config()


# ── 检查：各组密钥就绪 + best-effort LLM 连通性 ─────────────────


async def _live_check_llm(provider_url: str, model: str, api_key: str) -> dict:
    if not api_key:
        return {"status": "skipped", "detail": "尚未配置密钥，跳过连通性探测"}
    from openai import AsyncOpenAI
    import time

    client = AsyncOpenAI(api_key=api_key, base_url=provider_url)
    t0 = time.time()
    try:
        await client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": "hi"}], max_tokens=1, timeout=6.0,
        )
        latency = round((time.time() - t0) * 1000)
        return {"status": "ok", "detail": f"连通正常（{latency}ms）", "latency_ms": latency}
    except Exception as e:
        return {"status": "fail", "detail": f"连通失败：{e}", "latency_ms": round((time.time() - t0) * 1000)}


async def check_model_config() -> dict:
    """返回检查结果：5 组密钥就绪状态 + best-effort LLM 连通性。"""
    cfg = get_model_config()
    checks: list[dict] = []
    for typ in ("llm", "asr", "tts", "vision", "video"):
        s = cfg[typ]
        checks.append({
            "key": s.get("api_key_env", ""),
            "label": f"{s['label']} · API Key",
            "status": "ok" if s.get("api_key_set") else "missing",
            "detail": s.get("api_key_masked") or "需要填写，否则该服务会失败",
            "model": s.get("model", ""),
        })
    llm = cfg["llm"]
    live = await _live_check_llm(llm["url"], llm["model"], _get_llm_key(llm["type"]))
    ok = all(c["status"] == "ok" for c in checks)
    return {
        "ok": ok,
        "checks": checks,
        "live": live,
        "required": [c["key"] for c in checks if c["key"]],
    }


def _get_llm_key(_typ: str) -> str:
    provider = _active_llm_provider()
    if provider == "deepseek":
        return _get_env("DEEPSEEK_API_KEY")
    return _get_env("QWEN_LLM_API_KEY") or _get_env("DASHSCOPE_API_KEY")


# ── 获取可用模型目录 ─────────────────────────────────


def list_available_models(category: str | None = None) -> dict:
    c = (category or "").strip().lower()
    if not c:
        return {"category": "", "label": "", "models": []}
    return {
        "category": c,
        "label": SECTION_LABELS.get(c, c),
        "models": AVAILABLE_MODELS.get(c, []),
    }
