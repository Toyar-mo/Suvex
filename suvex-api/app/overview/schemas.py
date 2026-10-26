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

    # ── 「待处置」口径与加权风险分（v0.95）──────────────────
    #
    # ★ 为什么这组字段值得单独存在，而不是让前端拿 `severityBreakdown` 现算：
    #
    # `severityBreakdown` 是**全量**口径（含已修复、含误报），它是"发现过什么"
    # 的记录。而"现在还剩多少要做"是另一个问题 —— 两者在界面上并排时
    # 必须能一眼看出区别。前端现算做不到，因为全量分布里已经丢掉了状态信息。
    #
    # 这条设计直接来自 CyberStrikeAI 踩过的坑（`web/static/js/dashboard.js`
    # 的 `renderSeverityInsights`）：用全量口径算风险分时，把漏洞全修完
    # 之后分布不变、风险分依然居高，而"待处理严重"已经归零 ——
    # 界面上同时出现「风险极高」与「0 个待处理」，这是自相矛盾的。
    #
    # 我们把它放在**后端**算而不是前端：风险分的权重与阈值是**业务规则**
    # （凭什么一个严重等于十个低危），前端各页面各算一份必然漂移。
    # 前端的职责只是把 `riskLevel` 映射成颜色。

    #: 待处置口径下各严重度的条数，**按 `Severity` 顺序返回全部 5 档（含 0）**。
    #:
    #: ⚠️ 与 `severityBreakdown` 的差别只在口径，形状相同。
    #:    保留 0 值：它是 `riskScore` 的输入，缺一个键会让
    #:    "这一级确实没有"与"这里漏算了"混同。
    unresolvedBreakdown: list[SeverityBreakdownOut] = []
    #: 待处置总数（= `unresolvedBreakdown` 各档之和）。
    #:
    #: ★ 它**不**等于 `severityBreakdown` 各档之和，也不等于 Findings 页的
    #:   `total`。三个数字都对，只是回答的问题不同：
    #:   `total` = 筛选范围内几条；`severityBreakdown` 之和 = 发现过几条；
    #:   这个 = 还要处理几条。界面上必须用不同措辞，否则用户会以为其中一个错了。
    unresolvedTotal: int = 0
    #: 加权风险分。权重见 `overview.service.risk_score`。
    #:
    #: ⚠️ **可以为 `0.0`，那是"确实没有待处置项"，不是"算不出来"。**
    #:    与 `avgScanDurationSec` 的 `null` 语义相反 —— 那一项的无数据
    #:    与 0 无法区分（0 秒像是瞬间扫完），而这里 0 分只有一个意思。
    riskScore: float = 0.0
    #: 风险等级：`safe` / `low` / `medium` / `high` / `severe`。
    #:
    #: ★ 服务端给出**等级**而不只给分数，是因为阈值也是业务规则。
    #:   前端若自己定阈值，改一次阈值就要改所有页面 —— 而漏改的那个
    #:   会显示一个颜色错误的徽章，没有任何测试会失败。
    riskLevel: str = "safe"


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


class AttentionItemOut(CamelModel):
    """总览页「该做什么」列表的一项（v0.95，学 CyberStrikeAI 的 recommended actions）。

    ## 为什么这项计算放在后端

    它回答的是"**现在**最该动哪一件"，而这个排序依赖三条业务规则：
    哪些事项算紧急、同类事项怎么折算、一共显示几条。
    这类规则一旦让前端各算一份，总览页与将来的告警中心就会给出
    不同的"最紧急事项" —— 而用户没法判断哪个对，只能都不信。

    ## ★ 有一个字段刻意不存在：`to`（跳转路由）

    `kind` 是稳定的**语义标识**（`unresolved_critical` / `pending_approval` /
    `failing_schedule` / `mcp_down`），前端用它查自己的路由表。
    后端若直接给 `/findings?status=...` 这样的路径，就把前端路由
    写进了 API 契约 —— 改一次路由要发布后端。这与"`navigation.ts`
    是前端的单一事实来源"直接冲突。
    """

    #: 稳定语义标识，前端据此选图标、文案与跳转目标。
    kind: str
    #: `urgent` / `warning` / `info`。**不是**严重度，是"多急"。
    #:
    #: ⚠️ 与 finding 的 `severity` 是两个概念：一条 medium 漏洞如果
    #:    已经挂了 30 天，它比一条刚发现的 critical 更急。混用会让
    #:    界面出现"中危显示成紧急红"这种没法解释的搭配。
    level: str
    #: 一句话说清要做什么（含数量）。由后端生成，因为它带数量。
    title: str
    #: 为什么要做 / 不做的后果。**空串表示没有补充说明**，前端不渲染空段。
    detail: str
    #: 涉及几条。前端用它决定显示成"处理 3 条"还是"处理一批"。
    count: int


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

    # ── v0.95 新增：该做什么（学 CyberStrikeAI 的 recommended actions）──
    #
    # ⚠️ 与 `activity` 的分工，这两块很容易被做重：
    #   `activity` 是"**正在**发生什么" —— 过去时，只增不改，给扫视用；
    #   这个是"**该**做什么" —— 将来时，随处置而消失，给决策用。
    #   一个只在有扫描在跑时才有内容，另一个在任何时候都有意义
    #   （比如"3 个计划连续失败"与扫描无关）。
    #
    # 上限 5 条（与 CyberStrikeAI 一致）：这是"最该动的一件"的列表，
    # 不是待办清单。超过 5 条时人会先关掉页面，而不是从第 6 条开始做。
    attention: list[AttentionItemOut] = []


__all__ = [
    "AttentionItemOut",
    "OverviewMetricsOut",
    "OverviewOut",
    "ProjectedActivityOut",
    "SeverityBreakdownOut",
]
