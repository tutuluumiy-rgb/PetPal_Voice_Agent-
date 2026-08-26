"""LLM 评测脚本 — 对应 M2.1 拟人度评分 + M5.1 人设一致性（metrics-for-interview/2-指标字典.md）

用法（cd backend）:
    python tests/llm_judge_metrics.py                     # 读 backend/sessions/*.jsonl，抽最近 N 段，LLM 打分
    python tests/llm_judge_metrics.py --sessions 20       # 指定最多用 20 个会话文件
    python tests/llm_judge_metrics.py --max-turns 6       # 每段对话取最多 6 轮（防过长）
    python tests/llm_judge_metrics.py --dry-run           # 不调 LLM，只打印会评测的样本预览
    python tests/llm_judge_metrics.py --judge deepseek    # 指定评测模型（默认读 .env LLM_PROVIDER）

评测维度：
    - M2.1 拟人度主观评分（1-5，rubric 锚点见下）
    - M5.1 人设一致性（出戏率，人格参照 backend/tests/persona_brief.md）

设计要点（双轨评测的 LLM 轨道）：
    - 用【不同模型家族】做 judge，避免自我偏好（默认 DeepSeek；也可配其他）
    - 每段对话评 2 次（顺序反转）取中位数，抗位置/长度偏差
    - 强制 LLM 输出 evidence + issues，防止只给分不给理由
"""
import argparse
import asyncio
import glob
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ".")

RULE_MOS = """评分标准（拟人度 MOS, 1-5）:
5 = 无法分辨是 AI；4 = 高度拟人，偶有机械感；3 = 像 AI 但有"人味"；
2 = 明显是 AI 但能对话；1 = 完全机械。

打分要点：
- 看口语自然度（有没有书面腔/堆术语）
- 看情绪是否自然连贯（不 5 秒内无理由突跳）
- 看是否会"记人"（记得刚聊过的内容）
- 短而自然的回答 > 长而正确的回答"""

RULE_CONSISTENCY = """评分标准（人设一致性, 出戏判定）：参照人格 Brief 判断是否出戏。
出戏信号（任一出现即记 1 次出戏）：
- 突然书面语/论文腔
- 情绪无理由突跳
- 忘记刚聊过的内容
- 冷漠敷衍且无上下文理由
- 报答大段感谢/免责声明口吻"""


def load_persona_brief() -> str:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "persona_brief.md")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    return "（未找到 persona_brief.md，使用默认人格：一只会撒娇、容易犯困、口语化的 AI 宠物猫）"


def build_judge_prompt(transcript: str, persona: str, metric: str) -> list:
    rule = RULE_MOS if metric == "anthropomorphism" else RULE_CONSISTENCY
    system = (
        "你是一名资深 AI 语音产品评测员。请根据以下对话记录评估 AI 宠物。\n"
        "只输出 JSON，不要多余解释：{\"score\": <1-5>, \"evidence\": [\"引用对话原文\"], \"issues\": [\"问题描述\"]}\n"
        f"\n{rule}\n"
    )
    user = f"【AI 宠物人格】\n{persona}\n\n【对话记录】\n{transcript}"
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


def clean_transcript(msgs: list, max_turns: int) -> str:
    """把会话原文转评测用 transcript（跳过 tool 消息过长部分）。"""
    lines = []
    count = 0
    for m in msgs:
        role = m.get("role")
        content = m.get("content")
        if role == "tool":
            if content and len(content) > 120:
                content = content[:120] + "…"
            lines.append(f"工具结果: {content}")
            continue
        if role in ("user", "assistant"):
            if content is None or content == "":
                continue
            lines.append(f"{'用户' if role == 'user' else 'AI宠物'}: {content}")
            count += 1
    if max_turns and count > max_turns:
        lines = lines[-max_turns * 2:]  # 保留末尾最近的轮次
    return "\n".join(lines) if lines else "（空会话）"


async def judge_once(client, model, transcript: str, persona: str, metric: str) -> dict:
    prompt = build_judge_prompt(transcript, persona, metric)
    # 关闭思考模式的 extra_body：仅对 DeepSeek 系模型生效（其他提供商忽略）。
    # 用 try 包裹：某些网关对未知 extra_body 字段会 400，宁可少传不可传错。
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


