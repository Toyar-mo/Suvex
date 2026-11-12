"""执行图端点（P1）。"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.agents import graph_service as service
from app.agents import runner_service
from app.agents.graph_schemas import (
    AgentGoalIn,
    AgentIntentIn,
    AgentTaskIn,
    CancelIn,
    GoalOut,
    GoalProveIn,
    GraphOut,
    IntentFinishIn,
    IntentOut,
    RunOut,
    TaskOut,
)
from app.auth.deps import Identity, require_admin, require_read
from app.core.db import get_db

router = APIRouter(prefix="/agent-tasks", tags=["Agent 任务"])
ReadDep = Annotated[Identity, Depends(require_read)]
AdminDep = Annotated[Identity, Depends(require_admin)]
DbDep = Annotated[Session, Depends(get_db)]


def _owner(identity: Identity) -> str:
    return identity.user_id or identity.label


def _graph_out(db: Session, task) -> GraphOut:  # noqa: ANN001
    data = service.graph(db, task=task)
    return GraphOut(
        task=TaskOut.from_row(data["task"]),
        goals=[GoalOut.from_row(row) for row in data["goals"]],
        intents=[IntentOut.from_row(row) for row in data["intents"]],
        runs=[RunOut.from_row(row) for row in data["runs"]],
    )


@router.get("", response_model=list[TaskOut], summary="任务列表")
def list_tasks(_identity: ReadDep, db: DbDep) -> list[TaskOut]:
    return [TaskOut.from_row(row) for row in service.list_tasks(db, created_by=_owner(_identity))]


@router.post("", response_model=GraphOut, status_code=201, summary="新建任务")
def create_task(payload: AgentTaskIn, identity: AdminDep, db: DbDep) -> GraphOut:
    task = service.create_task(
        db,
        created_by=_owner(identity),
        title=payload.title,
        objective=payload.objective,
        scope=payload.scope,
    )
    return _graph_out(db, task)


@router.get("/{task_id}", response_model=GraphOut, summary="任务图")
def get_task(task_id: str, identity: ReadDep, db: DbDep) -> GraphOut:
    return _graph_out(db, service.get_task(db, task_id=task_id, created_by=_owner(identity)))


@router.post("/{task_id}/goals", response_model=GoalOut, status_code=201, summary="加目标")
def add_goal(task_id: str, payload: AgentGoalIn, identity: AdminDep, db: DbDep) -> GoalOut:
    task = service.get_task(db, task_id=task_id, created_by=_owner(identity))
    return GoalOut.from_row(service.add_goal(db, task=task, text=payload.text))


@router.post("/{task_id}/intents", response_model=IntentOut, status_code=201, summary="加意图")
def add_intent(task_id: str, payload: AgentIntentIn, identity: AdminDep, db: DbDep) -> IntentOut:
    task = service.get_task(db, task_id=task_id, created_by=_owner(identity))
    return IntentOut.from_row(
        service.add_intent(
            db,
            task=task,
            summary=payload.summary,
            goal_id=payload.goalId,
            priority=payload.priority,
        )
    )


@router.post("/{task_id}/intents/{intent_id}/finish", response_model=IntentOut, summary="写回意图")
def finish_intent(
    task_id: str, intent_id: str, payload: IntentFinishIn, identity: AdminDep, db: DbDep
) -> IntentOut:
    service.get_task(db, task_id=task_id, created_by=_owner(identity))
    return IntentOut.from_row(
        service.finish_intent(
            db, intent_id=intent_id, status=payload.status, result=payload.result
        )
    )


@router.post("/{task_id}/goals/{goal_id}/prove", response_model=GoalOut, summary="标目标完成")
def prove_goal(
    task_id: str, goal_id: str, payload: GoalProveIn, identity: AdminDep, db: DbDep
) -> GoalOut:
    service.get_task(db, task_id=task_id, created_by=_owner(identity))
    return GoalOut.from_row(service.prove_goal(db, goal_id=goal_id, evidence=payload.evidence))


@router.post("/{task_id}/intents/{intent_id}/cancel", response_model=IntentOut, summary="作废意图")
def cancel_intent(
    task_id: str, intent_id: str, payload: CancelIn, identity: AdminDep, db: DbDep
) -> IntentOut:
    service.get_task(db, task_id=task_id, created_by=_owner(identity))
    return IntentOut.from_row(service.cancel_intent(db, intent_id=intent_id, reason=payload.reason))


@router.post("/{task_id}/run", summary="执行当前 open 意图")
async def run_task(task_id: str, identity: AdminDep, db: DbDep) -> dict[str, int]:
    """把当前 open 意图交给 worker 会话执行（后台，最多 4 条）。

    P2a：不自动生成意图、不自动唤醒 planner；只消费已经排好的队列。
    """
    task = service.get_task(db, task_id=task_id, created_by=_owner(identity))
    queued = len(runner_service.open_intents(db, task))
    if queued:
        asyncio.create_task(
            runner_service.run_open_intents(task_id=task.id, identity=identity)
        )
    return {"queued": queued}


@router.post("/{task_id}/cancel", response_model=TaskOut, summary="取消任务")
def cancel_task(task_id: str, identity: AdminDep, db: DbDep) -> TaskOut:
    return TaskOut.from_row(
        service.cancel_task(db, task=service.get_task(db, task_id=task_id, created_by=_owner(identity)))
    )
