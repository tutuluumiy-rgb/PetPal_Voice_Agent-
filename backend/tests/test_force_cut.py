# -*- coding: utf-8 -*-
"""强制切短句逻辑验证：不调网络，直接复用 providers.llm 的真实切句器。

验证点：
1. 短自然句（带逗号、句读齐全）→ 只在句读标点处切，逗号不切，不会被切碎。
2. 超上限且无标点的长句 → 触发强制短切（硬切兜底）。
3. 逗号靠近上限 → 就近自然切点处切（保留自然停顿）。
4. 首句开头逗号太靠前 → 最短片段保护，不切出「你好，」碎片。
"""
import asyncio
import os
import sys

# 确保以 backend 为包根导入 providers.llm
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers.llm import OpenAICompatLLM


def _chunk_seq(text, chunk):
    """把文本切成若干 delta，构造伪流式响应（仅含 choices[0].delta.content）。"""
    class _Delta:
        def __init__(self, content):
            self.content = content
    class _Choice:
        def __init__(self, content):
            self.delta = _Delta(content)
    class _Chunk:
        def __init__(self, content):
            self.choices = [_Choice(content)]
    for i in range(0, len(text), chunk):
        yield _Chunk(text[i:i + chunk])


async def _chunk_seq_async(text, chunk):
    """async 生成器版本，供 _stream_sentences 的 async for 使用。"""
    for c in _chunk_seq(text, chunk):
        yield c


def sim_cut(text, chunk=1):
    """用真实 _stream_sentences 切句，返回句子列表。"""
    llm = object.__new__(OpenAICompatLLM)  # 不触发 __init__（不建客户端）

    async def run():
        out = []
        async for s, _e in llm._stream_sentences(_chunk_seq_async(text, chunk), 0.0):
            out.append(s)
        return out

    return asyncio.run(run())


def show(name, text, chunk=1):
    print("=" * 20, name, f"(chunk={chunk})")
    print("输入:", text, " 长度", len(text))
    for s in sim_cut(text, chunk):
        print("  切出:", repr(s), "长度", len(s))


# 断言：首句长度 ≤ FIRST_SENTENCE_MAX_CHARS
def assert_first_len_small(text, limit, chunk=1):
    first = sim_cut(text, chunk)[0]
    # 长度按中文/ASCII 字符计
    assert len(first) <= limit, f"首句 {len(first)} 字超上限 {limit}: {first!r}"
    return first


if __name__ == "__main__":
    from providers.llm import FIRST_SENTENCE_MAX_CHARS

    # 用例1：短自然句，逗号不切，只在！和。处切
    show("短自然句（逗号不切）", "你好呀，我是年年！明天见。")
    # 用例2：超上限无标点长句 → 强制硬切（12字首句
    show("超上限无标点长句（首句硬切）", "今天天气真的非常好阳光特别灿烂我们出去走走吧散散步")
    # 用例3：逗号靠近上限 → 就近自然切点
    show("逗号靠近上限（就近自然切）", "主人你是不是有点困了我们今天早点休息吧明天还要早起去上班")
    # 用例3b：逗号出现在第9字附近 → 应在逗号处就近切（而非硬切）
    show("逗号足够靠后（就近在逗号切）", "今天天气不错，我们出去散步晒太阳吧顺便买杯奶茶喝")
    # 用例4：首句开头逗号太靠前 → 最短片段保护，不切出「好，」
    show("首句片段保护（前面逗号太早）", "好，这个问题的答案其实非常长而且很复杂我慢慢给你解释清楚")
    # 用例5：整段一次到达（qwen 真实流式行为）→ 强制切【优先于】标点，首句仍 ≤12
    show("整段一次到达（强制切优先）", "我叫年年，是主人最乖的小可爱～\n软乎乎的毛，大眼睛blingbling，\n最爱蹭主人怀里啦！", chunk=999)
    # 用例6：首句带尾标点（！~）→ 吸收尾标点，不产生孤立「！」「~」碎片
    first6 = show("首句尾标点吸收（整段到达）", "我叫年年，是主人的小宝贝！~\n软乎乎的毛，大大的眼睛，\n最爱蹭主人摸头了～", chunk=999)

    # ── 断言汇总 ──
    print("\n" + "=" * 20, "断言", "=" * 20)
    assert_first_len_small("你好呀，我是年年！明天见。", 12)   # 短句不超
    assert_first_len_small("今天天气真的非常好阳光特别灿烂我们出去走走吧散散步", 12)  # 硬切
    assert_first_len_small("主人你是不是有点困了我们今天早点休息吧明天还要早起去上班", 12)
    assert_first_len_small("好，这个问题的答案其实非常长而且很复杂我慢慢给你解释清楚", 12)  # 片段保护
    assert_first_len_small("我叫年年，是主人最乖的小可爱～\n软乎乎的毛，大眼睛blingbling，\n最爱蹭主人怀里啦！", 12, chunk=999)
    first_big = sim_cut("我叫年年，是主人的小宝贝！~\n软乎乎的毛，大大的眼睛，\n最爱蹭主人摸头了～", 999)[0]
    # 尾标点吸收：首句可略超 cap（+尾标点 ≤ cap+2），且应含「！~」、不产生孤立标点碎片
    assert len(first_big) <= FIRST_SENTENCE_MAX_CHARS + 2, f"首句过长: {first_big!r}"
    assert "！" in first_big and "~" in first_big, f"首句应吸收尾标点: {first_big!r}"
    print("全部断言通过 ✅")
