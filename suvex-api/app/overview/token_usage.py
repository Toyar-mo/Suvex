"""LLM Token 按天序列（v1.82）。

## 为什么单独一个端点

`/overview` 给的是**总量**；图表要的是**时间序列**。把 30 天的点塞进
总览响应会让那个端点的体积随窗口线性增长，而总览每 60 秒轮询一次。
分开之后：总量仍然便宜，图表只在打开时取一次。

## 缓存读

`KernelSolveUsage` 没有缓存命中的列。这里返回 `cacheTracked: false` 与
`cacheRead: 0`，**不估算**缓存读数 —— 估算值画在图上会被当成事实。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.kernel.models import KernelSolveUsage

SUPPORTED_DAYS = (7, 30, 90, 180, 365)


def _day(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def token_series(db: Session, *, days: int = 30) -> dict[str, Any]:
    """按天聚合 token；缺失的日期补 0，保证图上横轴连续。"""
    if days not in SUPPORTED_DAYS:
        days = 30
    today = datetime.now(UTC).date()
    start = today - timedelta(days=days - 1)
    start_at = datetime.combine(start, datetime.min.time(), tzinfo=UTC)

    rows = db.execute(
        select(
            func.date(KernelSolveUsage.created_at),
            func.coalesce(func.sum(KernelSolveUsage.prompt_tokens), 0),
            func.coalesce(func.sum(KernelSolveUsage.completion_tokens), 0),
            func.count(),
        )
        .where(KernelSolveUsage.created_at >= start_at)
        .group_by(func.date(KernelSolveUsage.created_at))
    ).all()

    by_day: dict[date, dict[str, int]] = {}
    for raw_day, prompt, completion, calls in rows:
        day = _day(raw_day)
        by_day[day] = {
            "prompt": int(prompt),
            "completion": int(completion),
            "calls": int(calls),
        }

    series: list[dict[str, Any]] = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        item = by_day.get(day, {"prompt": 0, "completion": 0, "calls": 0})
        series.append(
            {
                "date": day.isoformat(),
                "prompt": item["prompt"],
                "completion": item["completion"],
                "total": item["prompt"] + item["completion"],
                "calls": item["calls"],
                # 没有缓存列：恒 0，并用 cacheTracked 说明原因。
                "cacheRead": 0,
            }
        )

    unreported = int(
        db.scalar(
            select(func.count())
            .select_from(KernelSolveUsage)
            .where(
                KernelSolveUsage.created_at >= start_at,
                KernelSolveUsage.usage_reported.is_(False),
            )
        )
        or 0
    )
    return {
        "days": days,
        "series": series,
        "totals": {
            "prompt": sum(item["prompt"] for item in series),
            "completion": sum(item["completion"] for item in series),
            "total": sum(item["total"] for item in series),
            "calls": sum(item["calls"] for item in series),
            "unreportedCalls": unreported,
        },
        "cacheTracked": False,
        "cacheRead": 0,
        "cacheRate": None,
    }


__all__ = ["SUPPORTED_DAYS", "token_series"]
