"""执行图 API 模型（P1）。"""

from __future__ import annotations

from pydantic import Field

from app.agents.graph_models import AgentGoal, AgentIntent, AgentRun, AgentTask
from app.core.schemas import CamelModel, StrictModel


class AgentTaskIn(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    objective: str = Field(default="", max_length=20000)
    scope: str = Field(default="", max_length=10000)


class AgentGoalIn(StrictModel):
    text: str = Field(min_length=1, max_length=2000)


class AgentIntentIn(StrictModel):
    summary: str = Field(min_length=1, max_length=2000)
    goalId: str | None = None
    priority: int = Field(default=5, ge=0, le=10)


class IntentFinishIn(StrictModel):
    status: str = Field(pattern="^(done|exhausted|blocked|stopped)$")
    result: str = Field(default="", max_length=20000)


class GoalProveIn(StrictModel):
    evidence: str = Field(min_length=1, max_length=20000)


class CancelIn(StrictModel):
    reason: str = Field(default="", max_length=2000)


class GoalOut(CamelModel):
    id: str
    seq: int
    text: str
    status: str
    evidence: str

    @classmethod
    def from_row(cls, row: AgentGoal) -> GoalOut:
        return cls(id=row.id, seq=row.seq, text=row.text, status=row.status, evidence=row.evidence)


class IntentOut(CamelModel):
    id: str
    goalId: str | None
    seq: int
    summary: str
    status: str
    priority: int
    workerSessionId: str | None
    result: str

    @classmethod
    def from_row(cls, row: AgentIntent) -> IntentOut:
        return cls(
            id=row.id,
            goalId=row.goal_id,
            seq=row.seq,
            summary=row.summary,
            status=row.status,
            priority=row.priority,
            workerSessionId=row.worker_session_id,
            result=row.result,
        )


class RunOut(CamelModel):
    id: str
    intentId: str | None
    role: str
    sessionId: str | None
    status: str
    output: str

    @classmethod
    def from_row(cls, row: AgentRun) -> RunOut:
        return cls(
            id=row.id,
            intentId=row.intent_id,
            role=row.role,
            sessionId=row.session_id,
            status=row.status,
            output=row.output,
        )


class TaskOut(CamelModel):
    id: str
    title: str
    objective: str
    scope: str
    status: str
    createdAt: str
    updatedAt: str

    @classmethod
    def from_row(cls, row: AgentTask) -> TaskOut:
        return cls(
            id=row.id,
            title=row.title,
            objective=row.objective,
            scope=row.scope,
            status=row.status,
            createdAt=row.created_at.isoformat() if row.created_at else "",
            updatedAt=row.updated_at.isoformat() if row.updated_at else "",
        )


class ScanLinkOut(CamelModel):
    id: str
    target: str
    status: str


class GraphOut(CamelModel):
    task: TaskOut
    goals: list[GoalOut]
    intents: list[IntentOut]
    runs: list[RunOut]
    scans: list[ScanLinkOut] = []
