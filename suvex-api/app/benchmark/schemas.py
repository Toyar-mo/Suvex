"""评测快照的请求/响应模型。"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from app.benchmark.models import BenchmarkCase, BenchmarkRun, QualitySnapshot
from app.core.schemas import CamelModel, StrictModel


class CaptureIn(StrictModel):
    label: str = Field(default="", max_length=200)


class SnapshotOut(CamelModel):
    id: str
    label: str
    capturedAt: str
    payload: dict[str, Any]

    @classmethod
    def from_row(cls, row: QualitySnapshot) -> SnapshotOut:
        return cls(
            id=row.id,
            label=row.label,
            capturedAt=row.captured_at.isoformat() if row.captured_at else "",
            payload=row.payload or {},
        )


class SnapshotListOut(CamelModel):
    items: list[SnapshotOut]


class CompareOut(CamelModel):
    before: dict[str, Any]
    after: dict[str, Any]
    delta: dict[str, Any]


class CaseIn(StrictModel):
    suite: str = "default"
    name: str = Field(min_length=1, max_length=200)
    kind: str = Field(pattern="^(ctf|scan)$")
    challengeId: str | None = None
    scanId: str | None = None
    expect: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class CaseOut(CamelModel):
    id: str
    suite: str
    name: str
    kind: str
    challengeId: str | None
    scanId: str | None
    expect: dict[str, Any]
    enabled: bool
    createdAt: str

    @classmethod
    def from_row(cls, row: BenchmarkCase) -> CaseOut:
        return cls(
            id=row.id,
            suite=row.suite,
            name=row.name,
            kind=row.kind,
            challengeId=row.challenge_id,
            scanId=row.scan_id,
            expect=row.expect or {},
            enabled=row.enabled,
            createdAt=row.created_at.isoformat() if row.created_at else "",
        )


class RunOut(CamelModel):
    id: str
    caseId: str
    status: str
    detail: dict[str, Any]
    collectedAt: str

    @classmethod
    def from_row(cls, row: BenchmarkRun) -> RunOut:
        return cls(
            id=row.id,
            caseId=row.case_id,
            status=row.status,
            detail=row.detail or {},
            collectedAt=row.collected_at.isoformat() if row.collected_at else "",
        )


class SuiteRunOut(CamelModel):
    suite: str
    runs: list[RunOut]
    passed: int
    failed: int
    pending: int
    error: int


class LaunchOut(CamelModel):
    """主动执行一次用例的结果：真的建了一条扫描。"""

    caseId: str
    scanId: str
    status: str


class SummaryOut(CamelModel):
    """一套用例的当前成绩（每个用例只取最近一次判定）。"""

    suite: str
    cases: int
    passed: int
    failed: int
    pending: int
    error: int
    #: 已判定里的通过率；没有可判定的行为 `None`（不是 0）。
    passRate: float | None
    #: `pending + error` 占比。
    unfinishedRate: float | None
    findings: int
    falsePositives: int
    falsePositiveRate: float | None
    avgDurationSec: float | None