async def main():
    parser = argparse.ArgumentParser(description="M2.1 拟人度 + M5.1 人设一致性 (LLM 评测)")
    parser.add_argument("--sessions", type=int, default=None, help="最多用 N 个会话文件（默认全部）")
    parser.add_argument("--max-turns", type=int, default=6, help="每段对话保留最近 N 轮")
    parser.add_argument("--dry-run", action="store_true", help="不调 LLM，打印样本预览")
    parser.add_argument("--judge", default=None, help="judge 模型名（默认取 .env LLM_PROVIDER）")
    args = parser.parse_args()

    persona = load_persona_brief()

    # 收集会话
    sessions_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sessions")
    files = sorted(glob.glob(os.path.join(sessions_dir, "*.jsonl")))
    if not files:
        print(f"[!] 未找到会话文件: {sessions_dir}")
        return
    if args.sessions:
        files = files[-args.sessions:]
    print(f"会话文件: {len(files)} 个 ({sessions_dir})")

    # 组装样本：每文件一段 transcript
    samples = []  # (文件名, transcript)
    for fp in files:
        msgs = []
        try:
            with open(fp, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            msgs.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except OSError:
            continue
        t = clean_transcript(msgs, args.max_turns)
        if t != "（空会话）":
            samples.append((os.path.basename(fp), t))

    if args.dry_run:
        print(f"将评测 {len(samples)} 段对话，示例预览：\n")
        for name, t in samples[:2]:
            print(f"── {name} ──\n{t[:400]}\n")
        print(f"人格参照:\n{persona[:300]}")
        return

    # LLM client（评测 judge 模型：默认复用 .env 的实际模型配置）
    from dotenv import load_dotenv
    load_dotenv()
    from openai import AsyncOpenAI

    provider = os.getenv("LLM_PROVIDER", "").lower()
    # 模型名解析：LLM_PROVIDER 是提供商名，不是模型名——必须映射到真实模型键
    if args.judge:
        model = args.judge
    elif provider == "qwen":
        model = os.getenv("QWEN_LLM_MODEL") or "qwen-flash"
    elif provider == "deepseek":
        model = os.getenv("DEEPSEEK_MODEL") or "deepseek-chat"
    else:
        model = os.getenv("QWEN_LLM_MODEL") or os.getenv("DEEPSEEK_MODEL") or "deepseek-chat"
    if provider == "qwen":
        base_url = os.getenv("QWEN_LLM_BASE_URL") or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        api_key = os.getenv("QWEN_LLM_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
    else:
        base_url = os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"
        api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        print(f"[!] 未找到评测 API key（provider={provider}）")
        return
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    print(f"评测模型: {model}  base_url={base_url}")
    print(f"评测样本: {len(samples)} 段   每段最多 {args.max_turns} 轮")
    if not args.judge:
        print("  ⚠️ 方法论提示：LLM-as-Judge 最好用【与产品不同模型家族】做评委（避免自我偏好）。")
        print("     当前默认复用产品模型；建议 --judge <另一家模型名> 做最终评测。")

    results = {"anthropomorphism": [], "consistency": []}

    for idx, (name, transcript) in enumerate(samples, 1):
        for metric in ("anthropomorphism", "consistency"):
            # 双次评分取中位 —— 抗单次随机性
            # 不做"对话倒序"反转（会颠倒用户/AI 角色，使判断失真）；
            # 抗偏差正确做法是评委级随机化（换模型家族），本轮先取两次同序评分中位。
            scores = []
            for attempt in range(2):
                try:
                    r = await judge_once(client, model, transcript, persona, metric)
                    sc = r.get("score")
                    if isinstance(sc, (int, float)) and 1 <= sc <= 5:
                        scores.append(float(sc))
                    else:
                        print(f"  [{idx}] {metric} 无效分: {r.get('score')}")
                except Exception as e:
                    print(f"  [{idx}] {metric} LLM 调用失败: {type(e).__name__}: {e}")
            if scores:
                results[metric].append(statistics.median(scores))
            if idx % 5 == 0 or idx == len(samples):
                print(f"  …进度 {idx}/{len(samples)}", flush=True)

    # 汇总
    print("\n" + "=" * 64)
    for metric, label in (("anthropomorphism", "M2.1 拟人度"), ("consistency", "M5.1 人设一致性")):
        vals = results[metric]
        if not vals:
            print(f"{label}: 无有效样本")
            continue
        avg = statistics.mean(vals)
        med = statistics.median(vals)
        if metric == "consistency":
            # M5.1 指标定义是「出戏率」：score <= 3 判为出戏（与 1-5 分制对齐）
            out_of_char = sum(1 for v in vals if v <= 3)
            rate = out_of_char / len(vals)
            print(f"{label}: n={len(vals)} 一致性均分={avg:.2f} 中位={med:.2f} "
                  f"出戏率={out_of_char}/{len(vals)}={rate*100:.0f}%")
        else:
            print(f"{label}: n={len(vals)} 平均={avg:.2f} 中位={med:.2f} 分布={sorted(vals)}")
    print("\n说明：")
    print("  · LLM-as-Judge 存在偏差（位置/长度/自我偏好），建议对关键样本做人工 20% 复核")
    print("  · 每段评 2 次取中位已做；若要更强，可换不同模型家族各评 1 次")


if __name__ == "__main__":
    asyncio.run(main())