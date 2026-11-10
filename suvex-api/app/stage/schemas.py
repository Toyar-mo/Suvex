"""受管暂存的请求/响应模型。"""

from __future__ import annotations

from pydantic import Field

from app.core.schemas import CamelModel, StrictModel
from app.stage.models import ManagedStage


class StageIn(StrictModel):
    name: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1)
    ttlMinutes: int = Field(default=15, ge=1, le=120)
    contentType: str = Field(default="text/plain", max_length=120)


class StageOut(CamelModel):
    id: str
    token: str
    name: str
    url: str
    expiresAt: str

    @classmethod
    def from_row(cls, row: ManagedStage) -> StageOut:
        return cls(
            id=row.id,
            token=row.token,
            name=row.name,
            url=f"/api/v1/stage/s/{row.token}/{row.name}",
            expiresAt=row.expires_at.isoformat() if row.expires_at else "",
        )
