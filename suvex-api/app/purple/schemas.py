"""紫队规则的请求/响应模型。"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from app.core.schemas import CamelModel, StrictModel
from app.purple.models import PurpleRule


class RuleIn(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    severity: str = Field(default="medium", max_length=16)
    cwe: str | None = Field(default=None, max_length=32)
    titleContains: str | None = Field(default=None, max_length=200)
    targetContains: str | None = Field(default=None, max_length=512)
    enabled: bool = True


class RuleOut(CamelModel):
    id: str
    name: str
    severity: str
    cwe: str | None
    titleContains: str | None
    targetContains: str | None
    enabled: bool
    createdAt: str

    @classmethod
    def from_row(cls, row: PurpleRule) -> RuleOut:
        return cls(
            id=row.id,
            name=row.name,
            severity=row.severity,
            cwe=row.cwe,
            titleContains=row.title_contains,
            targetContains=row.target_contains,
            enabled=row.enabled,
            createdAt=row.created_at.isoformat() if row.created_at else "",
        )


class PurpleHit(CamelModel):
    rule: RuleOut
    count: int
    samples: list[dict[str, Any]]


class EvaluateOut(CamelModel):
    totalFindings: int
    hits: list[PurpleHit]
