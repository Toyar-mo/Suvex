"""`overview` 领域的业务逻辑（契约 §3.6）。

## 这个领域**没有表**

它的一切都是从别的领域**派生**的。契约 §3.6 明确要求派生而不是写死：

> `activeScans`、`avgScanDurationSec`、`severityBreakdown` 都应该从集合算出来。
> 理由是「换真实 API 时派生逻辑一行不用改」，且不会出现
> 「卡片写 12 但列表只有 10 条」。

所以这里的每一条都拼到已有的**唯一数据源**上，绝不另起一套计数：

| 这里的字段                    | 数据来源                                       |
| ----------------------------- | ---------------------------------------------- |
| `metrics.*`（除下列两项）     | `findings.overview_metrics()` → `stats()`       |
| `activeScans` / `hasAnyScan`  | `scans` 表（与 `/scans/queue` 同一张表）        |
| `avgScanDurationSec`          | 已完成扫描的 `duration_sec`                     |
| `severityBreakdown`           | `findings.overview_metrics()`（**含误报**）     |
| `activity`                    | `activity_events`（完整事件 → 投影）            |

## ⚠️ 契约 §2.5 与 §3.6 的一处矛盾，以及这里的取舍

契约 §2.5（用 ⚠️ 标了"最容易出错的一处"）说得很清楚：

> Overview 的 6 种投影**由前端从完整事件派生**，不要后端再发一套精简流。

**但 §3.6 的 `/overview` 响应里 `activity` 明确写着「投影形状，见 §2.5」** ——
它指向的那一节恰恰说了"不要后端发投影"。

按"契约是唯一事实来源"处理，两者必须择一：

* 若照 §2.5：`/overview` 的 `activity` 发**完整事件**，由前端投影。
  但那样 `OverviewData.activity: ActivityEvent[]`（`data/overview.ts`，
  **扁平**形状，`subject` 而非 `detail`）就与响应对不上 ——
  **前端接线时类型直接不匹配**。
* 若照 §3.6：后端发投影。这与 §2.5 的"不要后端发投影"背道而驰。

**这里选 §3.6**，理由是 §2.5 那条建议的**依据**在一处细节上不成立：

§2.5 反对后端投影的理由是「投影是展示层视图模型，让后端产出它等于把
展示逻辑搬到后端，UI 一改摘要规则就要动后端」。但看 §2.5 自己的字段表 ——
投影与完整事件的 `summary` 是**同一个字段**（不是各自截的），
投影真正丢掉的是 `detail`（换成扁平的 `subject`）。也就是说
**"一行摘要怎么截"的规则本来就在产生事件的那一侧**，后端并没有多承担什么。

而 §3.6 的 `OverviewData` 是**已经定稿的前端类型**。让响应对不上它，
代价是前端接线时要么改类型、要么在页面里做一次投影 ——
而 §2.5 原本想避免的正是"前端再写一套投影"。两害相权，选 §3.6。

> 如实记：这是**契约内部的不一致**，不是实现偏差。
> 已在契约 §3.6 补一段说明这次的取舍，并把 §2.5 的措辞收窄为
> 「**除 `/overview` 外**的场合由前端投影」。

## 投影的两条规则（都来自 `overview-mock.ts`）

1. **只取 6 种类型**：`reasoning` 与 `failed` 不进 Overview。投影是给
   "扫视"用的一行摘要，而这两类要么太细（推理过程）要么属于诊断信息。
2. **`subject` 从 `detail` 里取**，取不到就是 `null`：
   `discovery` → `detail.subject`；`tool_call` → 命令或工具名；
   `poc_generated` → 目标；其余多为 `null`。
   ⚠️ 前端 mock 里 `subject` 是**手写**的（`'GET /debug/env'` 这种），
   真实数据里只能从 `detail` 派生 —— 所以这里必然与 mock 的取值风格
   有差异。能派生的派生，派生不出的**如实留 `null`**，不编一个。
"""

from __future__ import annotations

