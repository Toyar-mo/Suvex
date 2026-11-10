"""评测快照的请求/响应模型。"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from app.benchmark.models import QualitySnapshot
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
