"""LLM Provider：DeepSeek（迁自 llm_engine.py）+ 工具自路由 Agent 循环

方案 A 核心：不引入显式"chat/task 模式"、不引入意图识别模型——
LLM 通过 function calling 自己决定调不调工具、调几个、调几轮：
- 简单问题（"你好呀"）：不调工具，直接流式回答
- 轻任务（"查北京天气"）：调 1 个工具后回答
- 复杂任务（"查两个城市天气再算温差"）：多轮工具调用后总结

API 密钥从环境变量 DEEPSEEK_API_KEY 读取（在 .env 文件里配置）。
情绪标签约定：[开心] [委屈] [困] [好奇] [兴奋] [平静] [难过] [害怕]

agent_chat 的 yield 格式（两种元组，main.py 据此区分）：
    ("progress", 文本)          # 工具执行前的进度播报（TTS，不进回复显示）
    ("reply", 句子, 情绪标签)    # 最终回复流式逐句
"""

import json
import os
import re
import time

from dotenv import load_dotenv
from openai import AsyncOpenAI

from prompt_loader import build_system_prompt
from agent_loop import run_tool_loop
from .base import LLMProvider

load_dotenv()  # 读取 backend/.env

# 情绪标签正则（与 voice_style 一致）
_EMOTION_RE = re.compile(r"\[(开心|委屈|困|好奇|兴奋|平静|难过|害怕)\]")

# 句子切分标点：遇到这些就认为一句结束
# 加了顿号"、"：口语里顿号是短停顿，切句后 TTS 先播第一段，首句更快（优化首句延迟）
SENTENCE_ENDS = "。！？!?；;…、\n"

# ── 强制切短句（优化首句延迟）──────────────────────────────
# 现状：只按 SENTENCE_ENDS 标点切句，LLM 若写长句（逗号多、句读少）首句会拖很久才开口。
# 这里加「字符上限兜底」，超过上限还没遇到切分标点就强制切，保证首句及时落地。
# 设计（兼顾流畅度——用户反馈：首句切太短如「你好，」反而影响 TTS 段间衔接）：
#   · 软阈值：只有超过上限才强制切；短于上限的自然短句不会被切碎
#     （因此 4~6 字的「你好呀，」会继续攒到标点，不会单独成句）
#   · 就近自然切点：强制切时优先在【最近的逗号/顿号/分号/空格】处切
#     （保留自然停顿，尽量避免切断语义词组）
#   · 最短片段保护：若最近自然切点靠得太前（切出的前半段 < MIN_FRAGMENT，
#     会得到过短碎片），就放弃该切点，改在上限处硬切（取满一上限的完整长度）
# 首句用更小阈值（更快开口），后续句子用正常阈值。
FIRST_SENTENCE_MAX_CHARS = 12     # 首句字符上限（达到即强制切短句）
NORMAL_SENTENCE_MAX_CHARS = 20    # 后续句子字符上限
FIRST_MIN_FRAGMENT = 6            # 首句：自然切点至少这么靠后才采用（防「你好，」碎片）
NORMAL_MIN_FRAGMENT = 8           # 后续：同上
# 强制切时优先选择的自然切点字符（逗号/顿号/分号/空格）
SOFT_CUT_CHARS = "，、；;，,、 \u3000"
_SOFT_CUT_SET = set(SOFT_CUT_CHARS)

# 硬切（cap 处强制切）后，紧随的句读/语气标点要并入前句，避免切出孤立的「！」「~」单字碎片句
_TRAILING_PUNCT = set("。！？!?；;…、～~，,\n")

# 剥离正文中残留的情绪标签（兜底）：完整 [开心] 或残缺 委屈] / [难过
# 关键：必须【至少带一个括号】才算标签——防止误删正文里的裸词（如"你委屈了"）
_EMOTION_STRIP = re.compile(
    r"\[(开心|委屈|困|好奇|兴奋|平静|难过|害怕)\]"     # 完整 [开心]
    r"|\[(开心|委屈|困|好奇|兴奋|平静|难过|害怕)"        # 残缺 [难过（无右括号）
    r"|(开心|委屈|困|好奇|兴奋|平静|难过|害怕)\]"        # 残缺 委屈]（无左括号）
)


def strip_emotion_tags(text: str) -> str:
    """剥离文本中的情绪标签（含残缺形式），保留正文。进度播报与句子兜底共用。"""
    return _EMOTION_STRIP.sub("", text).strip()


def _find_soft_cut(buffer: str, cap: int, min_frag: int) -> int:
    """在 buffer 强制切分时寻找切点。

    - 在 buffer 前 cap 个字符内，从前往后的【最后一个】自然切点（逗号/顿号/分号/空格）
      即为「最近的一个」；返回它的下标。
    - 若这个切点太靠前（< min_frag，会切出过短碎片）返回 -1，表示没有合适自然切点。
    """
    seg = buffer[:cap]
    for i in range(len(seg) - 1, -1, -1):  # 从末尾往前找，第一个命中就是“最近的”
        if seg[i] in _SOFT_CUT_SET:
            return i if i >= min_frag else -1
    return -1



