"""评测快照（v1.81）。

一张表只存**质量指标快照**，不存原始运行记录 —— 原始数据已经在
scans / findings / challenges / scan_facts 里，快照是"某一刻的截面"，
用来回答"这周比上周好了没有"。存原始记录会让这张表变成第二份事实源，
迟早与源表漂移。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.columns import JsonColumn, UtcDateTime
from app.core.db import Base
from app.core.timeutil import now_utc


class QualitySnapshot(Base):
    __tablename__ = "quality_snapshots"
    __table_args__ = (Index("ix_quality_snapshots_captured", "captured_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: 人可读标签（"v1.81 基线" / "换模型前"）。空 = 未命名。
    label: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    #: `quality.quality_snapshot()` 的原样输出。
    payload: Mapped[dict] = mapped_column(JsonColumn, nullable=False, default=dict)
    captured_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=now_utc
    )

    def __repr__(self) -> str:  # pragma: no cover - 排错用
        return f"<QualitySnapshot {self.id} {self.label!r}>"


class BenchmarkCase(Base):
    """评测用例：一条可重复判定"过了没有"的登记项。

    ## 为什么不在这里存"运行"
    运行结果可能多次、要留历史；用例是稳定的。两者分开，删用例才不必
    连带删掉历史（历史里保留当时的判定输入）。
    """

    __tablename__ = "benchmark_cases"
    __table_args__ = (Index("ix_benchmark_cases_suite", "suite"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    suite: Mapped[str] = mapped_column(String(80), nullable=False, default="default")
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    #: `ctf` / `scan`
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    challenge_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scan_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 判定阈值，例如 `{"minFindings": 1}`。
    expect: Mapped[dict] = mapped_column(JsonColumn, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=now_utc
    )


class BenchmarkRun(Base):
    """一次对某用例的判定结果。不触发任何扫描，只读关联对象。"""

    __tablename__ = "benchmark_runs"
    __table_args__ = (
        Index("ix_benchmark_runs_case", "case_id"),
        Index("ix_benchmark_runs_collected", "collected_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: `passed` / `failed` / `pending` / `error`
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    detail: Mapped[dict] = mapped_column(JsonColumn, nullable=False, default=dict)
    collected_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=now_utc
    )
