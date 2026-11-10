"""紫队检测规则（v1.82）。

## 为什么需要

红队产出发现，但"这些发现如果发生在你的环境，哪些检测会响"从来没有
被回答过。规则库把这一层显式化：每条规则声明它认哪个 CWE / 标题 / 目标，
evaluate 用它扫一遍现有 findings。

## 规则是**数据**，不是代码

匹配逻辑只有四种字段（cwe / titleContains / targetContains / severity），
新增检测项不需要改代码。规则库也能导出给 SIEM（见 `export_events`）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.columns import UtcDateTime
from app.core.db import Base
from app.core.timeutil import now_utc


class PurpleRule(Base):
    __tablename__ = "purple_rules"
    __table_args__ = (Index("ix_purple_rules_enabled", "enabled"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="medium")
    #: 匹配 CWE（`CWE-89` 形式）。空 = 不看 CWE。
    cwe: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: 标题包含（大小写不敏感）。空 = 不看标题。
    title_contains: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: 目标包含。空 = 不看目标。
    target_contains: Mapped[str | None] = mapped_column(String(512), nullable=True)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=now_utc
    )
