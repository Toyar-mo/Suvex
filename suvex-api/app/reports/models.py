"""`reports` 领域（报告的冻结快照）。

⚠️ **本域的核心是「冻结」两个字。** 报告一旦生成，它记录的
`findingIds` 顺序、`findingCount`、`excludedCount`、`severityCounts`
都不再随数据变化。原因（`data/report.ts` 与 `report-document.ts` 都记过）：

> 复现历史报告时快照里只有**被收录**的 id，重算 `inScope - selected` 恒为 0，
> 于是一份「只含已确认」、恰好把 2 条待确认全筛掉的报告会写着
> 「本次范围内未发现问题」—— **那不是没问题，那是没看。**

所以 `excluded_count` 必须在生成那一刻算好并写进表里，不能由前端重算。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.columns import JsonColumn, UtcDateTime
from app.core.db import Base
from app.core.timeutil import now_utc
from app.core.vocab import ReportFormat, ReportScopeKind, values


class Report(Base):
    """一份已生成的报告。

    ## `scope` 在表里摊平，但用 CHECK 约束守住 union 语义

    契约 §2.7 用 ⚠️ 标了：「`scope` 是 union，不是三个可选字段」——
    否则会出现 `scanId` 和 `target` 同时有值的非法状态。

    表结构上摊平（三组列）是必要的：只有一个字段有值的联合类型在 SQL 里
    就是「按 kind 选一组列」。但**摊平不等于放弃约束** ——
    下面那条 CHECK 把「恰好一组有值」写进数据库。
    只靠 service 层判断的话，一次手工 SQL、一次迁移脚本、一个忘了走 service
    的后台任务都能造出非法行，而它直到前端渲染时才炸。
    """

    __tablename__ = "reports"
    __table_args__ = (
        CheckConstraint(
            f"scope_kind IN ({', '.join(repr(v) for v in values(ReportScopeKind))})",
            name="scope_kind_valid",
        ),
        CheckConstraint(
            f"format IN ({', '.join(repr(v) for v in values(ReportFormat))})", name="format_valid"
        ),
        CheckConstraint(
            """
            (scope_kind = 'scan'
                AND scope_scan_id IS NOT NULL AND scope_target IS NULL
                AND scope_from IS NULL AND scope_to IS NULL)
            OR (scope_kind = 'target'
                AND scope_target IS NOT NULL AND scope_scan_id IS NULL
                AND scope_from IS NULL AND scope_to IS NULL)
            OR (scope_kind = 'range'
                AND scope_from IS NOT NULL AND scope_to IS NOT NULL
                AND scope_scan_id IS NULL AND scope_target IS NULL)
            -- ★ 手选漏洞（任务 10 P5）：四个 scope 列**全为 NULL**。
            --   成员是**一串 id**，塞不进任何一列 —— 它们存在
            --   `report_findings` 关联表里，而那本来就是"这份报告
            --   冻结了哪些漏洞"的既有一处事实来源。
            OR (scope_kind = 'findings'
                AND scope_scan_id IS NULL AND scope_target IS NULL
                AND scope_from IS NULL AND scope_to IS NULL)
            """,
            name="scope_exactly_one_shape",
        ),
        Index("ix_reports_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # `RPT-YYYYMMDD-XXXX`。由**生成动作**分配，之后跟着报告行走，不再重算
    # （契约 §2.7：草稿阶段显示一个编号、下载得到另一个，客户对账会对不上）。
    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)

    # ── scope（union，摊平）────────────────────────────────
    scope_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_scan_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scope_target: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # ⚠️ 闭区间（契约 §2.7）。
    scope_from: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    scope_to: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    # ── options（4 个布尔）──────────────────────────────────
    # 展开成列而不是一个 JSON：它们要参与「excludedCount 是怎么算出来的」
    # 这件事的可解释性，而 JSON 里的键名拼错不会报错，只会静默用默认值。
    opt_include_poc: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    opt_include_reasoning: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    opt_only_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    opt_include_remediation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    format: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=now_utc)

    # ── 冻结的统计（生成时算好，永不重算）──────────────────
    finding_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # ★ 契约 §2.7 用 ⚠️ 标了「必须由后端在生成时算好并冻结」。
    excluded_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    severity_counts: Mapped[dict[str, int]] = mapped_column(JsonColumn, nullable=False, default=dict)

    entries: Mapped[list[ReportFinding]] = relationship(
        back_populates="report",
        cascade="all, delete-orphan",
        # `findingIds` 的顺序**是报告的一部分**（严重度降序，契约 §2.7）。
        # 靠 `position` 而不是靠 severity 现排：现排会让同一份报告在
        # 数据变化后换一个顺序，而报告编号没变 —— 客户拿两份对不上。
        order_by="ReportFinding.position",
    )

    @property
    def finding_ids(self) -> list[str]:
        return [entry.finding_id for entry in self.entries]

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Report id={self.id!r} code={self.code!r} findings={self.finding_count}>"


class ReportFinding(Base):
    """报告 ↔ 漏洞的冻结关联，带顺序。

    `position` 从 0 开始，是**报告内顺序**（严重度降序）。它是有意义的载荷，
    不是排序技巧：报告正文的编号、目录、交叉引用都按它走。
    """

    __tablename__ = "report_findings"
    __table_args__ = (
        # ⚠️ 这里**刻意只留 position 的唯一约束**。
        #
        # 原来的模型上还有一条 `UniqueConstraint("report_id", "finding_id")`，
        # 而它和主键（同样是这两列）完全重合。SQLAlchemy 在生成 CREATE TABLE 时
        # 会把与主键相同的唯一约束跳过 —— 于是它**从来没进过数据库**，
        # 但模型里一直声明着。后果是每次 `alembic revision --autogenerate`
        # 都会报一次「检测到新增约束 uq_report_findings_pair」，
        # 生成一个什么都不改的迁移。
        #
        # 这类"永远存在的假差异"比一个真差异更危险：它训练人忽略 autogenerate
        # 的输出，而总有一天那条被忽略的提示是真的。
        # 任务 2 生成 0002 时抓到并修掉了它。
        UniqueConstraint("report_id", "position", name="uq_report_findings_position"),
        Index("ix_report_findings_finding_id", "finding_id"),
    )

    report_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("reports.id", ondelete="CASCADE"), primary_key=True
    )
    finding_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("findings.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    report: Mapped[Report] = relationship(back_populates="entries")
