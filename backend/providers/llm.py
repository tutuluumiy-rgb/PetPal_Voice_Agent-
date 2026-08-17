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

from personality import PERSONA_PROMPT
from voice_style import VOICE_GUIDE
from tools import get_tools
from agent_loop import TOOL_GUIDE, run_tool_loop
from .base import LLMProvider

load_dotenv()  # 读取 backend/.env

# 情绪标签正则（与 voice_style 一致）
_EMOTION_RE = re.compile(r"\[(开心|委屈|困|好奇|兴奋|平静|难过|害怕)\]")

# 句子切分标点：遇到这些就认为一句结束
SENTENCE_ENDS = "。！？!?；;…\n"


class DeepSeekLLM(LLMProvider):
    def __init__(self):
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError("未配置 DEEPSEEK_API_KEY，请在 backend/.env 里填写")

        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url="https://api.deepseek.com",
        )
        self.model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
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

        system_prompt = PERSONA_PROMPT + "\n" + VOICE_GUIDE
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
            extra_body={"thinking": {"type": "disabled"}},
        )

        async for sentence, emo in self._stream_sentences(stream, t_start):
            yield sentence, emo

    # ──────────────────────────────────────────────
    # 工具自路由 Agent 循环（方案 A 核心）
    # ──────────────────────────────────────────────
    async def agent_chat(self, user_text: str, history: list, on_progress=None, is_cancelled=None):
        """工具自路由生成。

        参数:
            user_text: 用户输入
            history: 对话历史（短期记忆）
            on_progress: async 回调，工具执行前收到进度文本（用于 TTS 播报）
            is_cancelled: callable，返回 True 时（打断）提前退出循环

        yield:
            ("progress", 文本)          # 进度播报
            ("reply", 句子, 情绪标签)    # 最终回复流式逐句
        """
        import time

        system_prompt = PERSONA_PROMPT + "\n" + VOICE_GUIDE + "\n" + TOOL_GUIDE
        messages = [{"role": "system", "content": system_prompt}]
        for msg in history[-20:]:
            messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": user_text})

        tools = get_tools()  # 方案 A：全量注入（3 个工具 ~1.5k tokens，无压力）

        # ── 工具循环：独立模块（agent_loop.py），改工具调用策略只动那个文件 ──
        messages, _ = await run_tool_loop(
            self.client,
            self.model,
            messages,
            tools,
            max_loops=self.max_loops,
            on_progress=on_progress,
            is_cancelled=is_cancelled,
        )

        # ── 最终轮：流式逐句（复用切句 + 情绪解析）──
        stream = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0.9,
            max_tokens=15000,
            stream=True,
            extra_body={"thinking": {"type": "disabled"}},
        )
        async for sentence, emo in self._stream_sentences(stream, time.time()):
            if is_cancelled and is_cancelled():
                print("[Agent] 最终轮检测到打断，停止输出")
                return
            yield "reply", sentence, emo

    # ──────────────────────────────────────────────
    # 内部：流式增量切句 + 情绪标签解析（chat_stream 与 agent_chat 共用）
    # ──────────────────────────────────────────────
    async def _stream_sentences(self, stream, t_start: float):
        """从 OpenAI 流式响应中切句，yield (句子, 情绪标签)"""
        buffer = ""
        emotion = "平静"
        emotion_parsed = False
        first_token_time = None

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
                        sentence = buffer.strip()
                        buffer = ""
                        yield sentence, emotion
                        continue

            # 按标点切句：找到【第一个】标点就切出一句
            while True:
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
                    yield sentence, emotion
                if buffer == "":
                    break

        self.total_time = round(time.time() - t_start, 2)

        # 收尾：剩余未切的内容作为最后一句
        if buffer.strip():
            yield buffer.strip(), emotion
