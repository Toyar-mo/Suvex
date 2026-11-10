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
