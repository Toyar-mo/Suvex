"""内网会话台账（v1.83）。

这是 ARTEX `sessions` 子系统的**登记面**：记录人工拿到的立足点
（webshell / 反弹 / ssh），供后续人工使用与审计。平台**不会**自动
连接或执行命令 —— 没有执行器就不假装有。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.columns import UtcDateTime
from app.core.db import Base
from app.core.timeutil import now_utc

KINDS = ("webshell", "reverse", "ssh")
STATUSES = ("alive", "dead", "unknown")


class IntranetSession(Base):
    __tablename__ = "intranet_sessions"
    __table_args__ = (
        CheckConstraint(f"kind IN ({', '.join(repr(v) for v in KINDS)})", name="kind_valid"),
        CheckConstraint(
            f"status IN ({', '.join(repr(v) for v in STATUSES)})", name="status_valid"
        ),
        Index("ix_intranet_sessions_created_by", "created_by"),
        Index("ix_intranet_sessions_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    host: Mapped[str] = mapped_column(String(512), nullable=False)
    username: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    #: Fernet 密文；列表不回显。没有执行器，也不提供 reveal。
    secret_token: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    notes: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    created_by: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=now_utc, onupdate=now_utc
    )
