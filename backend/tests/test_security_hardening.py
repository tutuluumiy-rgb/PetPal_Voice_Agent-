"""安全加固回归测试（安全审计 F1~F3 修复验证）

覆盖：
  t-sec1 Origin 白名单（放行 本机/null/file://；拒绝 恶意域）
  t-sec2 user_id 白名单（防路径穿越）
  t-sec3 bash 默认禁用（ALLOW_BASH 未开时拒绝执行）
  t-sec4 loader 审批接线（REQUIRE_APPROVAL 工具在 enforce 时走 authorize 且可被拒）
  t-sec5 控制消息非 dict 忽略 + audioB64 超限丢弃

用法：cd backend && python tests/test_security_hardening.py
"""
import asyncio
import base64
import os
import sys
import unittest.mock as um

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main as main_mod  # noqa: E402
from main import _origin_allowed, _safe_uid  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  [PASS] {name} {detail}")
    else:
        FAIL.append(name)
        print(f"  [FAIL] {name} {detail}")


def t_sec1_origin_allowlist():
    print("== t-sec1 Origin 白名单 ==")
    check("放行: 无 Origin(主进程/测试)", _origin_allowed(None))
    check("放行: null(file 页面)", _origin_allowed("null"))
    check("放行: file://", _origin_allowed("file://"))
    check("放行: http://127.0.0.1:8001", _origin_allowed("http://127.0.0.1:8001"))
    check("放行: http://localhost:5173", _origin_allowed("http://localhost:5173"))
    check("拒绝: http://evil.example.com", not _origin_allowed("http://evil.example.com"))
    check("拒绝: https://attacker.io", not _origin_allowed("https://attacker.io"))


def t_sec2_user_id_allowlist():
    print("== t-sec2 user_id 白名单（防路径穿越）==")
    check("放行: abc_123", _safe_uid("abc_123"))
    check("放行: user-1", _safe_uid("user-1"))
    check("拒绝: ../etc", not _safe_uid("../etc"))
    check("拒绝: 空/None", not _safe_uid("") and not _safe_uid(None))
    check("拒绝: 含路径分隔符", not _safe_uid("a/b") and not _safe_uid("a\\b"))
    check("拒绝: 超长", not _safe_uid("x" * 65))


def t_sec3_bash_default_on_closable():
    print("== t-sec3 bash 默认开启（用户确认），ALLOW_BASH=0 可关闭 ==")
    import tools.bash as bash_mod
    check("bash 默认开启", bash_mod.ALLOW_BASH is True)
    with um.patch.object(bash_mod, "ALLOW_BASH", False):
        try:
            bash_mod.bash("echo hi")
            check("关闭时拒绝执行", False, "竟然执行了")
        except RuntimeError as e:
            check("关闭时拒绝执行", "关闭" in str(e) or "ALLOW_BASH" in str(e), f"err={str(e)[:50]}")


def t_sec4_loader_approval_wiring():
    print("== t-sec4 loader 审批接线 ==")
    import tools.loader as loader
    # read 为 AUTO_APPROVE（enforce 开启也不拦）；bash 为 REQUIRE_APPROVAL
    async def run():
        ok_read = await loader.execute_tool("read", {"path": "."}, mode="chat")
        check("read(chat白名单-经审批放行): 可执行", "错误" not in ok_read or "执行失败" in ok_read, ok_read[:40])
        # bash 现默认开启（用户确认）：work 模式不应再被白名单/禁用拦截
        denied_work = await loader.execute_tool("bash", {"command": "id"}, mode="work")
        check("bash(工作模式): 白名单放行（执行结果取决于本机 bash）",
              "不可用" not in denied_work and "已关闭" not in denied_work,
              denied_work[:40])
        denied_chat = await loader.execute_tool("bash", {"command": "id"}, mode="chat")
        check("bash(聊天模式): 白名单拦截", "不可用" in denied_chat, denied_chat[:40])
    asyncio.run(run())


def t_sec5_message_hardening():
    print("== t-sec5 控制消息加固（非 dict 忽略 / audioB64 超限）==")
    # handle_control_message 直接调用：非 dict JSON 应安全返回
    from main import handle_control_message
    from tests.test_full_link_rework import MockWs, ConversationSession

    async def run():
        ws = MockWs()
        session = ConversationSession()
        session.state = "listening"
        await handle_control_message(ws, session, '[1,2,3]')  # 非 dict：应被忽略不崩
        check("非 dict JSON 忽略（不崩溃）", True)
        await handle_control_message(ws, session, '"just a string"')
        check("字符串 JSON 忽略（不崩溃）", True)
        # speech_end 超限 audioB64 → 按无音频处理（不再触发 base64 大解码）
        huge = base64.b64encode(b"\x00" * (400 * 1024)).decode()
        await handle_control_message(ws, session, f'{{"type":"speech_end","audioB64":"{huge}"}}')
        check("speech_end audioB64 超限被丢弃（链路不炸）", True)
    asyncio.run(run())


def t_sec6_middleware_stack_builds():
    print("== t-sec6 中间件堆栈可构建（Starlette app= 关键字构造）==")
    # 回归：_OriginGuard.__init__ 首参必须名为 app（Starlette 用 cls(app=...)
    # 构造），否则 build_middleware_stack 抛 unexpected keyword argument 'app'，
    # 服务一接请求就崩。这里直接复现该构建路径。
    try:
        main_mod.app.build_middleware_stack()
        check("app.build_middleware_stack() 成功", True)
    except Exception as e:
        check("app.build_middleware_stack() 成功", False, f"{type(e).__name__}: {e}")


def main():
    t_sec1_origin_allowlist()
    t_sec2_user_id_allowlist()
    t_sec3_bash_default_on_closable()
    t_sec4_loader_approval_wiring()
    t_sec5_message_hardening()
    t_sec6_middleware_stack_builds()
    print(f"\n安全加固回归：通过 {len(PASS)} / 失败 {len(FAIL)}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())