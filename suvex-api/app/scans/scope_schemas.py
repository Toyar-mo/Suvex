"""待批准目标的请求/响应模型。"""

from __future__ import annotations

from pydantic import Field

from app.core.schemas import CamelModel, StrictModel
from app.scans.models import PendingScope


class PendingScopeIn(StrictModel):
    target: str = Field(min_length=1, max_length=512)
    reason: str = Field(default="", max_length=500)


class DecideIn(StrictModel):
    approve: bool


class PendingScopeOut(CamelModel):
    id: str
    scanId: str
    target: str
    reason: str
    status: str
    createdAt: str
    decidedAt: str | None = None
    decidedBy: str | None = None

    @classmethod
    def from_row(cls, row: PendingScope) -> PendingScopeOut:
        return cls(
            id=row.id,
            scanId=row.scan_id,
            target=row.target,
            reason=row.reason,
            status=row.status,
            createdAt=row.created_at.isoformat() if row.created_at else "",
            decidedAt=row.decided_at.isoformat() if row.decided_at else None,
            decidedBy=row.decided_by,
        )
