"""Smart Turn v3 真实数据探针：验证"窗口语义"（整段话 vs 尾部切片）的判别力

用途（一次性标定工具，不参与运行时链路）：
1. 从 HF datasets-server 拉取带 endpoint_bool 标签的 Smart Turn v3.1 test 音频；
2. miniaudio 解码为 16k mono int16；
3. 分别按两种窗口口径喂给本仓库的 SmartTurnJudge：
   a) 整段话（上游 pipecat 口径：自语音开始到结束，≤8s，后补/前补零）→ 期望与标签强相关；
   b) 尾部 1.6s（改造前"只在 speech_end 带尾部切片"的口径）→ 比照其退化程度。
输出：每样本 p 值 + 两口径的均值差/方向一致率，用于决定集成侧窗口策略。

用法：cd backend && python scripts/smart_turn_probe.py
"""

import io
import ssl
import sys
import urllib.request

import miniaudio
import numpy as np

sys.path.insert(0, ".")  # 使 scripts 目录下可 import backend 模块
from smart_turn import SmartTurnJudge

SR = 16000
BASE = "https://datasets-server.huggingface.co/rows"
DATASET = "pipecat-ai%2Fsmart-turn-data-v3.1-test"
CTX = ssl._create_unverified_context()


def fetch_rows(offset: int, length: int = 10) -> list[dict]:
    u = f"{BASE}?dataset={DATASET}&config=default&split=train&offset={offset}&length={length}"
    d = urllib.request.urlopen(
        urllib.request.Request(u, headers={"User-Agent": "python"}), context=CTX, timeout=60
    ).read()
    import json
    return json.loads(d)["rows"]


def decode_audio(flac_bytes: bytes) -> np.ndarray:
    dec = miniaudio.decode(
        flac_bytes,
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=1,
        sample_rate=SR,
    )
    pcm = bytes(dec.samples)
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


def b64_pcm(f: np.ndarray) -> bytes:
    return (np.clip(f, -1, 1) * 32767).astype(np.int16).tobytes()


def p_of_whole(j: SmartTurnJudge, f: np.ndarray) -> float | None:
    """上游口径：整段话喂给 judge（judge 内部会截/补到 8s 保留尾部）"""
    return j.judge(b64_pcm(f))


def p_of_tail(j: SmartTurnJudge, f: np.ndarray, ms: int) -> float | None:
    """尾部切片口径：只取最后 ms 毫秒"""
    n = int(SR * ms / 1000)
    tail = f[-n:] if f.size > n else f
    return j.judge(b64_pcm(tail))


def main() -> int:
    j = SmartTurnJudge("models/smart_turn_v3.onnx")
    print(f"model available: {j.available}")

    rows = []
    offsets = [0, 300, 800, 1500, 3000, 6000, 9000, 12000]
    for off in offsets:
        try:
            rows += fetch_rows(off, length=6)
        except Exception as e:
            print(f"offset {off} fetch fail: {e}")

    # 按标签计数，各取最多 10 条，保证两类均衡
    pos = [r for r in rows if r["row"]["endpoint_bool"] is True][:10]
    neg = [r for r in rows if r["row"]["endpoint_bool"] is False][:10]
    pick = pos + neg
    print(f"samples: total={len(pick)} (finished={len(pos)}, unfinished={len(neg)})")

    w_ok = t_ok = 0
    rows_out = []
    for i, r in enumerate(pick):
        row, lbl = r["row"], r["row"]["endpoint_bool"]
        src = row["audio"][0]["src"]
        try:
            audio = urllib.request.urlopen(
                urllib.request.Request(src, headers={"User-Agent": "python"}), context=CTX, timeout=60
            ).read()
            f = decode_audio(audio)
        except Exception as e:
            print(f"[{i}] skip (fetch/decode): {e}")
            continue
        pw = p_of_whole(j, f)
        pt = p_of_tail(j, f, 1600)
        w_ok += pw is not None and (pw > 0.5) == bool(lbl)
        t_ok += pt is not None and (pt > 0.5) == bool(lbl)
        rows_out.append((i, lbl, f.size / SR, pw, pt, row.get("language"), row.get("dataset")))
        print(f"{i:>2} label={int(lbl)} dur={f.size/SR:5.2f}s | whole_p={pw:.3f} tail1600_p={pt:.3f} | lang={row.get('language')} ds={row.get('dataset')}")

    n = len(rows_out)
    if n:
        print(f"\naccuracy (p>0.5 == label): whole={w_ok}/{n} ({w_ok/n*100:.0f}%)  tail1.6s={t_ok}/{n} ({t_ok/n*100:.0f}%)")

    # test.mp3 真 TTS 语音（无标签，仅观察）
    try:
        with open("test.mp3", "rb") as fh:
            f = decode_audio(fh.read())
        print(f"\ntest.mp3 (TTS): dur={f.size/SR:.2f}s whole_p={p_of_whole(j, f):.3f} tail1600_p={p_of_tail(j, f, 1600):.3f}")
    except Exception as e:
        print(f"test.mp3 skip: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())