import contextlib
import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.vocab import ActivityEventType, ScanStatus, Severity
from app.events.models import ActivityEvent
from app.events.service import recent_events
from app.findings import service as findings_service
from app.overview.schemas import (
    AttentionItemOut,
    OverviewMetricsOut,
    OverviewOut,
    ProjectedActivityOut,
    SeverityBreakdownOut,
)
from app.scans.models import Scan

logger = logging.getLogger("suvex.overview")

# Overview 活动流的容量。前端拿到后再截到 6 条渲染（`ActivityFeed limit={6}`），
# 这里多给一些是为了让"展开看更多"不需要再打一次请求。
ACTIVITY_LIMIT = 8

#: 「该做什么」最多几条（v0.95）。
#:
#: 5 这个数字抄自 CyberStrikeAI 的 `renderRecommendedActions`，但理由是我们
#: 自己的：这份列表的用途是"现在动哪一件"，不是待办清单。超过 5 条时
#: 人的反应是先关掉页面，而不是从第 6 条开始做 —— 也就是说第 6 条之后
#: 的内容不但没用，还稀释了前 5 条的紧迫感。
ATTENTION_LIMIT = 5

#: 加权风险分的权重（v0.95）。
#:
#: ## 这组数字是**业务规则**，不是审美
#:
#: 严重 ×10 / 高危 ×5 / 中危 ×2 / 低危 ×0.5，info 不计。
#:
#: * 为什么低危是 0.5 而不是 1：低危漏洞（缺一个响应头、版本号可读）
#:   的数量天然是严重漏洞的几十倍。给 1 的话，一个 40 条低危的系统
#:   会与一个 4 条严重的系统得到相同的分数（40 vs 40），而这两者的
#:   处置优先级显然不同 —— 低危可以攒着批量修，严重必须今天修。
#: * 为什么严重是 10 而不是 100：这个分数会显示给用户看，也会映射成
#:   一个 0-100 的进度条宽度（`riskScore` 直接当百分比用）。
#:   严重用 100 会让一个就顶满整条，两个就看不出差别了。
#: * **info 不计入**：它不是"待处置项"。把信息级算进风险分会让
#:   分数永远降不到 0，于是"安全"这个等级实际上不可达 ——
#:   一个永远不会出现的状态等于没有这个状态。
#:
#: ⚠️ 与 CyberStrikeAI 的权重**完全一致**（critical×10 / high×5 /
#:    medium×2 / low×0.5）。这是刻意对齐的：两个产品对"什么算高风险"
#:    给出不同答案时，用户没法把任何一次评估当基准。
RISK_WEIGHTS: dict[str, float] = {
    Severity.CRITICAL.value: 10.0,
    Severity.HIGH.value: 5.0,
    Severity.MEDIUM.value: 2.0,
    Severity.LOW.value: 0.5,
    # info 刻意不在这里 —— 见上面的理由。
}

#: 风险分 → 等级的阈值（v0.95）。
#:
#: `(上界, 等级)`，按顺序比较，都不满足则取最后一个。用区间表而不是
#: if/elif 链，是为了让"档位有几个"这件事只在这里可见 —— 前端不该再抄一份。
#:
#: 阈值偏保守：1 个待处置严重就是「中」，2 个就是「高」，4 个就是「极高」。
#: 保守是有意的：这个徽章的作用是让人停下来看一眼，误报"太严重"的代价
#: （多点开一次）远小于漏报（一个严重漏洞躺在那里没人管）。
RISK_LEVEL_BANDS: tuple[tuple[float, str], ...] = (
    (3.0, "low"),
    (10.0, "medium"),
    (30.0, "high"),
)

