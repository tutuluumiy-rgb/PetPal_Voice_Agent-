"""LLM 大脑引擎

DeepSeek + function calling，让球球能「干活」。
输出格式：回复文本 + 情绪标签（用于驱动 TTS 语气）。

API 密钥从环境变量 DEEPSEEK_API_KEY 读取（在 .env 文件里配置）。
情绪标签约定：
[开心] [委屈] [困] [好奇] [兴奋] [平静]
"""

import os
import re

from dotenv import load_dotenv
from openai import AsyncOpenAI

from personality import PERSONA_PROMPT
from voice_style import VOICE_GUIDE

load_dotenv()  # 读取 backend/.env


class ChatEngine:
    def __init__(self):
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError("未配置 DEEPSEEK_API_KEY，请在 backend/.env 里填写")

        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url="https://api.deepseek.com",
        )
        self.model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

    async def chat(self, user_text: str, history: list) -> tuple[str, str]:
        """返回 (回复文本, 情绪标签) —— 兼容旧接口，内部用 chat_stream 拼接"""
        text = ""
        emotion = "平静"
        async for sentence, emo in self.chat_stream(user_text, history):
            if text == "":
                emotion = emo
            text += sentence
        return text, emotion

    async def chat_stream(self, user_text: str, history: list):
        """流式生成，逐句 yield (句子文本, 情绪标签)

        边生成边按标点切句，凑够一句立即 yield，不等整段生成完。
        这样 main.py 可以逐句发给 TTS，实现「LLM 生成第一句就开播」。
        """
        import time

        # system prompt = 人格 + 语气输出要求
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

        # 句子切分标点：遇到这些就认为一句结束
        SENTENCE_ENDS = "。！？!?；;…\n"
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
                m = re.search(r"\[(开心|委屈|困|好奇|兴奋|平静|难过|害怕)\]", buffer)
                if m:
                    emotion = m.group(1)
                    buffer = buffer.replace(m.group(0), "", 1)
                    emotion_parsed = True
                    # 如果情绪标签后面紧跟标点，可能已经是一句
                    if buffer.strip() and buffer.strip()[-1] in SENTENCE_ENDS:
                        sentence = buffer.strip()
                        buffer = ""
                        yield sentence, emotion
                        continue

            # 按标点切句：找到最后一个标点，把之前的完整句切出来
            while True:
                cut = -1
                for i, ch in enumerate(buffer):
                    if ch in SENTENCE_ENDS:
                        cut = i
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

    def _parse_emotion(self, raw: str) -> tuple[str, str]:
        """从回复里提取情绪标签"""
        m = re.search(r"\[(开心|委屈|困|好奇|兴奋|平静|难过|害怕)\]", raw)
        if m:
            emotion = m.group(1)
            text = raw.replace(m.group(0), "").strip()
        else:
            emotion = "平静"
            text = raw.strip()
        return emotion, text
