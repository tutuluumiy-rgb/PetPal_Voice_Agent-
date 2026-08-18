"""双模式全局状态：闲聊 / 工作 + 语音切换指令解析（方案 A）

识别规则（收紧为「切换意图」）：
  - 必须同时命中【切换动词】与【目标模式词】才判定为切换指令（防止"你现在是什么模式？"
    这种状态查询被当成切换）。
  - 状态查询句（如"你现在是闲聊还是工作模式？"）不含切换动词 → 不触发切换，
    作为普通问句送 LLM 回答（LLM 通过系统提示词里的"当前模式"标注答对）。
  - 取消无目标 toggle：必须指明目标模式才动作。

切换动词清单（可扩展）：
  打开 开启 启动 切到 切换 切成 切去 切换到 换成 换到 改为 改成 改到 设为
  设置为 调成 调整成 转入 转成 变成 变为 进入 开工
目标模式词：
  工作：工作模式 / 工作 / 干活 / 执行 / 办公
  闲聊：闲聊模式 / 闲聊 / 聊 / 休闲
"""

from __future__ import annotations

CHAT_MODE = "chat"
WORK_MODE = "work"

MODE_NAMES = {CHAT_MODE: "闲聊模式", WORK_MODE: "工作模式"}

# ── 切换动作动词（命中任一：标识"要切换"意图）──────────────────
SWITCH_VERBS = [
    "打开", "开启", "启动",
    "切到", "切换", "切成", "切去", "切换到", "切去",
    "换成", "换到", "换一下",
    "改为", "改成", "改到", "改一下",
    "设为", "设置为", "调成", "调整成",
    "转入", "转成", "变成", "变为",
    "进入", "开工",
]
# ── 目标模式关键词 ───────────────────────────────────────────
WORK_KEYS = ["工作模式", "工作", "干活", "执行", "办公"]
CHAT_KEYS = ["闲聊模式", "闲聊", "聊", "休闲"]

# 去重用（保持任一命中即可）
_DEDUP = dict.fromkeys


class ModeState:
    """双模式全局状态（单例，随会话存活）。"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.mode = CHAT_MODE  # 默认闲聊
        return cls._instance

    def get_mode(self) -> str:
        return self.mode

    def switch(self, mode: str) -> str:
        if mode not in (CHAT_MODE, WORK_MODE):
            raise ValueError(f"未知模式: {mode}")
        self.mode = mode
        return self.mode

    def toggle(self) -> str:
        """手动切换（前端按钮/控制消息用）：在两者间取反面。

        注意：语音指令解析【不含】toggle（见 parse_mode_command），
        只保留给手动场景使用。
        """
        self.mode = WORK_MODE if self.mode == CHAT_MODE else CHAT_MODE
        return self.mode

    def name(self) -> str:
        return MODE_NAMES.get(self.mode, self.mode)


def get_mode_state() -> ModeState:
    return ModeState()


def parse_mode_command(text: str):
    """解析语音文本是否命中「切换指令」（方案 A）。

    规则：必须【同时】有切换动词 + 目标模式词才判定为切换。
    返回：
        (True, WORK_MODE|CHAT_MODE)  命中了明确的切换指令
        (False, None)                未命中（普通对话 / 状态查询，走 LLM 处理）
    """
    if not text:
        return False, None
    t = text.strip()

    has_verb = any(v in t for v in _DEDUP(SWITCH_VERBS))
    if not has_verb:
        return False, None  # 无切换动词 → 不触发（状态查询/普通对话）

    if any(k in t for k in _DEDUP(WORK_KEYS)):
        return True, WORK_MODE
    if any(k in t for k in _DEDUP(CHAT_KEYS)):
        return True, CHAT_MODE
    return False, None  # 有动词但没指明目标模式 → 不动作（等用户说清）


def build_switch_context(mode: str) -> str:
    """生成「切换状态」系统上下文，合并进用户输入一起送入大模型。

    让 LLM 知道已经切换成功、不必再执行或复述"切换"动作，直接处理用户本句的实际任务。
    """
    name = MODE_NAMES.get(mode, mode)
    return (
        "（系统状态通知：已经成功切换到" + name + "。"
        "这是一个系统切换状态，用户要的“切换成" + name + "”已经完成，"
        "不要再去执行或复述“切换”相关的动作，"
        "不要回复“已经切换到" + name + "”。"
        "请把注意力放在用户刚刚说出的请求上，直接开始处理它。）"
    )