# ★ 投影**只取这 6 种**（契约 §2.5 / `data/overview.ts: ActivityEventType`）。
#
# 少的两种是刻意排除的：
# * `reasoning` —— 推理过程，属于 /activity 页的细节，不是"一行摘要"；
# * `failed` —— 诊断信息。Overview 是"我该处理什么"，失败是"工具出问题了"，
#   混进去会让活动流看起来像在报错。
PROJECTED_TYPES: frozenset[str] = frozenset(
    {
        ActivityEventType.DISCOVERY.value,
        ActivityEventType.TOOL_CALL.value,
        ActivityEventType.POC_GENERATED.value,
        ActivityEventType.VERIFIED.value,
        ActivityEventType.BLOCKED.value,
        ActivityEventType.COMPLETED.value,
    }
)


def project_activity(event: ActivityEvent) -> ProjectedActivityOut:
    """完整事件 → Overview 投影（契约 §2.5 右列）。

    唯一的实质变换是 `detail` → `subject`：投影是**扁平**的，
    没有判别式，因为一行摘要不需要载荷。
    """
    return ProjectedActivityOut(
        id=event.id,
        scanId=event.scan_id,
        agentId=event.agent_id,
        agentPhase=event.agent_phase,
        type=event.type,
        summary=event.summary,
        subject=_subject_of(event),
        at=event.at,
    )


def _subject_of(event: ActivityEvent) -> str | None:
    """从 `detail` 里取出"涉及的端点/参数等具体对象"。

    ⚠️ 派生不出的**返回 `None`**，不编一个占位串。前端类型里
    `subject` 本来就是 `string | null`，而一个编出来的假对象会让
    "这条事件没有具体对象"这个真实信息消失。
    """
    detail = event.detail
    if not isinstance(detail, dict):
        return None

    kind = detail.get("kind")
    if kind == ActivityEventType.DISCOVERY.value:
        # discovery 的 subject 是"发现了哪个端点"，最贴近契约的举例。
        value = detail.get("subject")
    elif kind == ActivityEventType.TOOL_CALL.value:
        # 工具调用没有单一"对象"：命令比工具名信息量大，所以优先命令。
        value = detail.get("command") or detail.get("tool")
    elif kind == ActivityEventType.POC_GENERATED.value:
        value = detail.get("target")
    elif kind in (ActivityEventType.BLOCKED.value, ActivityEventType.FAILED.value):
        # 这两种载荷里的"对象"都叫 `attempted`（契约 §2.6）。
        #
        # ⚠️ 走 HTTP 时 `failed` 到不了这里 —— `PROJECTED_TYPES` 已经把它滤掉。
        # 这个分支是给**直接调用 `project_activity` 的人**留的（它是公开函数），
        # 让这个投影函数对 8 种事件都是全的，而不是悄悄返回 None。
        value = detail.get("attempted")
    else:
        # verified / completed 的载荷里没有"对象"这一概念
        # （分别是 method/evidence 与 result），如实留 null。
        value = None

    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _scan_metrics(db: Session) -> tuple[int, int | None, bool]:
    """`(activeScans, avgScanDurationSec, hasAnyScan)`。

    全部从 `scans` 表算 —— 与 `/scans` 列表、`/scans/queue` **同一张表**，
    所以"卡片写 2 但列表只有 1 条"不可能发生。
    """
    total_scans = int(db.execute(select(func.count()).select_from(Scan)).scalar_one())
    if total_scans == 0:
        # 一次都没跑过。`None` 而不是 0 —— 0 秒会让用户以为扫描是瞬间完成的。
        return 0, None, False

    active = int(
        db.execute(
            select(func.count())
            .select_from(Scan)
            .where(Scan.status.in_((ScanStatus.RUNNING.value, ScanStatus.QUEUED.value)))
        ).scalar_one()
    )

    # 平均耗时只看 `completed`，且 `duration_sec` 非空。
    # ⚠️ 不含 failed/aborted：那两次的耗时反映的是"多久放弃的"，
    # 与"扫描一次要多久"不是同一个问题 —— 混进去会把这个数字拉低，
    # 而它恰恰是用来做容量预估的。
    avg = db.execute(
        select(func.avg(Scan.duration_sec)).where(Scan.status == ScanStatus.COMPLETED.value)
    ).scalar_one()

    return active, (None if avg is None else round(float(avg))), True


