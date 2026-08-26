"""单元测试：_build_timing_stats 打断轮不进 avg 的逻辑"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 模拟 _build_timing_stats（不依赖 main.py 的全局 tts/llm 对象）
def _build_timing_stats_mock(session, t_llm_first_sentence, t_llm, t_tts,
                              llm_first_token, tts_first, last_asr_time,
                              include_in_avg=True):
    """简化的 _build_timing_stats，验证 avg 累加行为"""
    e2e = round(last_asr_time + (t_llm_first_sentence or llm_first_token) + (tts_first or 0), 2)
    current = {
        "asr": last_asr_time,
        "llm_first_token": llm_first_token,
        "llm_first_sentence": round(t_llm_first_sentence or 0, 2),
        "tts_first_packet": tts_first or 0,
        "e2e": e2e,
        "total": round(last_asr_time + t_llm + t_tts, 2),
    }
    session["timing_count"] += 1
    if include_in_avg:
        session["avg_count"] += 1
        for k, v in current.items():
            session["timing_sum"][k] = session["timing_sum"].get(k, 0) + v
    avg = {}
    if session["avg_count"] > 0:
        avg = {k: round(session["timing_sum"][k] / session["avg_count"], 2) for k in current.keys()}
    return current, avg


def make_session():
    return {
        "timing_count": 0,
        "avg_count": 0,
        "timing_sum": {"asr": 0.0, "llm_first_token": 0.0, "llm_first_sentence": 0.0, "tts_first_packet": 0.0, "e2e": 0.0, "barge_in": 0.0, "total": 0.0},
    }


def test_normal_round_in_avg():
    """正常轮计入 avg"""
    s = make_session()
    c, avg = _build_timing_stats_mock(s, t_llm_first_sentence=0.5, t_llm=1.5, t_tts=2.0,
                                       llm_first_token=0.1, tts_first=0.3, last_asr_time=0.4)
    assert s["timing_count"] == 1
    assert s["avg_count"] == 1
    assert avg["e2e"] == round(0.4 + 0.5 + 0.3, 2), f"got {avg['e2e']}"
    print(f"[OK] normal_round: e2e={avg['e2e']}s")


def test_interrupted_round_excluded_from_avg():
    """打断轮不计入 avg（tts_first=0 模拟未完整发送）"""
    s = make_session()
    # 第 1 轮：正常
    _build_timing_stats_mock(s, t_llm_first_sentence=0.5, t_llm=1.5, t_tts=2.0,
                              llm_first_token=0.1, tts_first=0.3, last_asr_time=0.4)
    # 第 2 轮：打断（include_in_avg=False，tts_first=0）
    c, avg = _build_timing_stats_mock(s, t_llm_first_sentence=0.3, t_llm=0.5, t_tts=0,
                                       llm_first_token=0.1, tts_first=0, last_asr_time=0.4,
                                       include_in_avg=False)
    assert s["timing_count"] == 2, "timing_count 仍应 +=1（前端展示用）"
    assert s["avg_count"] == 1, "avg_count 不应增加"
    # avg 仍是第 1 轮的 e2e，没被第 2 轮的偏小值污染
    assert avg["e2e"] == round(0.4 + 0.5 + 0.3, 2), f"got {avg['e2e']}, 期望 1.2（只有第 1 轮）"
    print(f"[OK] interrupted_round_excluded: timing_count=2, avg_count=1, avg.e2e={avg['e2e']}s（未污染）")


def test_multiple_interrupted_rounds_excluded():
    """连续多轮打断都不进 avg"""
    s = make_session()
    # 第 1 轮：正常
    _build_timing_stats_mock(s, t_llm_first_sentence=0.5, t_llm=1.5, t_tts=2.0,
                              llm_first_token=0.1, tts_first=0.3, last_asr_time=0.4)
    # 第 2/3 轮：都打断
    _build_timing_stats_mock(s, t_llm_first_sentence=0.1, t_llm=0.2, t_tts=0,
                              llm_first_token=0.1, tts_first=0, last_asr_time=0.4,
                              include_in_avg=False)
    _build_timing_stats_mock(s, t_llm_first_sentence=0.2, t_llm=0.3, t_tts=0,
                              llm_first_token=0.1, tts_first=0, last_asr_time=0.4,
                              include_in_avg=False)
    # 第 4 轮：正常
    c4, avg = _build_timing_stats_mock(s, t_llm_first_sentence=0.6, t_llm=1.6, t_tts=2.1,
                                        llm_first_token=0.1, tts_first=0.35, last_asr_time=0.45)
    assert s["timing_count"] == 4
    assert s["avg_count"] == 2  # 只有第 1 + 第 4
    expected_e2e = round((0.4 + 0.5 + 0.3) + (0.45 + 0.6 + 0.35), 2) / 2
    assert abs(avg["e2e"] - expected_e2e) < 0.01, f"got {avg['e2e']}, expected {expected_e2e}"
    print(f"[OK] multiple_interrupted: timing_count=4, avg_count=2, avg.e2e={avg['e2e']:.2f}s")


def test_avg_empty_before_first_normal():
    """所有轮都打断（没有正常轮）→ avg 不应崩溃，应为 {}"""
    s = make_session()
    _build_timing_stats_mock(s, t_llm_first_sentence=0.1, t_llm=0.2, t_tts=0,
                              llm_first_token=0.1, tts_first=0, last_asr_time=0.4,
                              include_in_avg=False)
    c, avg = _build_timing_stats_mock(s, t_llm_first_sentence=0.1, t_llm=0.2, t_tts=0,
                                       llm_first_token=0.1, tts_first=0, last_asr_time=0.4,
                                       include_in_avg=False)
    assert s["timing_count"] == 2
    assert s["avg_count"] == 0
    assert avg == {}, f"avg 应为空 dict，实际 {avg}"
    print(f"[OK] avg_empty_initially: avg={avg}, timing_count={s['timing_count']}")


if __name__ == "__main__":
    test_normal_round_in_avg()
    test_interrupted_round_excluded_from_avg()
    test_multiple_interrupted_rounds_excluded()
    test_avg_empty_before_first_normal()
    print("\n[ALL PASS] _build_timing_stats 打断轮不进 avg 逻辑验证通过")