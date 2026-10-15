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


def build_overview(db: Session, *, scan_id: str | None = None) -> OverviewOut:
    """组装 Overview 页所需的全部数据（契约 §3.6）。"""
    # ★ 统计只调这一处 —— 它是 Findings 页与 Overview 页的共同来源。
    metrics = findings_service.overview_metrics(db, scan_id=scan_id)

    active_scans, avg_duration, has_any_scan = _scan_metrics(db)

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
    #    这与 `runner._recall_notes_text` 吞异常是同一条理由。
    #
    # ⚠️ 只在真有 CTF2 数据时才有值；一条都没有时 `since=None`，
    #    前端据此显示"还没有数据"而不是"0 token"。
    usage: dict = {}
    with contextlib.suppress(Exception):
        from app.ctf2 import service as ctf2_service

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
        ),
        activity=activity,
        # ⚠️ 固定按严重度顺序、**保留 0 值**（与 findings 的 Partial 相反）。
        severityBreakdown=[
            SeverityBreakdownOut(severity=item["severity"], count=item["count"])
            for item in metrics["severityBreakdown"]
        ],
        hasAnyScan=has_any_scan,
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
        ),
        activity=[],
        severityBreakdown=[
            SeverityBreakdownOut(severity=member.value, count=0) for member in Severity
        ],
        hasAnyScan=False,
    )


__all__: list[str] = [
    "ACTIVITY_LIMIT",
    "PROJECTED_TYPES",
    "build_overview",
    "empty_overview",
    "project_activity",
]