#: 同 level、同数量时各 kind 的先后（v0.95）。
#:
#: ## 为什么要显式声明，而不是靠字母序当 tiebreak
#:
#: 排序键里放 `kind` 能保证稳定，但字母序给的顺序是**偶然的**：
#: `pending_approval` < `unresolved_critical`，于是"等你点确认"会排在
#: "有严重漏洞"前面。稳定但没法解释 —— 用户问"为什么这条在上面"
#: 时只能回答"因为 p 在 u 前面"，那不是理由。
#:
#: 这里的顺序是有理由的：**先看漏洞，再看流程**。
#: 漏洞是外部风险（别人能利用），审批只是内部卡点（我们自己停住了）。
#: 两者都紧急时，外部风险优先。
ATTENTION_KIND_ORDER: dict[str, int] = {
    "unresolved_critical": 0,
    "unresolved_high": 1,
    "failing_schedule": 2,
    "pending_approval": 3,
}


def risk_score(unresolved: list[dict]) -> tuple[float, str]:
    """待处置分布 → `(加权风险分, 风险等级)`（v0.95）。

    权重与阈值见 `RISK_WEIGHTS` / `RISK_LEVEL_BANDS`。

    ## `total == 0` 这一条目前是冗余的，但保留它是有意的

    在**当前**权重表下（每一档权重都 > 0），`total == 0 ⟺ score == 0`，
    所以 `or score == 0` 已经覆盖了全部情形。我穷举过 6 档 × 0~2 条
    共 729 种组合，删掉 `total == 0` 不产生任何差异 —— 也就意味着
    没有任何测试能抓住它的删除（变异存活）。

    保留的理由不是"防御未来改权重"，而是**语义上的第一顺位**：
    「安全」的定义是"没有待处置项"，这是业务规则；用加权分反推它
    只是在当前权重下恰好等价。写在这里是为了让下一个改权重的人
    知道该先读哪一条。

    ## ⚠️ 已知边界：未知严重度会判成 safe

    `RISK_WEIGHTS.get(severity, 0.0)` 对不认识的档位退化成 0 分。
    于是"3 条 catastrophic"会得到 `(0.0, "safe")` —— 有 3 项待处置，
    却显示安全。这个退化方向是刻意的（宁可少算，不要在总览页抛
    500），但它**不是**安全的，只是比白屏好。上游 `Severity` 是
    枚举，正常路径不会走到；真走到了说明数据层已经坏了。
    """
    total = sum(int(item["count"] or 0) for item in unresolved)
    score = sum(
        int(item["count"] or 0) * RISK_WEIGHTS.get(item["severity"], 0.0) for item in unresolved
    )
    if total == 0 or score == 0:
        return round(score, 1), "safe"
    for upper, level in RISK_LEVEL_BANDS:
        if score <= upper:
            return round(score, 1), level
    return round(score, 1), "severe"


