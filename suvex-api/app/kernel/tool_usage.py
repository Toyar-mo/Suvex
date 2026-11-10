"""工具调用统计（v1.83）。

借鉴 ARTEX 工具页的「调用 N 次」：工具装了但模型从不用，是一个只能靠
统计发现的问题。这里从 `activity_events` 的 `tool_call` 事件聚合，
口径与 `skill_usage` 同源（同表、同 `detail.tool`），避免两处数字打架。

## 诚实边界

- 只统计**最近 `limit` 条** `tool_call`；超过上限时 `truncated=true`，
  界面必须写出来，否则"0 次"会被读成"从没用过"。
- 事件里的 `detail.tool` 是模型写的动作名；解析不出的行计入 sampled
  但不计入任何工具 —— 比例因此是诚实的。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

#: 最多回溯多少条工具调用事件。理由同 `skill_usage.MAX_USAGE_EVENTS`。
MAX_USAGE_EVENTS = 20_000


@dataclass(frozen=True)
class ToolUsageReport:
    rows: dict[str, dict] = field(default_factory=dict)
    sampled: int = 0
    truncated: bool = False
    limit: int = MAX_USAGE_EVENTS


def collect_tool_usage(db: Session, *, limit: int = MAX_USAGE_EVENTS) -> ToolUsageReport:
    from app.events.models import ActivityEvent

    stmt = (
        select(ActivityEvent.detail, ActivityEvent.at)
        .where(ActivityEvent.type == "tool_call")
        .order_by(ActivityEvent.seq.desc())
        .limit(limit + 1)
    )
    fetched = db.execute(stmt).all()
    truncated = len(fetched) > limit
    fetched = fetched[:limit]

    rows: dict[str, dict] = {}
    for detail, at in fetched:
        if not isinstance(detail, dict):
            continue
        name = str(detail.get("tool") or "").strip()
        if not name:
            continue
        entry = rows.setdefault(name, {"calls": 0, "lastCallAt": None})
        entry["calls"] += 1
        if entry["lastCallAt"] is None and isinstance(at, datetime):
            entry["lastCallAt"] = at.isoformat()

    return ToolUsageReport(rows=rows, sampled=len(fetched), truncated=truncated, limit=limit)


__all__ = ["MAX_USAGE_EVENTS", "ToolUsageReport", "collect_tool_usage"]
