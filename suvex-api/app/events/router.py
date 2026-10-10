"""实时事件流的 HTTP 端点（契约 §4）。

| 方法 | 路径                                        |
| ---- | ------------------------------------------- |
| GET  | `/scans/{scanId}/events`          （SSE）   |
| GET  | `/scans/{scanId}/events/recent`             |
| GET  | `/events`                         （SSE，全局）|

`/scans/{scanId}/agents` 在 `app/scans/router.py`（它属于扫描资源）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.auth.deps import Identity, require_read
from app.core.db import get_db
from app.core.errors import ApiError
from app.events import service
from app.events.schemas import RecentEventsOut
from app.events.stream import SSE_HEADERS, SSE_MEDIA_TYPE, stream_events
from app.scans.models import Scan

router = APIRouter(tags=["实时事件流"])


def _require_scan(db: Session, scan_id: str) -> Scan:
    scan = db.get(Scan, scan_id)
    if scan is None:
        raise ApiError.not_found("扫描")
    return scan


def _last_event_id(request: Request, query_value: str | None) -> str | None:
    """取补发游标。

    优先级：**查询参数 > 请求头**。
    查询参数是调用方显式放的（首次连接时前端的 seed 游标），
    而请求头是浏览器自动带的重连游标 —— 重连时两者都在，且指向同一条事件，
    所以顺序不影响正确性；但如果客户端显式传了参数，那是它更准的意图。
    """
    return query_value or request.headers.get("last-event-id")


@router.get("/events", summary="全局事件流（SSE）")
def global_events(
    request: Request,
    _identity: Annotated[Identity, Depends(require_read)],
    lastEventId: Annotated[str | None, Query(description="补发游标：从这个事件之后开始")] = None,
) -> StreamingResponse:
    """同时看多个扫描（契约 §4.1）。"""
    return StreamingResponse(
        stream_events(scan_id=None, last_event_id=_last_event_id(request, lastEventId)),
        media_type=SSE_MEDIA_TYPE,
        headers=SSE_HEADERS,
    )


@router.get("/scans/{scanId}/events", summary="单次扫描的事件流（SSE）")
def scan_events(
    scanId: str,
    request: Request,
    _identity: Annotated[Identity, Depends(require_read)],
    db: Annotated[Session, Depends(get_db)],
    lastEventId: Annotated[str | None, Query(description="补发游标：从这个事件之后开始")] = None,
) -> StreamingResponse:
    """前端 Activity 页订阅的就是这条（`/activity?scan=<id>`）。

    先检查扫描存在，再开流：不检查的话，订阅一个不存在的扫描会得到一个
    **永远沉默的 200**，而前端无法区分「目标不存在」与「暂时没有事件」。
    """
    _require_scan(db, scanId)
    return StreamingResponse(
        stream_events(scan_id=scanId, last_event_id=_last_event_id(request, lastEventId)),
        media_type=SSE_MEDIA_TYPE,
        headers=SSE_HEADERS,
    )


@router.get(
    "/scans/{scanId}/events/recent",
    response_model=RecentEventsOut,
    summary="历史事件（SSE 建立前的 seed）",
)
def recent_events(
    scanId: str,
    _identity: Annotated[Identity, Depends(require_read)],
    db: Annotated[Session, Depends(get_db)],
    since: Annotated[datetime | None, Query(description="只取这个时刻之后的事件")] = None,
    limit: Annotated[int, Query(ge=1, le=1000, description="最多返回多少条")] = 200,
) -> RecentEventsOut:
    """契约 §4.4：前端在建立 SSE 之前先拉一次作为 seed。

    顺序必须是**先拉历史、再订阅**，并且把这里最后一条的 id 通过
    `?lastEventId=` 带给 SSE 端点 —— 否则两步之间产生的事件会永久丢失。
    """
    _require_scan(db, scanId)
    events, has_more = service.recent_events(db, scan_id=scanId, since=since, limit=limit)
    return RecentEventsOut.model_validate({"events": events, "has_more": has_more})