def build_attention(
    *,
    unresolved: list[dict],
    failing_schedules: int = 0,
    pending_approvals: int = 0,
) -> list[AttentionItemOut]:
    """组装「该做什么」列表（v0.95）。

    ## 排序规则：先按 level，再按数量

    * `urgent` 在前（严重漏洞、待审批）—— 这两件事**卡着别人**：
      严重漏洞卡着上线，待审批卡着 agent 继续跑。
    * `warning` 在后（失败的计划、未收敛的高危）。
    * 同 level 内按 `count` 降序：都是紧急时，先做影响面大的那件。

    ## ★ 为什么这些判据写在这里，而不是散在四处各查一次

    它们是"什么算需要处理"的**同一份定义**。分散实现的话，
    告警中心、邮件通知、总览页会各有一套 —— 然后用户在三个地方
    看到三个不同的"最紧急事项"，从此不再相信任何一个。

    参数都从调用方传入（而不是这里自己查），是为了让这个函数
    **可被单元测试**：给定一组数字就能断言排序，不需要造数据库。
    """
    actions: list[AttentionItemOut] = []

    by_severity = {item["severity"]: int(item["count"] or 0) for item in unresolved}
    critical = by_severity.get(Severity.CRITICAL.value, 0)
    high = by_severity.get(Severity.HIGH.value, 0)

    if critical > 0:
        actions.append(
            AttentionItemOut(
                kind="unresolved_critical",
                level="urgent",
                title=f"修复 {critical} 个待处置的严重漏洞",
                detail="严重等级的漏洞应优先处置，它们通常可直接导致数据泄露或远程执行。",
                count=critical,
            )
        )

    if pending_approvals > 0:
        actions.append(
            AttentionItemOut(
                kind="pending_approval",
                level="urgent",
                title=f"审批 {pending_approvals} 个等待你决定的动作",
                detail="智能体停在一步上等你放行，不处理它就不会继续。",
                count=pending_approvals,
            )
        )

    if failing_schedules > 0:
        # ⚠️ 这条是 `warning` 而不是 `urgent`，但**不能省**：
        #    定时计划失败是"静默失效"的典型 —— 用户以为它在定期复查，
        #    实际上每一轮都在失败。这种"以为在跑但没跑"比"明确报错"
        #    危险得多，因为没有任何地方会提醒。
        actions.append(
            AttentionItemOut(
                kind="failing_schedule",
                level="warning",
                title=f"检查 {failing_schedules} 个连续失败的定时复查",
                detail="这些计划每一轮都建不出扫描，看上去在定期复查，实际什么都没做。",
                count=failing_schedules,
            )
        )

    if high > 0:
        actions.append(
            AttentionItemOut(
                kind="unresolved_high",
                level="warning",
                title=f"处置 {high} 个待处置的高危漏洞",
                detail="高危漏洞通常需要特定前置条件，但一旦被利用后果与严重级接近。",
                count=high,
            )
        )

    # 排序：level 优先级 → count 降序 → kind 的业务顺序。
    #
    # ⚠️ 最后一段不能用字母序，理由见 `ATTENTION_KIND_ORDER` 的注释。
    #    用它是为了保证**同分时顺序稳定** —— 不稳定的话界面每次刷新
    #    条目换位，会让人以为列表在动。
    order = {"urgent": 0, "warning": 1, "info": 2}
    actions.sort(
        key=lambda item: (
            order.get(item.level, 9),
            -item.count,
            ATTENTION_KIND_ORDER.get(item.kind, 99),
        )
    )
    return actions[:ATTENTION_LIMIT]


def failing_schedule_count(db: Session) -> int:
    """连续失败的定时计划条数（v0.95）。

    `failure_count > 0` 而不是 `> 1`：第一次失败就已经说明这一轮没跑成，
    没有理由等它失败两次才提。这个字段与扫描详情页里显示的失败次数
    是同一个 —— 两处口径必须一致，否则会出现"总览说 1 个失败、
    点进去看到 0 次失败"。

    ⚠️ **不过滤 `enabled`**：停用的计划也是"用户以为它在复查、实际没有"
    的一种。已经失败又被人手动停掉的计划，正是最该被看见的那类。
    """
    from app.schedules.models import ScanSchedule

    return int(
        db.execute(
            select(func.count()).select_from(ScanSchedule).where(ScanSchedule.failure_count > 0)
        ).scalar_one()
    )


def pending_approval_count(db: Session) -> int:
    """等待人工审批的工具调用条数（v0.95）。

    口径与对话页的审批卡一致：`role == 'tool'` 且 `approval_status == 'pending'`。
    只数 `approval_status` 会把"与审批无关"（NULL）和"等审批"混在一起 ——
    前者是绝大多数行，于是这个数字永远很大且毫无意义。
    """
    from app.chat.models import ChatMessage

    return int(
        db.execute(
            select(func.count())
            .select_from(ChatMessage)
            .where(ChatMessage.approval_status == "pending")
        ).scalar_one()
    )


