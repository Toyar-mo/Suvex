"""`scan_schedules` 领域（定时扫描，v0.85）。

## 它解决的是什么问题

资产视图（v0.85）会告诉你「这个目标上一次拿到结论是 40 天前」，
但**看见了不代表会去扫**。人不会记得每周回来点一次「发起扫描」。
定期扫描就是把这个动作自动化。

## ★ 为什么是「计划」而不是「定时任务」

很多同类产品的定时扫描存的是 **cron 表达式**。这里故意不存：

* cron 的 `0 3 * * *` 对用户来说要查文档才能写对，而这一页的读者
  是安全工程师，不是运维 —— 他们想表达的是「每周一次」「每天凌晨」；
* cron 表达式的**下一次到点时刻**取决于表达式解析器，而
  「每 15 分钟」与「每小时的第 0/15/30/45 分」在夏令时切换那天
  会给出不同答案。存一个 `interval_minutes` 整数，这个歧义就不存在。

所以这里存的是「间隔 + 基准时刻」，下一次到点 = 基准 + 整数倍间隔。
代价如实记：**没法表达「每月 1 号」这种日历语义**。真需要时再加一列
`cron`，而不是现在就把所有人都拖进 cron 的复杂度里。

## ★ 为什么 `next_run_at` 是**存储列**而不是算出来的

它完全可以由 `(interval_minutes, next_run_at 的初值, 已跑次数)` 推出来，
但那要求**每次 tick 都把全部计划取回来逐个算**。存在列里之后，
调度器只需要 `WHERE next_run_at <= now()` 一条带索引的查询。

⚠️ 代价是必须有人负责推进它。推进点只有一个：
`service.claim_due()` —— 认领到期的计划时立刻写下一次。
放在别处（比如扫描跑完时）会出现「扫描失败就再也不扫了」，而
「上次失败了所以下次不扫」恰恰是最不该发生的事。

## ★ 为什么计划里存的是**建扫描的参数快照**，不是对某个模板的引用

定时扫描必须能**在没有人参与的情况下**建出一条合法扫描 —— 而
`POST /scans` 要求授权声明（契约 §5.4，法律留痕）。所以授权那一组
字段必须在建计划时一次性确认并存在这里，每次到点复制进新的 scan 行。

如果改成"引用一个已存在的 scan 模板"，那么：
* 那条 scan 被删/归档后，计划就静默失效了（而界面还显示"已启用"）；
* 模板被重新扫描改写了 brief 之类的字段，计划的行为会跟着变 ——
  用户改一条历史记录，却改变了未来的定时行为，这不可能解释得清。

快照的代价是「改 agent 的 provider 要重建计划」，如实记。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.columns import JsonColumn, UtcDateTime
from app.core.db import Base
from app.core.timeutil import now_utc
from app.core.vocab import ScanGoal, TargetKind, values

#: 允许的最小间隔。5 分钟是「人还看得过来」的下限 —— 再密就是
#: 持续攻击了，而这一页的语义是"定期复查"，不是"持续压测"。
MIN_INTERVAL_MINUTES = 5

#: 允许的最大间隔。365 天（一年）是"起码还叫定期"的上限。
MAX_INTERVAL_MINUTES = 365 * 24 * 60


class ScanSchedule(Base):
    """一条定时扫描计划。"""

    __tablename__ = "scan_schedules"
    __table_args__ = (
        CheckConstraint(
            f"target_kind IN ({', '.join(repr(v) for v in values(TargetKind))})",
            name="schedule_target_kind_valid",
        ),
        CheckConstraint(
            f"goal IN ({', '.join(repr(v) for v in values(ScanGoal))})",
            name="schedule_goal_valid",
        ),
        # 间隔的上下界放在数据库：它是「这条记录不可能长成什么样」的定义，
        # 而不是某个 handler 的输入校验。直接从 SQL 插一条 0 分钟的计划
        # 会让调度器在一个 tick 里反复建扫描。
        CheckConstraint(
            f"interval_minutes >= {MIN_INTERVAL_MINUTES} AND interval_minutes <= {MAX_INTERVAL_MINUTES}",
            name="schedule_interval_range",
        ),
        # ★ 停用的计划**不应有**下一次到点时刻 —— 这条不变量让
        # 「界面显示已停用、但调度器还在扫」这种状态从数据层就不可能出现。
        CheckConstraint(
            "(enabled = false AND next_run_at IS NOT NULL) = false",
            name="schedule_disabled_has_no_next_run",
        ),
        # 调度器的唯一热查询：`WHERE enabled AND next_run_at <= now()`
        Index("ix_scan_schedules_due", "enabled", "next_run_at"),
        Index("ix_scan_schedules_target", "target"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    name: Mapped[str] = mapped_column(String(200), nullable=False)

    #: ⚠️ 归一化后的目标（与 `scans.target` 同一口径，契约 §2.8）。
    #: 不归一化的话，`http://a.com` 与 `a.com` 会变成两条计划，
    #: 而它们扫的是同一个服务 —— 于是「每周一次」变成「每周两次」。
    target: Mapped[str] = mapped_column(String(512), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    goal: Mapped[str] = mapped_column(String(32), nullable=False, default=ScanGoal.FIND_VULNS.value)

    #: 间隔分钟数。见模块 docstring 里「为什么不是 cron」。
    interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False)

    #: 停用的计划不参与调度。用布尔而不是把行删掉：用户"先停一周"
    #: 之后还要用，而每次重输一遍参数是最容易输错的一个交互。
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    #: 下一次该跑的时刻（调度器只认这一列）。
    #: ⚠️ 停用时必须为 NULL，见 `schedule_disabled_has_no_next_run`。
    next_run_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    #: 上一次由这条计划建出来的扫描 id。给界面显示"最近一次"用。
    #: 不做外键：扫描会被清理/归档，而计划不该因为扫描被删就失去历史 ——
    #: 它只是"最近一次是谁"，不是一条引用完整性。
    last_scan_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 上一次建扫描的时刻。与 `last_scan_id` 分开是因为合并成一条会
    #: 出现「扫描已被物理清理 → 两个都变 NULL → 看起来从没跑过」。
    last_run_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    #: 连续建失败了几次（建扫描本身失败，比如 provider 被删了）。
    #: ⚠️ **不因为它归零**：这条计划只是"上次建不出扫描"，
    #: 不自动停用 —— 静默停用一个用户以为在跑的计划是最坏的形态。
    #: 界面上要把这个数显示出来并给出原因。
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: 建扫描时用的 provider（快照当前值，给界面看；实际用的是
    #: `provider_id`）。provider 被删时这里会指向一个不存在的 id ——
    #: 与 `Scan.provider_id` 同一口径（历史引用，不做外键）。
    provider_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    skills: Mapped[list[str]] = mapped_column(JsonColumn, nullable=False, default=list)
    brief: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── 授权留痕的**快照**（契约 §5.4）───────────────────────
    #
    # 见模块 docstring：定时扫描要在无人参与时建扫描，而建扫描要求
    # 授权声明。所以用户在**建计划**那一刻确认一次，之后每一轮都由
    # 服务复制这几列进新的 scan 行。
    #
    # ⚠️ 这不是拿法律要求打折扣：声明内容与「每次新建扫描」完全一致，
    #   只是确认的**动作**从"每次"变成"建计划时 + 建计划的人"。留痕里
    #   `authorized_by` 记的仍是那个点下确认的人，`scheduled_from` 记
    #   这条授权来自哪条计划 —— 事后能完整回答"这句话是谁同意的"。
    authorized_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    authorization_statement: Mapped[str | None] = mapped_column(Text, nullable=True)
    authorization_confirmed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=now_utc, onupdate=now_utc
    )

    @property
    def is_due(self) -> bool:  # pragma: no cover - 由 SQL 判定，这里只做可读性
        return self.enabled and self.next_run_at is not None

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"<ScanSchedule id={self.id!r} target={self.target!r} "
            f"every={self.interval_minutes}m enabled={self.enabled}>"
        )


__all__ = ["MAX_INTERVAL_MINUTES", "MIN_INTERVAL_MINUTES", "ScanSchedule"]
