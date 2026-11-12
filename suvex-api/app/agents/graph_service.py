"""Agent 执行图的业务规则（P1）。

状态机：
- task: planning → running → completed/failed/cancelled
- goal: open → met/abandoned
- intent: open → running → done/exhausted/blocked/stopped

规则与 ARTeX 对齐的地方：意图只能由**一个** worker 领取；目标只有在有证据
时才能标 met；最后一个目标 met 时任务自动完成。P1 不做自动 planner 唤醒，
领取/完成由角色通过聊天工具调用，但状态是真的。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.graph_models import (
    GOAL_STATUSES,
    INTENT_STATUSES,
    TASK_STATUSES,
    AgentGoal,
    AgentIntent,
    AgentRun,
    AgentTask,
)
from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.timeutil import now_utc


def create_task(
    db: Session,
    *,
    created_by: str,
    title: str,
    objective: str = "",
    scope: str = "",
) -> AgentTask:
    title = (title or "").strip()
    if not title:
        raise ApiError.validation("任务标题不能为空")
    row = AgentTask(
        id=new_id("tsk"),
        created_by=created_by,
        title=title[:200],
        objective=(objective or "").strip(),
        scope=(scope or "").strip(),
        status="planning",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_tasks(db: Session, *, created_by: str, limit: int = 50) -> list[AgentTask]:
    return list(
        db.execute(
            select(AgentTask)
            .where(AgentTask.created_by == created_by)
            .order_by(AgentTask.updated_at.desc())
            .limit(max(1, min(limit, 200)))
        ).scalars()
    )


def get_task(db: Session, *, task_id: str, created_by: str) -> AgentTask:
    row = db.get(AgentTask, task_id)
    if row is None or row.created_by != created_by:
        raise ApiError.not_found("任务不存在")
    return row


def add_goal(db: Session, *, task: AgentTask, text: str) -> AgentGoal:
    text = (text or "").strip()
    if not text:
        raise ApiError.validation("目标不能为空")
    if task.status in {"completed", "cancelled"}:
        raise ApiError.conflict("TASK_CLOSED", "任务已结束，不能再加目标")
    seq = (
        db.scalar(select(func.max(AgentGoal.seq)).where(AgentGoal.task_id == task.id))
        or 0
    ) + 1
    row = AgentGoal(id=new_id("gol"), task_id=task.id, seq=seq, text=text[:2000])
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def add_intent(
    db: Session,
    *,
    task: AgentTask,
    summary: str,
    goal_id: str | None = None,
    priority: int = 5,
) -> AgentIntent:
    summary = (summary or "").strip()
    if not summary:
        raise ApiError.validation("意图不能为空")
    if task.status in {"completed", "cancelled"}:
        raise ApiError.conflict("TASK_CLOSED", "任务已结束，不能再加意图")
    if goal_id is not None:
        goal = db.get(AgentGoal, goal_id)
        if goal is None or goal.task_id != task.id:
            raise ApiError.validation("goal_id 不属于这个任务")
    seq = (
        db.scalar(select(func.max(AgentIntent.seq)).where(AgentIntent.task_id == task.id))
        or 0
    ) + 1
    row = AgentIntent(
        id=new_id("itn"),
        task_id=task.id,
        goal_id=goal_id,
        seq=seq,
        summary=summary[:2000],
        priority=max(0, min(int(priority), 10)),
    )
    db.add(row)
    task.status = "running" if task.status == "planning" else task.status
    db.commit()
    db.refresh(row)
    return row


def claim_intent(
    db: Session, *, intent_id: str, worker_session_id: str | None, role: str = "worker"
) -> AgentIntent:
    row = db.get(AgentIntent, intent_id)
    if row is None:
        raise ApiError.not_found("意图不存在")
    if row.status == "running":
        raise ApiError.conflict("INTENT_RUNNING", "这条意图已经被领取")
    if row.status not in {"open", "blocked", "exhausted"}:
        raise ApiError.conflict("INTENT_CLOSED", f"意图状态为 {row.status}，不能领取")
    row.status = "running"
    row.worker_session_id = worker_session_id
    task = db.get(AgentTask, row.task_id)
    if task is not None and task.status == "planning":
        task.status = "running"
    run = AgentRun(
        id=new_id("run"),
        task_id=row.task_id,
        intent_id=row.id,
        role=role,
        session_id=worker_session_id,
        status="running",
    )
    db.add(run)
    db.commit()
    db.refresh(row)
    return row


def finish_intent(
    db: Session,
    *,
    intent_id: str,
    status: str,
    result: str = "",
    worker_session_id: str | None = None,
) -> AgentIntent:
    if status not in INTENT_STATUSES or status == "running":
        raise ApiError.validation(f"非法意图结果：{status}")
    row = db.get(AgentIntent, intent_id)
    if row is None:
        raise ApiError.not_found("意图不存在")
    if row.status != "running":
        raise ApiError.conflict("INTENT_NOT_RUNNING", "这条意图不在运行中")
    if worker_session_id and row.worker_session_id != worker_session_id:
        raise ApiError.conflict("INTENT_OWNER", "只能由领取它的 worker 写回")
    row.status = status
    row.result = (result or "").strip()[:20000]
    row.updated_at = now_utc()
    run = db.execute(
        select(AgentRun)
        .where(AgentRun.intent_id == row.id, AgentRun.status == "running")
        .order_by(AgentRun.created_at.desc())
    ).scalars().first()
    if run is not None:
        run.status = status
        run.output = row.result
        run.finished_at = now_utc()
    task = db.get(AgentTask, row.task_id)
    if task is not None:
        task.status = "running" if task.status == "planning" else task.status
    db.commit()
    db.refresh(row)
    return row


def prove_goal(db: Session, *, goal_id: str, evidence: str) -> AgentGoal:
    text = (evidence or "").strip()
    if not text:
        raise ApiError.validation("标完成目标必须给证据")
    row = db.get(AgentGoal, goal_id)
    if row is None:
        raise ApiError.not_found("目标不存在")
    if row.status == "abandoned":
        raise ApiError.conflict("GOAL_ABANDONED", "目标已放弃")
    row.status = "met"
    row.evidence = text[:20000]
    # 会话工厂 autoflush=False：不显式 flush，下面的 count 还会看到 open。
    db.flush()
    task = db.get(AgentTask, row.task_id)
    if task is not None:
        remaining = db.scalar(
            select(func.count())
            .select_from(AgentGoal)
            .where(AgentGoal.task_id == task.id, AgentGoal.status == "open")
        )
        if not remaining:
            task.status = "completed"
            task.completed_at = now_utc()
    db.commit()
    db.refresh(row)
    return row


def cancel_intent(db: Session, *, intent_id: str, reason: str = "") -> AgentIntent:
    row = db.get(AgentIntent, intent_id)
    if row is None:
        raise ApiError.not_found("意图不存在")
    if row.status not in {"open", "running"}:
        raise ApiError.conflict("INTENT_CLOSED", "这条意图已经结束")
    row.status = "stopped"
    if reason.strip():
        row.result = reason.strip()[:20000]
    db.commit()
    db.refresh(row)
    return row


def cancel_task(db: Session, *, task: AgentTask) -> AgentTask:
    task.status = "cancelled"
    task.completed_at = now_utc()
    for row in db.execute(
        select(AgentIntent).where(AgentIntent.task_id == task.id, AgentIntent.status == "running")
    ).scalars():
        row.status = "stopped"
    db.commit()
    db.refresh(task)
    return task


def graph(db: Session, *, task: AgentTask) -> dict[str, Any]:
    goals = list(
        db.execute(select(AgentGoal).where(AgentGoal.task_id == task.id).order_by(AgentGoal.seq))
        .scalars()
    )
    intents = list(
        db.execute(
            select(AgentIntent)
            .where(AgentIntent.task_id == task.id)
            .order_by(AgentIntent.priority.desc(), AgentIntent.seq)
        ).scalars()
    )
    runs = list(
        db.execute(
            select(AgentRun).where(AgentRun.task_id == task.id).order_by(AgentRun.created_at)
        ).scalars()
    )
    return {"task": task, "goals": goals, "intents": intents, "runs": runs}


__all__ = [
    "GOAL_STATUSES",
    "INTENT_STATUSES",
    "TASK_STATUSES",
    "add_goal",
    "add_intent",
    "cancel_intent",
    "cancel_task",
    "claim_intent",
    "create_task",
    "finish_intent",
    "get_task",
    "graph",
    "list_tasks",
    "prove_goal",
]
