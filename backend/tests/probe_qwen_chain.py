"""千问(qwen)完整链路验证：LLM=qwen + 工具调用 + 多工具并行 + TTS 不断链"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ".")

os.environ["LLM_PROVIDER"] = "qwen"
os.environ["TTS_PROVIDER"] = "ali"

from main import handle_user_speech, ConversationSession, emotion_state  # noqa: E402


class MockWs:
    def __init__(self): self.messages = []; self.audio = 0
    async def send_json(self, obj): self.messages.append((time.time(), obj.get("type", "?"), obj))
    async def send_bytes(self, d): self.audio += len(d)


async def run(text, label):
    ws = MockWs()
    session = ConversationSession()
    session.last_asr_time = 0.5
    emotion_state.current = "平静"
    print(f"\n=== {label}: {text} ===", flush=True)
    t0 = time.time()
    try:
        await asyncio.wait_for(handle_user_speech(ws, session, text), timeout=90)
    except asyncio.TimeoutError:
        print("  [!] 链路超时", flush=True)
    td = time.time() - t0
    replies = [d for _, m, d in ws.messages if m == "reply"]
    appends = [d for _, m, d in ws.messages if m == "reply_append"]
    tts = [d for _, m, d in ws.messages if m == "tts_start"]
    timing = [d for _, m, d in ws.messages if m == "timing"]
    errs = [d for _, m, d in ws.messages if m == "reply" and "大脑暂时卡住" in d.get("text", "")]
    print(f"  耗时{td:.1f}s | reply={len(replies)} append={len(appends)} tts_start={len(tts)} timing={len(timing)} 音频={ws.audio//1024}KB", flush=True)
    print(f"  '大脑卡住'异常: {len(errs)}（期望0）", flush=True)
    full = "".join(d.get("text", "") for _, m, d in ws.messages if m in ("reply", "reply_append"))
    print(f"  完整回复: {full[:150]!r}", flush=True)


async def main():
    from providers import get_llm
    print(f"LLM={type(get_llm()).__name__}, model={get_llm().model}", flush=True)
    await run("帮我搜一下人工智能的最新新闻", "千问搜索工具")
    print("\n[done]", flush=True)


if __name__ == "__main__":
    asyncio.run(main())