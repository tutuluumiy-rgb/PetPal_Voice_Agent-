"""Smart Turn 阈值标定：用真实"说完/未说完"样本集分析，不拍脑袋定阈值

样本构成（label 依据）：
- 说完(1)：官方 smart-turn-v3.1-test 标注 endpoint_bool=True（10 条）
          + edge-tts 中文自然完句 s1~s4 + 完整犹豫句「嗯 如果 如果说 我想要换一种呢」
- 未说完(0)：官方标注 endpoint_bool=False（10 条）
            + 截断式未完（完整句砍到 50~60% 戛然而止）
            + 犹豫式未完（「嗯」/「嗯…如果」/「嗯…如果…如果说」——贴用户实际插话场景）

窗口口径（与运行时一致）：
- 首判 = 整段话 + ~320ms 尾静音（speech_end 时的真实输入）
- 重判 = 首判段 + 600ms 真实静音（收取窗口后的输入，SMART_TURN_REJUDGE_MS）

用法：cd backend && python scripts/smart_turn_calib.py （需 miniaudio；会联网拉官方测试集）
"""

import ssl
import sys
import urllib.request

import miniaudio
import numpy as np

sys.path.insert(0, ".")
from smart_turn import SmartTurnJudge

SR = 16000
CTX = ssl._create_unverified_context()
BASE = "https://datasets-server.huggingface.co/rows"
DATASET = "pipecat-ai%2Fsmart-turn-data-v3.1-test"


def fetch_official(off: int, length: int = 6):
    u = f"{BASE}?dataset={DATASET}&config=default&split=train&offset={off}&length={length}"
    import json
    d = urllib.request.urlopen(
        urllib.request.Request(u, headers={"User-Agent": "python"}), context=CTX, timeout=60
    ).read()
    return json.loads(d)["rows"]


def decode_mp3_or_flac(raw: bytes) -> np.ndarray:
    dec = miniaudio.decode(
        raw, output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1, sample_rate=SR
    )
    return np.frombuffer(bytes(dec.samples), dtype=np.int16).astype(np.float32) / 32768.0


def load_tts(name: str) -> np.ndarray:
    with open(f"probe_tts_{name}.mp3", "rb") as f:
        return decode_mp3_or_flac(f.read())


def b64_pcm(x: np.ndarray) -> bytes:
    return (np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes()


def silence(ms: int) -> np.ndarray:
    return np.zeros(int(SR * ms / 1000))


def main():
    j = SmartTurnJudge("models/smart_turn_v3.onnx")
    print(f"model available: {j.available}")

    samples = []  # (name, label, audio)

    # 1) 官方标注集
    rows = []
    for off in [0, 300, 800, 1500, 3000, 6000, 9000, 12000]:
        try:
            rows += fetch_official(off, length=6)
        except Exception as e:
            print(f"offset {off} fail: {e}")
    pos = [r for r in rows if r["row"]["endpoint_bool"] is True][:10]
    neg = [r for r in rows if r["row"]["endpoint_bool"] is False][:10]
    for i, r in enumerate(pos):
        src = r["row"]["audio"][0]["src"]
        a = urllib.request.urlopen(urllib.request.Request(src, headers={"User-Agent": "python"}), context=CTX, timeout=60).read()
        samples.append((f"official_fin_{i}", 1, decode_mp3_or_flac(a)))
    for i, r in enumerate(neg):
        src = r["row"]["audio"][0]["src"]
        a = urllib.request.urlopen(urllib.request.Request(src, headers={"User-Agent": "python"}), context=CTX, timeout=60).read()
        samples.append((f"official_unf_{i}", 0, decode_mp3_or_flac(a)))

    # 2) TTS 自然完句
    for n in ["s1", "s2", "s3", "s4"]:
        samples.append((f"tts_{n}_fin", 1, load_tts(n)))

    # 3) 完整犹豫句（说完：改了主意把话说完）
    samples.append(("hesi_full_fin", 1, load_tts("frag_full")))

    # 4) 截断式未完（完整句说到一半戛然而止 + 尾静音）
    for n, cut in [("s1", 0.55), ("s2", 0.5), ("s3", 0.4)]:
        x = load_tts(n)
        samples.append((f"tts_{n}_cut_unf", 0, np.concatenate([x[: int(len(x) * cut)], silence(320)])))

    # 5) 犹豫式未完（贴用户场景：只说了一两个片段就停，句尾上扬未决）
    frags = {"en": load_tts("frag_en"), "rugo": load_tts("frag_rugo"), "ruoshuo": load_tts("frag_ruoshuo")}
    samples.append(("hesi_en_unf", 0, np.concatenate([frags["en"], silence(320)])))
    samples.append(("hesi_en_rugo_unf", 0, np.concatenate([frags["en"], silence(250), frags["rugo"], silence(320)])))
    samples.append(("hesi_en_rugo_ruoshuo_unf", 0, np.concatenate([frags["en"], silence(250), frags["rugo"], silence(250), frags["ruoshuo"], silence(320)])))

    # ── 评分 ──
    rows_out = []
    for name, label, x in samples:
        p1 = j.judge(b64_pcm(x))  # 首判：整段话(含尾静音)
        p2 = j.judge(b64_pcm(np.concatenate([x, silence(600)])))  # 重判：+600ms 真实静音
        rows_out.append((name, label, round(len(x) / SR, 2), p1, p2))

    print(f"\n{'样本':32s} {'label':>5s} {'dur':>5s} {'首判p':>7s} {'+600ms重判p':>10s}")
    for name, label, dur, p1, p2 in rows_out:
        print(f"{name:32s} {label:>5d} {dur:>5.2f} {p1:>7.3f} {p2:>10.3f}")

    fin = [r for r in rows_out if r[1] == 1]
    unf = [r for r in rows_out if r[1] == 0]

    print("\n── 阈值扫描（首判口径）──")
    for th in [0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50]:
        tp = sum(1 for r in fin if r[3] is not None and r[3] > th)
        tn = sum(1 for r in unf if r[3] is not None and r[3] <= th)
        fp = sum(1 for r in unf if r[3] is not None and r[3] > th)   # 未说完被当说完 → 提前提交（丢合并）
        fn = sum(1 for r in fin if r[3] is not None and r[3] <= th)  # 说完被当未完 → 开窗等待（延迟）
        acc = (tp + tn) / (len(fin) + len(unf)) * 100
        print(f"θ={th:.2f} acc={acc:4.0f}% 说完≥θ={tp}/{len(fin)} 未完<θ={tn}/{len(unf)} | 误判完说(fp)={fp} 误判未完(fn)={fn}")

    # 重判口径下：未完样本是否会被错误地抬升到≥θ（若抬升 → 不该提前提交却提交了）
    print("\n── 重判后仍未说完（<0.3）的未完样本数 ──")
    risky = [r[0] for r in unf if r[4] is not None and r[4] >= 0.3]
    print("重判后 p≥0.3 的未完样本:", risky or "无")

    # 首判<0.3 但重判≥0.3 的说完样本（说明能被重判救回）
    rescued = [(r[0], r[3], r[4]) for r in fin if r[3] is not None and r[3] < 0.3 and r[4] is not None and r[4] >= 0.3]
    print("说完但首判<0.3、重判≥0.3（可被重判救回）:", rescued or "无")


if __name__ == "__main__":
    main()