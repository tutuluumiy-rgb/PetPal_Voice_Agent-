import sys

sys.path.insert(0, ".")
from providers.minimax_tts import MiniMaxTTS  # noqa: E402

CASES = [
    ("半角书名号停顿", "哼〈#0.5#〉，要听笑话是吧"),
    ("半角尖括号拟声", "全场猫都笑了<laughs>，裁判<groans>，<breath>"),
    ("全角括号拟声", "（breath）累死了，＜groans＞哼"),
    ("全角圆括号中文旁白", "好嘛（憋笑）这下没面子了"),
    ("正确样例应保持", "累(breath)死了…<#0.6#>那我要去看看"),
]

t = MiniMaxTTS()
for name, s in CASES:
    print(f"[{name}] {s}")
    print("   ->", t._clean_text(s))
print("\n编译?" )
