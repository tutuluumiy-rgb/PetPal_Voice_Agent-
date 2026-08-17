"""环节2-场景复现：真实打断时序下 _confirm_real_speech 的判定

模拟球球说话期间用户插话的时序：
- cache = 球球回声(前) + 用户插话(后)，speech_start 到达时 cache 尾部 = 用户开口后音频
- preRoll = 用户开口前 256ms（含开口前瞬间，多为静音/球球回声）

验证两个拼接顺序：
  A. 当前代码：confirm = cache + preRoll（preRoll 拼末尾）→ 最近256ms = 开口前 → 疑似误拒根因
  B. 修复候选：confirm = preRoll + cache（preRoll 拼前面）→ 最近256ms = 开口后 → 应确认

用法：
  cd backend
  python test_confirm_scenario.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio

import numpy as np

SAMPLE_RATE = 16000


def downsample_24k_to_16k(pcm_bytes: bytes) -> bytes:
    data = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
    n_out = int(len(data) * 16000 / 24000)
    x_old = np.linspace(0, 1, len(data))
    x_new = np.linspace(0, 1, n_out)
    return np.interp(x_new, x_old, data).astype(np.int16).tobytes()


def amp_scale(pcm_bytes: bytes, scale: float) -> bytes:
    data = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
    return (data * scale).astype(np.int16).tobytes()


async def synth_16k(tts, text: str) -> bytes:
    chunks = []
    async for c in tts.synth_stream(text):
        chunks.append(c)
    return downsample_24k_to_16k(b"".join(chunks))


async def main():
    from main import _confirm_real_speech

    from providers.tts import AliyunTTS

    tts = AliyunTTS()

    # 球球回声（AEC 后残留弱化）—— 模拟球球正在说话
    ball = amp_scale(await synth_16k(tts, "一、二、三、四、五、六、七、八、九、十！数完啦！有奖励吗？"), 0.15)
    # 用户插话（AEC 削弱 x0.3）—— 模拟用户开口"从五数到十"
    user = amp_scale(await synth_16k(tts, "从五数到十"), 0.3)
    # 开口前 256ms：静音（用户开口前瞬间）
    pre_roll_silence = bytes(bytearray(int(SAMPLE_RATE * 0.256 * 2)))

    # 模拟时序：球球说了一阵后用户开口，speech_start 到达时：
    # cache 尾部 = 用户插话的开头 ~300ms（VAD延迟+传输延迟内用户继续说的部分）
    # 简化：cache = ball(前) + user(完整) ；preRoll = 开口前256ms静音
    cache = bytearray(ball + user)
    pre_roll = pre_roll_silence

    print("=" * 72)
    print("环节2c：真实打断时序下二次确认（阈值 0.45 / 占比 0.05 / 窗口 256ms）")
    print("=" * 72)
    print(f"  cache: 球球回声 {len(ball)/2/SAMPLE_RATE*1000:.0f}ms + 用户插话 {len(user)/2/SAMPLE_RATE*1000:.0f}ms")
    print(f"  preRoll: 开口前 256ms（静音）")

    # A. 当前代码：cache + preRoll
    confirm_a = bytearray(cache) + bytearray(pre_roll)
    dec_a = _confirm_real_speech(confirm_a)

    # B. 修复候选：preRoll + cache
    confirm_b = bytearray(pre_roll) + bytearray(cache)
    dec_b = _confirm_real_speech(confirm_b)

    print(f"\n  A. cache+preRoll（当前代码）: {'确认人声(打断)' if dec_a else '判定噪声(拒绝) ← 误报!'}")
    print(f"  B. preRoll+cache（修复候选）: {'确认人声(打断)' if dec_b else '判定噪声(拒绝)'}")

    # C. 极端场景：用户插话很短/很弱（只说"好"），验证窗口是否够敏感
    user_short = amp_scale(await synth_16k(tts, "好"), 0.3)
    cache_c = bytearray(ball + user_short)
    confirm_c = bytearray(pre_roll) + bytearray(cache_c)
    dec_c = _confirm_real_speech(confirm_c)
    print(f"\n  C. 用户只说'好'(x0.3) preRoll+cache: {'确认人声(打断)' if dec_c else '判定噪声(拒绝)'}")


if __name__ == "__main__":
    asyncio.run(main())