class OpenAICompatLLM(LLMProvider):
    """OpenAI 兼容接口的 LLM 基类（DeepSeek / Qwen 等走 /chat/completions 的模型）

    与具体厂商解耦：api_key / base_url / model / extra_body 全部由子类配置，
    换模型 = 新增子类 + 工厂注册 + .env 切换 LLM_PROVIDER，管道代码不动。
    """

    def __init__(self, api_key: str, base_url: str, model: str, extra_body: dict | None = None):
        if not api_key:
            raise RuntimeError(f"未配置 LLM API Key，请在 backend/.env 里填写（{self.__class__.__name__}）")
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.extra_body = extra_body  # 厂商特殊参数（如 DeepSeek 关闭思考）
        # 工具自路由最大循环轮数（配置于 .env，防死循环）
        self.max_loops = int(os.getenv("MAX_AGENT_LOOPS", "6"))
        self.first_token_time = None  # LLM 首字时间（暴露给 main.py 统计）
        self.total_time = None

    async def chat(self, user_text: str, history: list) -> tuple[str, str]:
        """返回 (回复文本, 情绪标签) —— 兼容旧接口，内部用 chat_stream 拼接"""
        text = ""
        emotion = "平静"
        async for sentence, emo in self.chat_stream(user_text, history):
            if text == "":
                emotion = emo
            text += sentence
        return text, emotion

    # ──────────────────────────────────────────────
    # 无工具流式生成（兼容旧逻辑）
    # ──────────────────────────────────────────────
    async def chat_stream(self, user_text: str, history: list):
        """流式生成，逐句 yield (句子文本, 情绪标签)"""
        import time

        from mode_state import get_mode_state

        mode = get_mode_state().get_mode()
        system_prompt = build_system_prompt(mode)  # 人格 + 语气 + 当前模式工具目录 + 用户档案
        messages = [{"role": "system", "content": system_prompt}]
        for msg in history[-20:]:
            messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": user_text})

        t_start = time.time()
        stream = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0.9,
            max_tokens=15000,
            stream=True,
            extra_body=self.extra_body,
        )

        async for sentence, emo in self._stream_sentences(stream, t_start):
            yield sentence, emo

    # ──────────────────────────────────────────────
    # 工具自路由 Agent 循环（方案 A 核心）
    # ──────────────────────────────────────────────
    async def agent_chat(self, user_text: str, history: list, on_progress=None, is_cancelled=None,
                         extra_context: str | None = None):
        """工具自路由生成。

        参数:
            user_text: 用户输入
            history: 对话历史（短期记忆）
            on_progress: async 回调，工具执行前收到进度文本（用于 TTS 播报）
            is_cancelled: callable，返回 True 时（打断）提前退出循环
            extra_context: 可选系统上下文（如模式切换状态），以 system 消息注入 system_prompt 之后

        yield:
            ("progress", 文本)          # 进度播报
            ("reply", 句子, 情绪标签)    # 最终回复流式逐句
        """
        import time

        from mode_state import get_mode_state

        mode = get_mode_state().get_mode()
        system_prompt = build_system_prompt(mode)  # 人格+语气+当前模式工具指南+目录+用户档案（prompt_loader 组装）
        messages = [{"role": "system", "content": system_prompt}]
        if extra_context:
            # 注入系统级上下文（如"已切换模式"状态），让 LLM 知道而不重复播报
            messages.append({"role": "system", "content": extra_context})
        for msg in history[-20:]:
            messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": user_text})

        # ── 工具循环：两级渐进式（工具目录已在 system prompt，LLM 文本输出 TOOL_CALL 声明）──
        # mode 透传：execute_tool 按模式白名单校验工具权限
        messages, _ = await run_tool_loop(
            self.client,
            self.model,
            messages,
            max_loops=self.max_loops,
            on_progress=on_progress,
            is_cancelled=is_cancelled,
            extra_body=self.extra_body,
            mode=mode,
        )

        # ── 最终轮：流式逐句（复用切句 + 情绪解析）──
        # 首字计时起点 = 发起最终轮请求的时刻（而非流开始后），
        # 这样 first_token_time 才能反映「请求发出 → 服务端返回首个 token」的真实延迟
        # （否则流一开首个 delta 几乎立 到，看板 LLM首字 恒 ≈0）。
        t_req = time.time()
        stream = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0.9,
            max_tokens=15000,
            stream=True,
            extra_body=self.extra_body,
        )
        async for sentence, emo in self._stream_sentences(stream, t_req):
            if is_cancelled and is_cancelled():
                print("[Agent] 最终轮检测到打断，停止输出")
                return
            yield "reply", sentence, emo

    # ──────────────────────────────────────────────
    # 内部：流式增量切句 + 情绪标签解析（chat_stream 与 agent_chat 共用）
    # ──────────────────────────────────────────────
    async def _stream_sentences(self, stream, t_start: float):
        """从 OpenAI 流式响应中切句，yield (句子, 情绪标签)

        切句策略：
        - 正常切分：遇到 SENTENCE_ENDS 标点（。！？;…、\n）就切
        - 强制短切（兜底）：超过字符上限还没遇标点就强制切，首句更小上限更快开口
          （详见 _find_soft_cut：优先最近自然切点，且保护最短片段，防「你好，」碎片）
        """
        buffer = ""
        emotion = "平静"
        emotion_parsed = False
        first_token_time = None
        first_done = False  # 首句是否已切出（决定用首句上限还是正常上限）

        async for chunk in stream:
            delta = chunk.choices[0].delta
            if not delta or not delta.content:
                continue
            if first_token_time is None:
                first_token_time = time.time() - t_start
                self.first_token_time = round(first_token_time, 2)

            buffer += delta.content

            # 第一段：解析情绪标签（在开头）
            if not emotion_parsed:
                m = _EMOTION_RE.search(buffer)
                if m:
                    emotion = m.group(1)
                    buffer = buffer.replace(m.group(0), "", 1)
                    emotion_parsed = True
                    if buffer.strip() and buffer.strip()[-1] in SENTENCE_ENDS:
                        sentence = strip_emotion_tags(buffer)  # 兜底剥离残留/残缺标签
                        buffer = ""
                        if sentence:
                            first_done = True
                            yield sentence, emotion
                        continue

            # 按标点切句 / 超上限强制短切
            # 关键：强制短切【优先于】标点切。只要累积超上限就强制切，
            # 避免「delta 一次吐出整句且带 \n/！等标点」时，标点把首句/句子切得过长、
            # 绕过了强制切（本轮实测：首句 15 字、续句 20 字超上限未被切，根因在此）。
            while True:
                cap = NORMAL_SENTENCE_MAX_CHARS if first_done else FIRST_SENTENCE_MAX_CHARS
                min_frag = NORMAL_MIN_FRAGMENT if first_done else FIRST_MIN_FRAGMENT

                if len(buffer) >= cap:
                    # 1) 已超上限 → 强制短切（优先就近自然切点，无合适切点则在上限处硬切）
                    pos = _find_soft_cut(buffer, cap, min_frag)
                    if pos >= 0:
                        cut = pos
                    else:
                        cut = cap - 1
                        # 修复孤立标点碎片：硬切时把紧随切点的句读/语气标点并入前句，
                        # 避免切出「！」「~」这类单字碎片句（如「...小宝贝」+ 孤立「！~」）
                        j = cut + 1
                        while j < len(buffer) and buffer[j] in _TRAILING_PUNCT:
                            j += 1
                        if j - 1 > cut:
                            cut = j - 1
                else:
                    # 2) 未超上限 → 按真正的切分标点正常切（短句自然成句，不切碎）
                    cut = -1
                    for i, ch in enumerate(buffer):
                        if ch in SENTENCE_ENDS:
                            cut = i
                            break
                if cut == -1:
                    break
                sentence = buffer[: cut + 1].strip()
                buffer = buffer[cut + 1 :]
                if sentence:
                    first_done = True
                    yield strip_emotion_tags(sentence), emotion  # 兜底剥离残留/残缺标签
                if buffer == "":
                    break

        self.total_time = round(time.time() - t_start, 2)

        # 收尾：剩余未切的内容作为最后一句
        if buffer.strip():
            yield strip_emotion_tags(buffer), emotion


