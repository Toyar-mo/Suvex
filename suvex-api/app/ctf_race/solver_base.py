"""Solver 结果类型、状态常量、solver 协议 —— 全后端共享（学 ctf-agent
`solver_base.py`，但改成 Suvex 的形状）。

## 为什么要一个 `SolveOneFn` 协议而不是 solver 类

ctf-agent 里 `SolverProtocol` 是「start / run_until_done_or_gave_up / bump
/ stop」四件套的**有状态对象**——因为它的 solver 要在一个长生命周期里反复
被 bump 续跑。而 Suvex 的 `solve_in_sandbox` 是「一次调用跑到产出候选或
回合用尽」的**无状态函数**。

把 bump 表达成「再调一次 solve，把兄弟洞察作为 `hint` 注入」，比维护一堆
有状态 solver 对象简单得多，也复用了 Suvex 已验证的解题循环。所以这里
`solver_base` 只保留**纯数据**（状态常量 + `SolverResult`），真正的执行
函数签名在 `swarm.SolveOneFn`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ── 状态常量（与 ctf-agent 同名，便于对照阅读）──────────────────────────
FLAG_FOUND = "flag_found"   # 拿到了被平台确认的 flag
GAVE_UP = "gave_up"         # 这一轮没找到、但可 bump 续跑
CANCELLED = "cancelled"     # 被取消（兄弟已胜 / 用户停）
ERROR = "error"             # 跑了但出错
QUOTA_ERROR = "quota_error"  # 额度/接入点问题（adapter 可换网关重试）

#: 平台回执里代表「答对了」的标记（Suvex `SubmitOutcome.is_solved` 已表达，
#: 这里保留一份用于 adapter 归一化）。
CORRECT_MARKERS = ("CORRECT", "ALREADY SOLVED")


@dataclass
class SolverResult:
    """一个模型的一次求解结果。

    `flag` 只有在**平台确认**后才非空——模型自认为找到但没过提交的，不放这里
    （放 `findings_summary`）。这是 ctf-agent 同款纪律：flag 是平台盖章的，
    不是模型自称的。
    """

    model: str
    status: str = GAVE_UP
    flag: str | None = None
    findings_summary: str = ""
    step_count: int = 0
    cost_usd: float = 0.0
    #: 本轮产出的「未确认候选 flag」——用于 sibling 洞察广播，不作为最终结果。
    candidate_flag: str | None = None
    #: 任意后端附带的诊断信息（adapter 填）。
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_win(self) -> bool:
        return self.status == FLAG_FOUND and bool(self.flag)

    @property
    def is_broken(self) -> bool:
        """0 步且 0 花费 = 环境坏了/没真跑，不该被 bump（ctf-agent 同款判据）。"""
        return self.step_count == 0 and self.cost_usd == 0.0


__all__ = [
    "CANCELLED",
    "CORRECT_MARKERS",
    "ERROR",
    "FLAG_FOUND",
    "GAVE_UP",
    "QUOTA_ERROR",
    "SolverResult",
]
