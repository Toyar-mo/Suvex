"""`audit` 领域路由（契约 §3.9，任务 8）。

| 方法 | 路径     | 鉴权    |
| ---- | -------- | ------- |
| GET  | `/audit` | `admin` |

⚠️ **只有读，没有任何写 / 删**。审计记录由服务端在业务动作里写
（`app.audit.service.record`），而**不允许通过 HTTP 创建** ——
一个能被外部写入的审计日志，其可信度等于零。

删除也只有保留期清理一条路（`purge_expired`），不走 HTTP：
如果有一个 `DELETE /audit/{id}`，它就成了一条抹掉某次操作痕迹的捷径。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.audit import service
from app.audit.schemas import AuditEntryOut, AuditPageOut
from app.auth.deps import Identity, require_admin
from app.core.db import get_db
from app.core.timeutil import parse_iso

router = APIRouter(prefix="/audit", tags=["审计"])

AdminDep = Annotated[Identity, Depends(require_admin)]
DbDep = Annotated[Session, Depends(get_db)]


@router.get("", response_model=AuditPageOut, summary="审计日志（只读，admin）")
def list_audit(
    _identity: AdminDep,
    db: DbDep,
    limit: Annotated[int, Query(ge=1, le=200, description="每页条数，默认 50")] = 50,
    cursor: Annotated[str | None, Query(description="上一页返回的 nextCursor")] = None,
    action: Annotated[str | None, Query(description="按动作筛选，如 scan.create")] = None,
    actor: Annotated[str | None, Query(description="按主体称呼模糊筛选")] = None,
    since: Annotated[str | None, Query(description="只看这个时刻之后的（ISO 8601）")] = None,
) -> AuditPageOut:
    """审计日志，**最新在前**。

    `limit` 上限 200：这个端点的响应会被完整读进内存，
    而审计记录里的 `userAgent`/`detail` 是可变的长度 ——
    不设上限等于给了一个"一次拉全表"的入口。
    """
    since_dt = None
    if since:
        try:
            since_dt = parse_iso(since)
        except ValueError:
            # 坏参数静默忽略 vs 报错？这里**报错**：`since` 是筛选条件，
            # 忽略它会让调用方以为自己在看一个时间窗口，实际看到的是全部 ——
            # 一个静默失效的筛选条件会产生"看起来没问题但结论是错的"结果。
            from app.core.errors import ApiError

            raise ApiError.validation(
                "since 不是合法的 ISO 8601 时刻。",
                {"since": "形如 2026-09-24T00:00:00Z"},
            ) from None

    rows = service.list_entries(
        db, limit=limit, cursor=cursor, action=action, actor=actor, since=since_dt
    )
    entries = [
        AuditEntryOut(
            id=row.id,
            at=row.at,
            action=row.action,
            actionLabel=service.action_label(row.action),
            actorKind=row.actor_kind,
            actorLabel=row.actor_label,
            actorId=row.actor_id,
            target=row.target,
            summary=row.summary,
            ip=row.ip,
            userAgent=row.user_agent,
            detail=row.detail,
        )
        for row in rows
    ]
    # 只有**取满一页**时才给下一页游标。不满说明后面没有了 ——
    # 给一个游标会让前端多一次必然为空的请求。
    next_cursor = service.cursor_for(rows[-1]) if len(rows) == limit else None

    return AuditPageOut(
        entries=entries,
        nextCursor=next_cursor,
        total=service.count(db),
        retentionDays=service.AUDIT_RETENTION_DAYS,
    )
