"""受管暂存（v1.83）。

对齐 ARTEX `stage_share`：临时投递物必须有平台台账、随机 token、硬 TTL。
与普通上传的区别：它**一定会过期**，且过期即不可解析（不是"隐藏"）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.columns import UtcDateTime
from app.core.db import Base
from app.core.timeutil import now_utc


class ManagedStage(Base):
    __tablename__ = "managed_stage"
    __table_args__ = (
        Index("ix_managed_stage_token", "token", unique=True),
        Index("ix_managed_stage_expires", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    token: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False, default="text/plain")
    size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=now_utc)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    downloaded_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
