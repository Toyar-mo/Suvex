"""实时事件流的查询与写入。

本模块是**事件唯一的读写入口**。后续任务的 agent 运行时通过
`append_event()` 产出事件，SSE 端通过 `events_after()` 读取 ——
两边共用同一套排序与序列化规则，不会出现「写的时候按 A 排、
读的时候按 B 排」这种只在同时发生时才暴露的问题。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.timeutil import now_utc
from app.core.vocab import ActivityEventType, AgentPhase
from app.events.models import ActivityEvent, Agent


def agents_for_scan(db: Session, scan_id: str) -> list[Agent]:
    """名册。按 id 排序 —— id 里带 ULID，字典序即创建序。"""
    return list(db.execute(select(Agent).where(Agent.scan_id == scan_id).order_by(Agent.id)).scalars())


def recent_events(
    db: Session,
    *,
    scan_id: str | None = None,
    since: datetime | None = None,
    limit: int = 200,
) -> tuple[list[ActivityEvent], bool]:
    """历史事件（升序），供 SSE 建立前的 seed 使用（契约 §4.4）。

    返回 `(事件列表, hasMore)`。多取一条来判断 `hasMore` ——
    用 `count(*)` 会多一次全表扫描，而调用方只关心"还有没有"。
    """
    stmt = select(ActivityEvent)
    if scan_id is not None:
        stmt = stmt.where(ActivityEvent.scan_id == scan_id)
    if since is not None:
        stmt = stmt.where(ActivityEvent.at >= since)
    # 取最近的 N 条（按 seq 倒序），再翻回升序返回。
    # 直接升序取前 N 条会返回**最旧的** N 条，而 seed 要的是"最近发生了什么"。
    stmt = stmt.order_by(ActivityEvent.seq.desc()).limit(limit + 1)
    rows = list(db.execute(stmt).scalars())
    has_more = len(rows) > limit
    rows = rows[:limit]
    rows.reverse()
    return rows, has_more


def events_after(
    db: Session,
    *,
    cursor: int,
    scan_id: str | None = None,
    limit: int = 500,
) -> list[ActivityEvent]:
    """`seq > cursor` 的事件（升序）。SSE 的补发与轮询都走它。"""
    stmt = select(ActivityEvent).where(ActivityEvent.seq > cursor)
    if scan_id is not None:
        stmt = stmt.where(ActivityEvent.scan_id == scan_id)
    return list(db.execute(stmt.order_by(ActivityEvent.seq).limit(limit)).scalars())


def seq_for_event_id(db: Session, event_id: str) -> int | None:
    """把 `Last-Event-ID` 翻成游标。

    找不到 = 那条事件已经被清理（或者客户端给了一个不存在的 id）。
    契约 §4.4 要求此时发 `resync` 帧，而不是静默地从当前位置继续 ——
    静默继续会让客户端以为中间没有漏，而实际上漏了一段。
    """
    return db.execute(
        select(ActivityEvent.seq).where(ActivityEvent.id == event_id)
    ).scalar_one_or_none()


def latest_seq(db: Session, scan_id: str | None = None) -> int:
    stmt = select(func.coalesce(func.max(ActivityEvent.seq), 0))
    if scan_id is not None:
        stmt = stmt.where(ActivityEvent.scan_id == scan_id)
    return int(db.execute(stmt).scalar_one())


def append_event(
    db: Session,
    *,
    scan_id: str,
    agent_id: str,
    agent_phase: str | AgentPhase,
    type: str | ActivityEventType,
    summary: str,
    detail: dict[str, Any] | None = None,
    at: datetime | None = None,
) -> ActivityEvent:
    """写入一条完整事件。

    ⚠️ `at` 是**事件发生时刻**，不是推送时刻（契约 §4.4 专门警告过）。
    调用方如果知道真实发生时间就必须传它；补发时这一点尤其明显 ——
    3 分钟前的事件补发出去，`at` 仍应是 3 分钟前。

    不传则取当前时刻，并且**在入库前取定**：让 `at` 与 `seq` 的顺序一致。
    如果交给数据库在 commit 时算，同一事务里的多条事件会拿到同一个时间戳，
    它们之间的先后就只能靠 `seq` —— 那没问题，但"时间戳全都一样"
    在界面上看起来像是一起发生的，而实际不是。

    `detail` 的形状由调用方保证（契约 §2.6 的 8 种载荷）。
    这里只做一个最低限度的形状校验：`detail.kind` 必须存在 ——
    **判别式缺失等于这条例载荷没人能解析**，而它不会在写入时报错，
    只会在前端渲染时显示空白。
    """
    if detail is not None and not isinstance(detail.get("kind"), str):
        raise ApiError.validation(
            "事件载荷缺少 kind 判别式，无法确定形状。",
            {"detail.kind": "必填"},
        )

    phase_value = agent_phase.value if isinstance(agent_phase, AgentPhase) else agent_phase
    type_value = type.value if isinstance(type, ActivityEventType) else type

    event = ActivityEvent(
        id=new_id("evt"),
        scan_id=scan_id,
        agent_id=agent_id,
        agent_phase=phase_value,
        type=type_value,
        summary=summary,
        detail=detail,
        at=at or now_utc(),
    )
    db.add(event)
    return event


def record_agent_activity(db: Session, agent: Agent, *, at: datetime | None = None) -> None:
    """agent 每产生一个事件就累加计数并刷新最后活跃时间。

    契约 §2.4：这两个字段是**兜底值**，前端会按事件流覆盖它们。
    所以这里不做"精确"的维护（比如重算），只做增量 ——
    它的作用是让「一个 agent 产生了 3 个事件然后挂了」这种情况
    即使前端还没收到流也能被读出来。
    """
    moment = at or now_utc()
    agent.event_count = int(agent.event_count or 0) + 1
    agent.last_active_at = moment


def summarize_types(events: Sequence[ActivityEvent]) -> dict[str, int]:
    """按类型计数。给 Overview 的派生用（任务 6 会用上）。"""
    counts: dict[str, int] = {}
    for event in events:
        counts[event.type] = counts.get(event.type, 0) + 1
    return counts
