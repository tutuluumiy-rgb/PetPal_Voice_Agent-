"""审批决策：对副作用工具的授权判断（Harness 精简版）

语音后端没有终端/图形界面可让用户敲 y/n，因此默认【不强制走交互审批】——
approval_policy 保留完整 API 与 AUTO_APPROVE / REQUIRE_APPROVAL 常量（供工具文件
的 TOOL_SPEC 声明），但实际执行端按 enforce_approval 开关决定：
    enforce_approval = False（默认）→ 一律放行（工作模式内全自动，语音场景可用）
    enforce_approval = True        → 走输入交互审批（需有终端，通常用于本地测试）

权限的真正收紧点在 tools/loader.py：按「模式 × 工具白名单」在调用前校验，
不在白名单内的工具直接拒绝，不用到这里。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from security import redact_for_log


AUTO_APPROVE = "auto_approve"
REQUIRE_APPROVAL = "require_approval"


@dataclass
class ApprovalDecision:
    allowed: bool
    status: str
    reason: str


@dataclass
class ApprovalPolicy:
    """对工具动作的授权判断。

    参数:
        modes           工具名 → AUTO_APPROVE/REQUIRE_APPROVAL（供查询元数据）
        input_func      输入函数（enforce 时用）
        output_func     输出函数（enforce 时用）
        enforce_approval 是否强制走用户交互审批（默认 False，语音场景全自动）
    """

    modes: dict = field(default_factory=dict)
    input_func: callable = input
    output_func: callable = print
    enforce_approval: bool = False

    def authorize(
        self,
        tool_name,
        arguments,
        *,
        force_approval=False,
        approval_id=None,
    ):
        mode = self.modes.get(tool_name, REQUIRE_APPROVAL)

        # 默认不强制审批：直接放行（语音场景全自动）。
        # 仅当显式开启强制审批，且该工具声明为需审批时，才走交互。
        if not self.enforce_approval:
            return ApprovalDecision(
                allowed=True,
                status=("auto_approved" if mode == AUTO_APPROVE else "approved_by_policy"),
                reason="审批已设为自动放行（enforce_approval=False）。",
            )

        if mode == AUTO_APPROVE and not force_approval:
            return ApprovalDecision(
                allowed=True,
                status="auto_approved",
                reason="该工具被声明为只读或仅修改当前进程状态。",
            )

        self.output_func(
            f"\n[审批请求] 工具={tool_name} 参数={redact_for_log(arguments)}\n"
            "输入 y 执行；直接回车或输入其他内容拒绝："
        )
        answer = str(self.input_func("审批: ")).strip().lower()
        if answer in {"y", "yes"}:
            return ApprovalDecision(
                allowed=True,
                status="approved_by_user",
                reason="用户在终端确认执行。",
            )
        return ApprovalDecision(
            allowed=False,
            status="denied_by_user",
            reason="用户未确认该副作用动作。",
        )
