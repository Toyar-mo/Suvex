"""`overview` 领域的出入参（契约 §3.6）。

对应前端 `data/overview.ts` 的 `OverviewData`。字段名逐字一致 ——
前端接线时不该需要改任何类型。
"""

from __future__ import annotations

from app.core.schemas import CamelModel
from app.core.timeutil import IsoDateTime


class OverviewMetricsOut(CamelModel):
    """`OverviewData.metrics`。

    ★ **两个字段可以为 `null`，那不是"没有数据"，是"无法计算"**
    （契约 §3.6 用 ⚠️ 标了）：

    * `newHighSeverityDelta: null` = 上周没有高危，**给不出同比**。
      返回 0 会被读成"与上周持平"，而真相是"没有可比对象"。
    * `avgScanDurationSec: null` = 一次扫描都没跑完过。
      返回 0 会让用户以为扫描是瞬间完成的。

    这类"用 0 冒充未知"的错误很难在界面上被发现 ——
    0 看起来永远像一个合理的数字。
    """

    activeScans: int
    # 本周新增的 critical + high，**排除误报**。
    newHighSeverityThisWeek: int
    # 与上周之差；上周为 0 时是 `null`。
    newHighSeverityDelta: int | None
    # 已验证 PoC 的条数，**排除误报**。
    verifiedPocCount: int
    # 已完成扫描的平均耗时（秒）；没有已完成扫描时 `null`。
    avgScanDurationSec: int | None
    # 被判误报、**未计入上面两个指标**的条数。
    falsePositiveCount: int

    # ── CTF2 token 消耗（v0.36）────────────────────────────
    #
    # 需求原话是"总览页要写一个统计 token 消耗，细分到每一次任务
    # 的消耗，和总消耗"。这里给**总量**；细分（按题、按模型、逐轮）
    # 在 `GET /ctf2/usage` 与 `GET /ctf2/challenges/{id}/usage`。
    #
    # ⚠️ 放在 overview 而不是只留在 CTF2 页面：token 是**成本**，
    #    而成本该和"扫了多少、发现了多少"放在同一屏上看。
    #    （与 `avgScanDurationSec` 同一条理由：那张卡片回答的是
    #    "这套东西烧掉了多少资源"。）

    #: LLM 调用总次数（**永远准确**，从行数数得出来）。
    ctf2LlmCalls: int = 0
    #: 其中**接入点真的返回了用量**的次数。
    #:
    #: ★★ 与 `ctf2LlmCalls` 必须分开显示：两者不等时 `ctf2TotalTokens`
    #: 是**下限**（有几次没算进去）。把"没记录"显示成"消耗 0"
    #: 会与"没跑过"混淆 —— 见 `KernelTokenUsageOut` 的说明。
    ctf2RecordedCalls: int = 0
    #: 总 token（prompt + completion）。**未做金额换算**（单价易变）。
    ctf2TotalTokens: int = 0
    #: 统计从哪一刻开始。为空 = 一条记录都没有。
    #:
    #: ⚠️ v0.36 **之前**的解题没有 usage 记录。不给这个字段的话，
    #:    用户会以为"总共就花了这么点"，而实际是**没记**。
    ctf2UsageSince: IsoDateTime | None = None


class SeverityBreakdownOut(CamelModel):
    """`OverviewData.severityBreakdown` 的一项。

    ⚠️ 这一项**保留 0 值**（与 `findings.bySeverity` 的 `Partial` 语义相反）。
    图表里缺一根柱子比列一根 0 的柱子更容易被误读成"漏了"，
    尤其是 medium/low 这种不常出现的级别。
    """

    severity: str
    count: int


class ProjectedActivityOut(CamelModel):
    """Overview 活动流的**投影形状**（契约 §2.5 右列）。

    ⚠️ 与完整事件（`events.schemas.ActivityEventOut`）是**两个形状**，
    别搞混 —— 契约 §2.5 说这是它最容易出错的一处。

    投影是**扁平**的：`subject` 取代了 `detail`。完整事件用
    `detail.kind` 做判别式，投影没有判别式 —— 它就一行摘要，不需要载荷。
    """

    id: str
    scanId: str
    agentId: str
    # `recon` / `scan` / `exploit` / `verify` / `report`
    agentPhase: str
    # 投影只有 **6 种**（少了 `reasoning` 与 `failed`）
    type: str
    summary: str
    # 涉及的端点/参数等具体对象，没有则 null
    subject: str | None
    at: IsoDateTime


class OverviewOut(CamelModel):
    """`GET /overview` 的响应（契约 §3.6）。

    对应前端 `data/overview.ts` 的 `OverviewData`。
    """

    metrics: OverviewMetricsOut
    # 最近若干条，投影形状。前端会再截断到 6 条渲染。
    activity: list[ProjectedActivityOut]
    # ⚠️ 固定按严重度顺序返回**全部 5 档**（含 0）。
    severityBreakdown: list[SeverityBreakdownOut]
    # 有没有跑过任何扫描 —— 决定空状态与正常状态。
    # 不能用 `activity` 是否为空来判断：扫描跑完但没产出任何事件，
    # 与"从来没跑过扫描"是两种完全不同的空状态。
    hasAnyScan: bool


__all__ = [
    "OverviewMetricsOut",
    "OverviewOut",
    "ProjectedActivityOut",
    "SeverityBreakdownOut",
]
