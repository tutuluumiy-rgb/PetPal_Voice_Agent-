"""Harness 薄框架层：安全工具函数

- redact_for_log：把工具参数中可能的敏感字段（key/token/secret/password等）脱敏，
  用于审批/日志展示。
- environment_without_secrets：剔除环境变量里的敏感项，供 bash 子进程使用，
  避免把本机密钥泄漏给被执行的命令。
"""

from __future__ import annotations

import os
import re

# 键名命中这些即视为敏感（大小写不敏感）
_SENSITIVE_KEY = re.compile(
    r"(secret|password|passwd|token|api[_-]?key|access[_-]?key|"
    r"authorization|bearer|credential|private[_-]?key|app[_-]?secret)"
)


def redact_for_log(value, redacted: str = "[REDACTED]") -> str:
    """对参数做递归脱敏，用于日志/审批展示。

    - 值为 dict/list 时递归处理
    - key 命中敏感词 → 值替换为 redacted
    - 其他原样字符串化
    """
    if isinstance(value, dict):
        return {
            str(k): (redacted if _SENSITIVE_KEY.search(str(k).lower()) else redact_for_log(v, redacted))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_for_log(v, redacted) for v in value]
    return value


def environment_without_secrets(env: dict) -> dict:
    """返回去掉敏感环境变量后的副本，供子进程使用。"""
    cleaned = dict(env)
    for key in list(cleaned.keys()):
        if _SENSITIVE_KEY.search(key.lower()):
            cleaned.pop(key, None)
    return cleaned


def redact_env_for_display(env: dict) -> dict:
    """展示用：敏感环境变量值脱敏（key 保留，value 替换）。"""
    return {
        key: ("[REDACTED]" if _SENSITIVE_KEY.search(key.lower()) else value)
        for key, value in env.items()
    }
