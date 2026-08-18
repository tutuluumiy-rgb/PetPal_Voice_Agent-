"""系统提示词加载与组装：人格 + 语气 + 用户档案（全部独立成文件，可编辑不改代码）

结构：
    prompts/personality.md    宠物人格（Markdown，直接改）
    prompts/voice_style.md    语气输出要求（Markdown，直接改）
    users/                    用户档案
        registry.json          用户注册表（id/name/role/profile 路径）
        user_001/profile.json  用户档案（basic + reply_style + likes/dislikes + daily）
    .env  ACTIVE_USER=user_001  当前启用哪个用户（暂时只启用 user_001）

组装：build_system_prompt() = personality.md + voice_style.md + 当前用户档案
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
    """组装完整系统提示词：人格 + 语气 + 工具指南 + 可用工具目录 + 当前用户档案

    双模式（mode_state 的 CHAT_MODE / WORK_MODE）：
        chat（闲聊，默认）：personality + voice_style + system_prompt.md + 闲聊工具目录
        work（工作）       ：personality + voice_style + work_system_prompt.md + 全量工具目录
    工具目录（build_catalog_md(mode)）为动态生成——按模式过滤，新增工具后自动出现。
    mode=None 时按闲聊处理（向后兼容无参调用）。
    """
    from tools import build_catalog_md
    from mode_state import CHAT_MODE, WORK_MODE

    mode = mode or CHAT_MODE
    system_part = "work_system_prompt.md" if mode == WORK_MODE else "system_prompt.md"

    # 显式标注当前模式：让 LLM 答"你现在是什么模式？"时能据实回答
    mode_label = "工作模式" if mode == WORK_MODE else "闲聊模式"
    mode_mark = f"## 当前模式\n你当前处于「{mode_label}」。\n用户若问你是什么模式，请直接回答：{mode_label}。若用户要切换模式，用【切换指令】处理，不要把它当普通聊天话题。"

    parts = [
        load_prompt("personality.md"),
        load_prompt("voice_style.md"),
        load_prompt(system_part),
        build_catalog_md(mode),  # 第一级披露：按模式过滤的工具目录
        mode_mark,
        load_user_profile(get_active_user_id()),
    ]
    return "\n\n".join(p for p in parts if p)
