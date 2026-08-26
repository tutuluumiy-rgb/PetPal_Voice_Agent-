"""验证工具执行失败兜底：1) _execute_tool_calls 异常不崩溃 2) 完整链路不出 400"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ".")

os.environ["TTS_PROVIDER"] = "ali"
os.environ["LLM_PROVIDER"] = "deepseek"


async def main():
    # 1. _execute_tool_calls 异常兜底
    from agent_runtime import _execute_tool_calls
    calls = [("call_1", "web_search", {"query": "人工智能新闻"})]
    try:
        executed = await _execute_tool_calls(calls, "chat")
        print(f"[1] _execute_tool_calls 返回 {len(executed)} 条（期望 1）", flush=True)
        for cid, res in executed:
            print(f"    call_id={cid} 结果前缀: {str(res)[:50]!r}", flush=True)
    except Exception as e:
        print(f"[1] 异常: {type(e).__name__}: {e}", flush=True)

    # 2. 完整链路（正常搜索，验证不再 400）
    from main import handle_user_speech, ConversationSession, emotion_state
    class MockWs:
        def __init__(self): self.messages = []; self.audio = 0
        async def send_json(self, obj): self.messages.append((time.time(), obj.get("type", "?"), obj))
        async def send_bytes(self, d): self.audio += len(d)
    ws = MockWs()
    session = ConversationSession()
    session.last_asr_time = 0.5
    emotion_state.current = "平静"
    print("\n[2] 完整工具链路（搜索新闻）...", flush=True)
    t0 = time.time()
    try:
        await asyncio.wait_for(handle_user_speech(ws, session, "帮我搜一下最新的AI新闻"), timeout=90)
    except asyncio.TimeoutError:
        print("   [!] 链路超时", flush=True)
    td = time.time() - t0
    replies = [d for _, m, d in ws.messages if m == "reply"]
    appends = [d for _, m, d in ws.messages if m == "reply_append"]
    tts = [d for _, m, d in ws.messages if m == "tts_start"]
    timing = [d for _, m, d in ws.messages if m == "timing"]
    errs = [d for _, m, d in ws.messages if m == "reply" and "大脑暂时卡住" in d.get("text", "")]
    print(f"  耗时 {td:.1f}s | reply={len(replies)} append={len(appends)} tts_start={len(tts)} timing={len(timing)} 音频={ws.audio//1024}KB", flush=True)
    print(f"  '大脑卡住'异常回复: {len(errs)} 个（期望 0）", flush=True)
    if timing:
        cur = timing[-1].get("current", {})
        print(f"  timing.current: {cur}", flush=True)
    full = "".join(d.get("text", "") for _, m, d in ws.messages if m in ("reply", "reply_append"))
    print(f"  完整回复: {full[:120]!r}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())