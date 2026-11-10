"""内网会话台账的请求/响应模型。"""

from __future__ import annotations

from pydantic import Field

from app.core.schemas import CamelModel, StrictModel
from app.intranet.models import IntranetSession


class SessionIn(StrictModel):
    kind: str = Field(pattern="^(webshell|reverse|ssh)$")
    host: str = Field(min_length=1, max_length=512)
    username: str = Field(default="", max_length=200)
    secret: str = Field(min_length=1)
    notes: str = Field(default="", max_length=1000)
    status: str = Field(default="unknown", pattern="^(alive|dead|unknown)$")


class SessionPatch(StrictModel):
    status: str | None = Field(default=None, pattern="^(alive|dead|unknown)$")
    notes: str | None = Field(default=None, max_length=1000)
    username: str | None = Field(default=None, max_length=200)


class SessionOut(CamelModel):
    id: str
    kind: str
    host: str
    username: str
    status: str
    notes: str
    createdBy: str
    createdAt: str
    updatedAt: str
    hasSecret: bool = True

    @classmethod
    def from_row(cls, row: IntranetSession) -> SessionOut:
        return cls(
            id=row.id,
            kind=row.kind,
            host=row.host,
            username=row.username,
            status=row.status,
            notes=row.notes,
            createdBy=row.created_by,
            createdAt=row.created_at.isoformat() if row.created_at else "",
            updatedAt=row.updated_at.isoformat() if row.updated_at else "",
        )
