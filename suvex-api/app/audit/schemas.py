"""`audit` 领域的出参（契约 §3.9）。"""

from __future__ import annotations

from typing import Any

from app.core.schemas import CamelModel
from app.core.timeutil import IsoDateTime


class AuditEntryOut(CamelModel):
    """一条审计记录。

    ⚠️ **没有 `updatedAt` / 没有 `deletedAt`** —— 审计记录只有创建时刻。
    一个"可修改的审计记录"在语义上就不是审计记录。
    """

    id: str
    at: IsoDateTime
    action: str
    # 动作的中文标签。放在响应里而不是让前端维护一张映射表：
    # 加一个动作时后端先知道（它就是产生动作的那一端），
    # 而前端那张表一旦漏更新，界面上就会出现一个英文的 `scan.create`。
    actionLabel: str
    actorKind: str
    actorLabel: str
    actorId: str | None
    target: str | None
    summary: str
    ip: str | None
    userAgent: str | None
    detail: dict[str, Any] | None


class AuditPageOut(CamelModel):
    """一页审计记录。

    带 `nextCursor` 而不是只给数组：审计表会持续增长，
    调用方需要知道"还有没有更早的"。`nextCursor` 为 `null` = 到头了。

    也带 `total`：单机场景下这个计数很便宜，而它回答一个界面上
    很自然的问题 ——"一共记了多少条"。
    """

    entries: list[AuditEntryOut]
    nextCursor: str | None
    total: int
    # 让"这些记录会留多久"在 API 里也是可见的，而不是只写在文档里。
    retentionDays: int