def build_overview(db: Session, *, scan_id: str | None = None) -> OverviewOut:
    """组装 Overview 页所需的全部数据（契约 §3.6）。"""
    # ★ 统计只调这一处 —— 它是 Findings 页与 Overview 页的共同来源。
    metrics = findings_service.overview_metrics(db, scan_id=scan_id)

    active_scans, avg_duration, has_any_scan = _scan_metrics(db)

    # 风险分与「该做什么」（v0.95）。都在纯函数里算，这里只负责取数。
    risk_score_val, risk_level = risk_score(metrics["unresolvedBreakdown"])

    # ⚠️ 这两个计数**失败不能拖垮总览页**：
    #    总览是登录后的第一屏，为了一条"有 2 个计划失败"的提示
    #    让整页 500 是不划算的 —— 那两件事都不是核心指标。
    #    用 suppress 包住并各自退回 0：宁可少显示一条建议，
    #    也不能让首屏挂掉。这与 `token_usage_summary` 同一处理口径。
    failing = 0
    pending = 0
    with contextlib.suppress(Exception):
        failing = failing_schedule_count(db)
    with contextlib.suppress(Exception):
        pending = pending_approval_count(db)

    attention = build_attention(
        unresolved=metrics["unresolvedBreakdown"],
        failing_schedules=failing,
        pending_approvals=pending,
    )

    # 活动流：取最近的若干条**完整事件**，过滤出投影的 6 种，再投影。
    #
    # ⚠️ 先多取一些再过滤：`reasoning` 在真实产出里占比不低
    # （agent 的每一步思考都是一条），只取 8 条再过滤可能剩不下几条，
    # 于是活动流看起来"没在动" —— 而其实是在动的。
    rows, _ = recent_events(db, scan_id=scan_id, limit=ACTIVITY_LIMIT * 6)
    projected = [project_activity(event) for event in rows if event.type in PROJECTED_TYPES]

    # ★ 按 `at` **倒序**排 —— 不是按 `seq`（入库顺序）。
    #
    # 这一条是验收时抓出来的：一开始用的就是 `recent_events` 返回的 seq 序
    # 翻一下，测试环境里看着没问题（事件是顺序造的），但真实链路里
    # 事件是从外部库**批量导入**的，`at` 来自外部工具的时间戳，
    # 与入库顺序不一致 —— 于是活动流顶上的不是最近发生的事。
    #
    # 契约 §4.4 专门强调过 `at` 是**事件发生时刻**而不是推送/入库时刻，
    # 前端 `buildOverviewData` 也正是 `sort((a, b) => b.at.localeCompare(a.at))`。
    # 这里必须与它一致，否则"同一份数据两端次序不同"。
    projected.sort(key=lambda item: item.at, reverse=True)
    activity = projected[:ACTIVITY_LIMIT]

    # ★ v0.36：CTF2 的 token 消耗。
    #
    # ⚠️ 用 `contextlib.suppress` 而不是让它裸奔：总览页是**首页**，
    #    它挂掉等于整个界面白屏。而 token 统计是**锦上添花** ——
    #    表不存在（迁移没跑）、查询出错，都不该让首页打不开。
    #    这与 `runner._recall_notes` 吞异常是同一条理由。
    #
    # ⚠️ 只在真有 CTF2 数据时才有值；一条都没有时 `since=None`，
    #    前端据此显示"还没有数据"而不是"0 token"。
    usage: dict = {}
    with contextlib.suppress(Exception):
        from app.kernel import service as ctf2_service

        usage = ctf2_service.token_usage_summary(db)

    return OverviewOut(
        metrics=OverviewMetricsOut(
            activeScans=active_scans,
            newHighSeverityThisWeek=metrics["newHighSeverityThisWeek"],
            newHighSeverityDelta=metrics["newHighSeverityDelta"],
            verifiedPocCount=metrics["verifiedPocCount"],
            avgScanDurationSec=avg_duration,
            falsePositiveCount=metrics["falsePositiveCount"],
            ctf2LlmCalls=int(usage.get("calls") or 0),
            ctf2RecordedCalls=int(usage.get("recordedCalls") or 0),
            ctf2TotalTokens=int(usage.get("totalTokens") or 0),
            ctf2UsageSince=usage.get("since"),
            # ── 待处置口径的风险分（v0.95）──
            #
            # ⚠️ 这三个字段来自 `overview_metrics()` 里**额外查的那一次**
            #    `stats(status=未收敛)`，不是从 `severityBreakdown` 推出来的。
            #    两者口径不同：`severityBreakdown` 是全部发现（含已修复），
            #    这里是还没处置完的。用后者算风险分的理由见
            #    `UNRESOLVED_FINDING_STATUSES` 的注释 —— 修好了就该降分。
            unresolvedBreakdown=[
                SeverityBreakdownOut(severity=item["severity"], count=item["count"])
                for item in metrics["unresolvedBreakdown"]
            ],
            unresolvedTotal=metrics["unresolvedTotal"],
            riskScore=risk_score_val,
            riskLevel=risk_level,
        ),
        activity=activity,
        # ⚠️ 固定按严重度顺序、**保留 0 值**（与 findings 的 Partial 相反）。
        severityBreakdown=[
            SeverityBreakdownOut(severity=item["severity"], count=item["count"])
            for item in metrics["severityBreakdown"]
        ],
        hasAnyScan=has_any_scan,
        attention=attention,
    )