# ──────────────────────────────────────────────
# 具体厂商实现（只填配置；逻辑全部在 OpenAICompatLLM）
# 切换：.env 的 LLM_PROVIDER=deepseek|qwen
# ──────────────────────────────────────────────
class DeepSeekLLM(OpenAICompatLLM):
    """DeepSeek（https://api.deepseek.com）"""

    def __init__(self):
        super().__init__(
            api_key=os.getenv("DEEPSEEK_API_KEY"),
            base_url="https://api.deepseek.com",
            model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
            extra_body={"thinking": {"type": "disabled"}},  # DeepSeek 关闭思考模式
        )


class QwenLLM(OpenAICompatLLM):
    """阿里云百炼 Qwen（OpenAI 兼容模式）

    配置（.env）：
        QWEN_LLM_MODEL=qwen-flash
        QWEN_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
        QWEN_LLM_API_KEY=（留空则用 DASHSCOPE_API_KEY）
    """

    def __init__(self):
        super().__init__(
            api_key=os.getenv("QWEN_LLM_API_KEY") or os.getenv("DASHSCOPE_API_KEY"),
            base_url=os.getenv("QWEN_LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            model=os.getenv("QWEN_LLM_MODEL", "qwen-flash"),
            extra_body=None,  # Qwen 兼容接口暂无特殊参数（需要时在此配置）
        )
