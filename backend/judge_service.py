"""
judge_service.py — 评测中心 P4 主观双轨的 LLM 初评服务
────────────────────────────────────────────────────────────
设计依据：评测中心 M2（人工+LLM 双轨：LLM 初评 40%，人工审核 60%）
复用 llm_judge_metrics.py 的提示词/rubric 方法论，但输入改为评测中心
case_runs 的 (input_text, reply_text)，输出拟人度 + 人设一致性评分。

边界（不违反"不修改业务代码"）：
  - 独立新模块，不改 main.py 任何函数
  - 通过 telemetry 命令口调用（评测中心 driver → backend）

用法（供 telemetry.py 调用）:
    from judge_service import judge_reply
    result = asyncio.run(judge_reply(user_text, reply_text))
"""

import json
import os
import statistics
from typing import Any

# ── rubric（与 llm_judge_metrics.py 一致，锚点确保跨会话稳定）──

RULE_MOS = """评分标准（拟人度 MOS, 1-5）:
5 = 无法分辨是 AI；4 = 高度拟人，偶有机械感；3 = 像 AI 但有"人味"；
2 = 明显是 AI 但能对话；1 = 完全机械。

打分要点：
- 看口语自然度（有没有书面腔/堆术语）
- 看情绪是否自然连贯（不 5 秒内无理由突跳）
- 看是否会"记人"（记得刚聊过的内容）
- 短而自然的回答 > 长而正确的回答"""

RULE_CONSISTENCY = """评分标准（人设一致性, 出戏判定）：参照人格判断是否出戏。
出戏信号（任一出现即记 1 次出戏/扣分）：
- 突然书面语/论文腔
- 情绪无理由突跳
- 忘记刚聊过的内容
- 冷漠敷衍且无上下文理由
- 报答大段感谢/免责声明口吻"""

DEFAULT_PERSONA = "一只会撒娇、容易犯困、口语化的 AI 宠物猫（名字叫球球/西西），用中文口语回应"


def _load_persona() -> str:
    try:
        base = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(base, "tests", "persona_brief.md")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return f.read().strip()
    except Exception:
        pass
    return DEFAULT_PERSONA


def build_judge_prompt(user_text: str, reply_text: str, persona: str, metric: str) -> list:
    rule = RULE_MOS if metric == "anthropomorphism" else RULE_CONSISTENCY
    system = (
        "你是一名资深 AI 语音产品评测员。下面是一次单轮对话：用户说了一句话，AI 宠物回复了。\n"
        "请评估 AI 宠物的这次回复质量。"
        "只输出 JSON，不要多余解释：{\"score\": <1-5>, \"evidence\": [\"引用对话原文\"], \"issues\": [\"问题描述\"]}\n"
        f"\n{rule}\n"
    )
    user = (
        f"【AI 宠物人格】\n{persona}\n\n"
        f"【用户说的话】\n{user_text}\n\n"
        f"【AI 宠物的回复】\n{reply_text}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def parse_judge_json(text: str) -> dict:
    """从 LLM 输出中提取 JSON（容忍前后多余文本/围栏）"""
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.startswith("json"):
            s = s[4:]
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        start, end = s.find("{"), s.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(s[start:end + 1])
            except json.JSONDecodeError:
                pass
    return {"score": None, "evidence": [], "issues": [f"解析失败: {text[:200]}"]}


def _build_client():
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
    from openai import AsyncOpenAI

    provider = os.getenv("LLM_PROVIDER", "").lower()
    if provider == "deepseek":
        model = os.getenv("DEEPSEEK_MODEL") or "deepseek-chat"
        base_url = os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"
        api_key = os.getenv("DEEPSEEK_API_KEY")
    else:  # 默认 qwen
        model = os.getenv("QWEN_LLM_MODEL") or "qwen-flash"
        base_url = os.getenv("QWEN_LLM_BASE_URL") or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        api_key = os.getenv("QWEN_LLM_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError(f"未找到 LLM API key（provider={provider}）")
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    return client, model, provider


async def _judge_once(client, model, prompt: list) -> dict:
    extra = None
    if "deepseek" in model.lower():
        try:
            extra = {"thinking": {"type": "disabled"}}
        except Exception:
            extra = None
    resp = await client.chat.completions.create(
        model=model,
        messages=prompt,
        temperature=0.2,
        max_tokens=800,
        extra_body=extra,
    )
    text = resp.choices[0].message.content or ""
    return parse_judge_json(text)


async def judge_reply(user_text: str, reply_text: str, attempts: int = 2, prompt_suffix: str | None = None) -> dict:
    """LLM 初评一条 case（拟人度 + 人设一致性），双次取中位。

    参数:
      - prompt_suffix: 评测中心选定的 judge prompt（追加到 system，覆盖默认 rubric）

    返回:
      {
        "anthropomorphism_score": float | None,
        "persona_consistency_score": float | None,
        "evidence": {...}            # 每个维度的 evidence/issues
        "reason": str | None         # 评测理由（供大模型审核结果页展示）
        "error": str | None
      }
    """
    if not reply_text or not reply_text.strip():
        return {"anthropomorphism_score": None, "persona_consistency_score": None,
                "evidence": {}, "reason": None, "error": "empty reply_text"}

    try:
        client, model, provider = _build_client()
    except Exception as e:
        return {"anthropomorphism_score": None, "persona_consistency_score": None,
                "evidence": {}, "reason": None, "error": f"client init failed: {e}"}

    persona = _load_persona()
    result: dict[str, Any] = {"anthropomorphism_score": None, "persona_consistency_score": None,
                              "evidence": {}, "reason": None, "error": None}

    # 评测中心选定的评测 prompt（覆盖默认 rubric）
    extra_system = f"\n【评测中心指定评分指引】\n{prompt_suffix}" if prompt_suffix else ""

    for metric in ("anthropomorphism", "consistency"):
        scores = []
        evidences = []
        issues = []
        for _ in range(attempts):
            try:
                prompt = build_judge_prompt(user_text, reply_text, persona, metric)
                if extra_system:
                    prompt[0] = {"role": "system", "content": prompt[0]["content"] + extra_system}
                r = await _judge_once(client, model, prompt)
                sc = r.get("score")
                if isinstance(sc, (int, float)) and 1 <= sc <= 5:
                    scores.append(float(sc))
                evidences.extend(r.get("evidence", []) or [])
                issues.extend(r.get("issues", []) or [])
            except Exception as e:
                issues.append(f"LLM 调用失败: {type(e).__name__}: {e}")
        if scores:
            median = statistics.median(scores)
            if metric == "anthropomorphism":
                result["anthropomorphism_score"] = round(median, 2)
            else:
                result["persona_consistency_score"] = round(median, 2)
        result["evidence"][metric] = {"evidence": evidences[:6], "issues": issues[:6]}

    # 评测理由：优先取人设维度 issues（出戏点），其次拟人维度，再取 evidence 首条
    reason = None
    cons_issues = result["evidence"].get("consistency", {}).get("issues", []) or []
    anth_issues = result["evidence"].get("anthropomorphism", {}).get("issues", []) or []
    cons_ev = result["evidence"].get("consistency", {}).get("evidence", []) or []
    if cons_issues:
        reason = "；".join(str(x) for x in cons_issues[:3])
    elif anth_issues:
        reason = "；".join(str(x) for x in anth_issues[:3])
    elif cons_ev:
        reason = f"参考回复：「{cons_ev[0]}」"
    result["reason"] = reason

    # 关闭连接（尽量）
    try:
        await client.close()
    except Exception:
        pass
    return result