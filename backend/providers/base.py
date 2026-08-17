"""云接口抽象基类：ASR / TTS / LLM 的可插拔契约

目标：换接口（阿里云→其他厂商、DeepSeek→其他模型）只新增一个实现类 + 改 .env 配置，
管道代码（main.py 状态机）不感知具体实现。

三个基类的最小接口契约，对应各自同步/异步特性：
- ASR：同步累积音频，finalize 时异步识别
- TTS：异步流式合成（sync 方法 + async generator）
- LLM：异步流式生成（含工具自路由 agent_chat）
"""

from abc import ABC, abstractmethod


class ASRProvider(ABC):
    """语音识别接口"""

    @abstractmethod
    def start_streaming(self, session_id: str, on_partial):
        """开始新一轮识别会话，注册增量回调"""

    @abstractmethod
    def feed(self, session_id: str, pcm: bytes):
        """喂入 PCM 音频（同步累积）"""

    @abstractmethod
    async def finalize(self, session_id: str) -> str:
        """用户说完：识别累积音频，返回最终文本"""

    @abstractmethod
    def reset(self, session_id: str):
        """清理会话"""


class TTSProvider(ABC):
    """语音合成接口"""

    @abstractmethod
    def cancel(self):
        """打断时停止当前合成"""

    @abstractmethod
    async def synth_stream(self, text: str, params: dict | None = None):
        """流式合成：yield 音频块（PCM bytes）"""

    @abstractmethod
    async def speak_and_send(self, ws, text: str, session_id: str, params: dict | None = None):
        """流式合成并通过 WebSocket 发送给前端"""


class LLMProvider(ABC):
    """大模型接口"""

    @abstractmethod
    async def chat_stream(self, user_text: str, history: list):
        """无工具流式生成，逐句 yield (句子文本, 情绪标签)"""

    @abstractmethod
    async def agent_chat(self, user_text: str, history: list, on_progress=None):
        """工具自路由生成：LLM 自己决定调不调工具、调几轮，最终流式逐句 yield"""
