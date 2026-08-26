"""系统提示词加载与组装：人格 + 用户档案（全部独立成文件，可编辑不改代码）

注意：语音控制提示词（minimax_voice_style.md）【不拼入】大模型——
      让 LLM 生成 <#x#>/(breath) 等标签不可靠（会读出），停顿/拟声改由管道可靠处理。
      该文件仅存档参考，见 prompts/minimax_voice_style.md。

结构：
    prompts/personality.md           宠物人格（Markdown，直接改）
    prompts/_archive/voice_style_legacy.md  旧 instructions 方案归档
    users/                           用户档案
        registry.json                  用户注册表（id/name/role/profile 路径）
        user_001/profile.json          用户档案（basic + reply_style + likes/dislikes + daily）
    .env  ACTIVE_USER=user_001  当前启用哪个用户（暂时只启用 user_001）

组装：build_system_prompt() = agent.md + 用户档案 + 模式提示词 + 工具目录 + personality.md
（注入顺序以 build_system_prompt 的 parts 列表为准，用户指定：通用准则→档案→模式→工具→人格）
"""

import json
import os

from dotenv import load_dotenv

load_dotenv()  # 读取 backend/.env

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPTS_DIR = os.path.join(BASE_DIR, "prompts")
USERS_DIR = os.path.join(BASE_DIR, "users")

# 宠物 Agent 档案字段 → 中文标签（注入 LLM 时翻译，增强可读性）
_KEY_LABELS = {
    # daily
    "wake_time": "起床", "sleep_time": "睡觉",
}
_VALUE_LABELS = {
    "owner": "主人", "family": "家人", "guest": "访客",
}


def _label_key(k: str) -> str:
    return _KEY_LABELS.get(k, k)


def _label_value(v) -> str:
    if isinstance(v, bool):
        return "开启" if v else "关闭"
    s = str(v)
    return _VALUE_LABELS.get(s.lower(), s)


def load_prompt(name: str) -> str:
    """读取 prompts/ 下的 Markdown 提示词（不存在返回空串）"""
    path = os.path.join(PROMPTS_DIR, name)
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


def get_active_user_id() -> str:
    """当前启用用户（.env 配置，默认 user_001）"""
    return os.getenv("ACTIVE_USER", "user_001")


def _join_list(v) -> str:
    """列表转顿号分隔文本"""
    if isinstance(v, list):
        return "、".join(str(x) for x in v)
    return str(v) if v else ""


def load_user_profile(user_id: str) -> str:
    """从 users/<user_id>/profile.json 生成注入文本（宠物 Agent 格式）

    字段：basic(name/role) + reply_style(偏好回复风格) + likes(喜好) + dislikes(不喜欢) + daily(作息)
    """
    path = os.path.join(USERS_DIR, user_id, "profile.json")
    if not os.path.exists(path):
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            p = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[prompt_loader] 用户档案读取失败 {path}: {e}")
        return ""

    lines = ["## 当前用户信息"]
    basic = p.get("basic", {})
    if basic.get("name"):
        lines.append(f"当前用户：{basic['name']}（{_label_value(basic.get('role', '未知'))}）")
    if p.get("reply_style"):
        lines.append(f"偏好回复风格：{p['reply_style']}")
    likes = _join_list(p.get("likes"))
    if likes:
        lines.append(f"喜好：{likes}")
    dislikes = _join_list(p.get("dislikes"))
    if dislikes:
        lines.append(f"不喜欢：{dislikes}")
    daily = p.get("daily", {})
    if daily:
        lines.append("作息：" + "、".join(f"{_label_key(k)}: {_label_value(v)}" for k, v in daily.items()))
    return "\n".join(lines)


def build_system_prompt(mode: str | None = None) -> str:
    """组装完整系统提示词：通用准则→用户档案→模式提示词→工具目录→人格

    双模式（mode_state 的 CHAT_MODE / WORK_MODE）：
        chat（闲聊，默认）：agent.md + 用户档案 + chat_system_prompt.md + 闲聊工具目录 + personality.md
        work（工作）       ：agent.md + 用户档案 + work_system_prompt.md + 全量工具目录 + personality.md
    模式标注已写入模式提示词文件本身（chat_system_prompt.md / work_system_prompt.md），
    不再由代码动态插入独立段（用户要求）。
    工具目录（build_catalog_md(mode)）为动态生成——按模式过滤，新增工具后自动出现。
    mode=None 时按闲聊处理（向后兼容无参调用）。

    注入顺序（用户指定，2025-xx）：
        1. agent.md（通用准则）
        2. 用户档案（profile.json）
        3. 模式提示词（含模式标注）
        4. 工具目录
        5. personality.md（人格放最后，作为收尾）

    若后续要调整顺序，只改下面 parts 列表即可（文件内容不变）。
    """
    from tools import build_catalog_md
    from mode_state import CHAT_MODE, WORK_MODE

    mode = mode or CHAT_MODE
    mode_prompt = "work_system_prompt.md" if mode == WORK_MODE else "chat_system_prompt.md"

    parts = [
        load_prompt("agent.md"),            # 1. 通用准则（工具使用/记忆/输出限制）
        load_user_profile(get_active_user_id()),  # 2. 用户档案（profile.json）
        load_prompt(mode_prompt),           # 3. 模式专用提示词（含模式标注）
        build_catalog_md(mode),             # 4. 工具目录（按模式过滤）
        load_prompt("personality.md"),      # 5. 人格（最后收尾）
    ]
    return "\n\n".join(p for p in parts if p)
