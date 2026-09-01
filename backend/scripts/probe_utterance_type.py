# -*- coding: utf-8 -*-
"""句类快速探测：疑问句 vs 祈使/陈述句的"话是否完整"得分（SmartTurn 真实模型）"""
import asyncio
import sys

import miniaudio
import numpy as np

sys.path.insert(0, ".")
from smart_turn import SmartTurnJudge

j = SmartTurnJudge("models/smart_turn_v3.onnx")
j.threshold = 0.5
SR = 16000

SENTENCES = {
    "祈使-帮我查下天气": "帮我查下天气",
    "陈述-我在家": "我在家",
    "疑问-你在干嘛": "你今天在干嘛",
    "疑问-今天天气怎么样": "今天天气怎么样",
    "疑问-你吃饭了吗": "你吃饭了吗",
    "疑问-现在几点啦": "现在几点啦",
    "陈述-好的我知道了谢谢你": "好的我知道了谢谢你",
    "祈使-给我唱首歌吧": "给我唱首歌吧",
}


async def synth(text, out):
    import edge_tts
    await edge_tts.Communicate(text, "zh-CN-XiaoxiaoNeural", rate="-10%").save(out)


def load(n):
    with open(n, "rb") as f:
        d = miniaudio.decode(f.read(), output_format=miniaudio.SampleFormat.SIGNED16,
                             nchannels=1, sample_rate=SR)
    return np.frombuffer(bytes(d.samples), dtype=np.int16).astype(np.float32) / 32768.0


def p_of(x, tail_ms):
    y = np.concatenate([x, np.zeros(int(SR * tail_ms / 1000))])
    return j.judge((np.clip(y, -1, 1) * 32767).astype(np.int16).tobytes())


async def main():
    print(f"{'句类':26s} {'时长':>5s} {'+0ms':>7s} {'+600ms静音':>10s} {'+1200ms':>9s}")
    for i, (k, text) in enumerate(SENTENCES.items()):
        await synth(text, f"probe_q{i}.mp3")
        x = load(f"probe_q{i}.mp3")
        p0, p6, p12 = p_of(x, 0), p_of(x, 600), p_of(x, 1200)
        print(f"{k:26s} {len(x)/SR:5.2f}s {p0:7.3f} {p6:10.3f} {p12:9.3f}")


if __name__ == "__main__":
    asyncio.run(main())