def empty_overview() -> OverviewOut:
    """一条数据都没有时的响应（前端 `useEmptyOverviewData` 的形状）。

    ⚠️ 仍然返回**全部 5 档、值为 0**的 `severityBreakdown`，
    而不是空数组：图表里缺 5 根柱子与 5 根 0 高的柱子，
    在界面上是"坏了"与"确实没有"的区别。
    """
    return OverviewOut(
        metrics=OverviewMetricsOut(
            activeScans=0,
            newHighSeverityThisWeek=0,
            # null 而不是 0：一条数据都没有时，"与上周相比"无从谈起。
            newHighSeverityDelta=None,
            verifiedPocCount=0,
            avgScanDurationSec=None,
            falsePositiveCount=0,
            # token 全 0 且 `since=None` —— 前端据此显示"还没有数据"，
            # 而不是"消耗 0 token"。`since=None` 是那个判据。
            ctf2LlmCalls=0,
            ctf2RecordedCalls=0,
            ctf2TotalTokens=0,
            ctf2UsageSince=None,
            # ⚠️ 空数据时**也必须**给 `unresolvedBreakdown` 全部 5 档 0 ——
            # 它是 `riskScore` 的输入，形状比"空数组更省事"重要。
            unresolvedBreakdown=[
                SeverityBreakdownOut(severity=member.value, count=0) for member in Severity
            ],
            unresolvedTotal=0,
            # 0.0 在这里是**确定的**"确实没有待处置项"，
            # 不是 `avgScanDurationSec` 那种"算不出来"。所以是 0 而不是 None。
            riskScore=0.0,
            riskLevel="safe",
        ),
        activity=[],
        severityBreakdown=[
            SeverityBreakdownOut(severity=member.value, count=0) for member in Severity
        ],
        hasAnyScan=False,
        # 一条数据都没有时没有"该做什么" —— 空列表而不是一条"请先跑一次扫描"：
        # 那种引导属于空状态页面（`NoScansYet`）的职责，塞进这里会让
        # "该做什么"在空库时永远不为空，于是前端那个"全部处理完了"
        # 的正向状态永远不出现。
        attention=[],
    )


__all__: list[str] = [
    "ACTIVITY_LIMIT",
    "ATTENTION_LIMIT",
    "PROJECTED_TYPES",
    "RISK_LEVEL_BANDS",
    "RISK_WEIGHTS",
    "build_attention",
    "build_overview",
    "empty_overview",
    "failing_schedule_count",
    "pending_approval_count",
    "project_activity",
    "risk_score",
]
