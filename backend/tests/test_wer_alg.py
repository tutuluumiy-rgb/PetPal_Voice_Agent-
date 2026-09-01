# test_wer_alg.py — 纯算法自测：CER/WER 计算（无真实服务依赖）
# 与 backend/asr_service.py 的算法一致，独立验证正确性
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

def levenshtein(a, b):
    m, n = len(a), len(b)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(m + 1): dp[i][0] = i
    for j in range(n + 1): dp[0][j] = j
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            dp[i][j] = min(dp[i - 1][j] + 1, dp[i][j - 1] + 1, dp[i - 1][j - 1] + cost)
    return dp[m][n]

def cer(ref, hyp):
    ref = ref.replace(" ", "")
    hyp = hyp.replace(" ", "")
    if not ref: return None
    return levenshtein(ref, hyp) / len(ref)

# 用例
cases = [
    ("今天天气怎么样", "今天天汽怎么样", 1/7),   # 1 字替换
    ("你好", "你好", 0.0),                       # 完全一致
    ("你好", "你好呀", 1/2),                     # 插入
    ("下午三点开会", "", 1.0),                    # 全删
]

ok = True
for ref, hyp, expect in cases:
    got = cer(ref, hyp)
    status = "PASS" if abs(got - expect) < 1e-6 else "FAIL"
    if status == "FAIL": ok = False
    print(f"[{status}] cer('{ref}', '{hyp}') = {got:.4f} (期望 {expect:.4f})")

print("\nCER 算法测试:", "全部通过" if ok else "有失败")
sys.exit(0 if ok else 1)