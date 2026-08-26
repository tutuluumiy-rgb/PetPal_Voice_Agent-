"""TTS 语气指令实验库 —— instructions 单独列在这里，方便你集中填写与调试

用途：
  这是「测试专用」的指令库，tts_cli.py 里用 lib=编号 快速选用；
  手动临时指令仍可用 instr=xxx，情绪模板仍可用 emo=xxx。

  CLI 用法：
      lib             # 打印全部指令菜单（编号 + 名称 + 指令原文 + 备注）
      lib=3           # 选用第 3 条指令（替换当前 instr/emo）

填写说明：
  - 每条 = (名称, 指令文本, 备注)
  - 指令文本最终会原样作为 instructions 传给阿里 qwen3-tts-instruct，
    建议写「自然语言描述」而不是抽象词（它吃自然语言）
  - 编号 = 列表顺序（从 0 开始），加新实验指令直接在 HUMAN_FLAVOR_PRESETS 里往下加
"""

from voice_style import EMOTION_INSTRUCTIONS

# ── ① 情绪模板（自动同步 voice_style.py 的 7 条，正式链路用的同一份）──
EMOTION_PRESETS = [(name, text, "情绪模板") for name, text in EMOTION_INSTRUCTIONS.items()]

# ── ② 人味实验指令（调试用，可自行增删改）──
#    结构：(名称, 指令文本, 备注)
HUMAN_FLAVOR_PRESETS = [
    ("呼吸感",            "说话时自然地换气，句与句之间带点呼吸声，不要太满",       "测：呼吸/换气"),
    ("慢悠关键句",        "整体慢悠悠，说到重点那句稍微放慢加重",                   "测：句级语速变化"),
    ("词级轻重",          "关键词放慢放重，修饰的话轻轻带过，像聊天不是念稿",       "测：句内语速+重音"),
    ("叹气委屈",          "先轻轻叹一口气，然后用委屈巴巴的声音慢慢说",             "测：呼吸+情绪"),
    ("轻快兴奋",          "语速稍快，尾音上扬，像分享高兴的事",                     "对照：兴奋"),
    ("慵懒犯困",          "慢悠悠、拖长音，像刚睡醒不想说话",                       "对照：困"),
    ("思考停顿",          "开头先停顿一下，说『这个嘛』的时候慢一点拖一点",         "测：思考感"),

    # ── 在这里往下加你的实验指令 ──────────────────────────
    # ("名称", "指令文本", "备注"),
]

# 合并：编号 = EMOTION_PRESETS(0~6) → HUMAN_FLAVOR_PRESETS(7~)
ALL_PRESETS = EMOTION_PRESETS + HUMAN_FLAVOR_PRESETS


def list_presets_text() -> str:
    """生成指令库菜单文本（CLI 的 lib 命令用）。"""
    lines = [f"[指令库] 共 {len(ALL_PRESETS)} 条（lib=<编号> 选用）"]
    for i, (name, text, note) in enumerate(ALL_PRESETS):
        lines.append(f"  {i:>2}  {name:<6} {note}  → {text}")
    return "\n".join(lines)


def get_preset(idx: int):
    """按编号取 (名称, 指令文本, 备注)；越界返回 None。"""
    if 0 <= idx < len(ALL_PRESETS):
        return ALL_PRESETS[idx]
    return None