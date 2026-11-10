"""凭据台账（v1.83）。

借鉴 ARTEX 的 `credentials` 一等实体，**独立实现**（AGPL，不复制源码）。
凭据是扫描过程的产物，必须加密落库、列表只回显掩码、读取明文要审计。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.columns import UtcDateTime
from app.core.db import Base
from app.core.timeutil import now_utc

KINDS = ("password", "hash", "ticket", "key")


class Credential(Base):
    """一条可复用凭据。

    `secret_token` 是 Fernet 密文；**列表接口永远不解密**，只有显式的
    reveal（admin + 审计）才解出明文。
    """

    __tablename__ = "credentials"
    __table_args__ = (
        CheckConstraint(
            f"kind IN ({', '.join(repr(v) for v in KINDS)})", name="kind_valid"
        ),
        Index("ix_credentials_target", "target"),
        Index("ix_credentials_scan", "scan_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scan_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    finding_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="password")
    username: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    target: Mapped[str] = mapped_column(String(512), nullable=False)
    secret_token: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    notes: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=now_utc
    )
