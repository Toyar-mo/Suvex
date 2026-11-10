"""凭据台账的请求/响应模型。"""

from __future__ import annotations

from pydantic import Field

from app.core.schemas import CamelModel, StrictModel
from app.credentials.models import Credential


class CredentialIn(StrictModel):
    kind: str = Field(default="password", max_length=16)
    username: str = Field(default="", max_length=200)
    secret: str = Field(min_length=1)
    target: str = Field(min_length=1, max_length=512)
    scanId: str | None = None
    findingId: str | None = None
    source: str = Field(default="", max_length=120)
    notes: str = Field(default="", max_length=500)


class CredentialOut(CamelModel):
    id: str
    kind: str
    username: str
    target: str
    scanId: str | None
    findingId: str | None
    source: str
    notes: str
    createdAt: str
    #: 列表永远不回明文，只说明有值。
    hasSecret: bool = True

    @classmethod
    def from_row(cls, row: Credential) -> CredentialOut:
        return cls(
            id=row.id,
            kind=row.kind,
            username=row.username,
            target=row.target,
            scanId=row.scan_id,
            findingId=row.finding_id,
            source=row.source,
            notes=row.notes,
            createdAt=row.created_at.isoformat() if row.created_at else "",
        )


class RevealOut(CamelModel):
    secret: str


class ReusePlanOut(CamelModel):
    score: float
    basis: list[str]
    sameTarget: int
    sameHost: int
    sameScan: int
