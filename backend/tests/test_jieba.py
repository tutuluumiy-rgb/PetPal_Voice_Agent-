# test_jieba.py — 验证 jieba 分词可用性 + WER 计算
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import jieba
    seg = list(jieba.cut("今天天气怎么样"))
    print("jieba OK, seg =", seg)
except ImportError:
    print("jieba NOT installed - WER will fallback to char level")
    sys.exit(0)