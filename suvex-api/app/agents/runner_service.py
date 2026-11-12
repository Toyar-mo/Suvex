"""任务执行器（P2b）：worker 队列 + planner 自动补意图。

一轮执行的循环：

1. 有 open 意图 → 交给 worker 会话执行（最多 4 条/轮）；
2. 队列空了但还有 open goal → 起 planner 会话补意图；
3. planner 没补出新意图，或任务已完成/取消 → 结束。

这是 ARTeX "planner 被图变化唤醒" 的最小版本：没有事件总线，但队列一空
就同步问一次 planner。planner 仍然只做规划，不执行；worker 只执行一条。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents import graph_service
from app.agents.graph_models import AgentGoal, AgentIntent, AgentTask

log = logging.getLogger(__name__)

MAX_INTENTS_PER_ROUND = 4
MAX_INTENTS_PER_RUN = 12
MAX_PLANNER_ROUNDS = 3
WORKER_TOOLS = (
    "run_shell",
    "browser",
    "verify_http",
    "list_scans",
    "create_scan",
    "list_findings",
    "list_assets",
    "load_skill",
    "load_recipe",
    "list_skills",
    "record_fact",
    "record_coverage",
    "list_facts",
    "create_todo",
    "update_todo",
    "claim_agent_intent",
    "finish_agent_intent",
    "prove_agent_goal",
)
PLANNER_TOOLS = (
    "list_agent_graph",
    "add_agent_intent",
    "prove_agent_goal",
    "list_scans",
    "list_findings",
    "list_assets",
    "list_facts",
    "list_todos",
    "create_todo",
    "update_todo",
)


def open_intents(
    db: Session, task: AgentTask, limit: int = MAX_INTENTS_PER_ROUND
) -> list[AgentIntent]:
    return list(
        db.execute(
            select(AgentIntent)
            .where(AgentIntent.task_id == task.id, AgentIntent.status == "open")
            .order_by(AgentIntent.priority.desc(), AgentIntent.seq)
            .limit(limit)
        ).scalars()
    )


def _has_open_goals(db: Session, task: AgentTask) -> bool:
    count = db.scalar(
        select(func.count())
        .select_from(AgentGoal)
        .where(AgentGoal.task_id == task.id, AgentGoal.status == "open")
    )
    return bool(count)


def _graph_text(db: Session, task: AgentTask) -> str:
    data = graph_service.graph(db, task=task)
    goals = "\n".join(f"- {g.id} [{g.status}] {g.text}" for g in data["goals"]) or "（无）"
    intents = "\n".join(
        f"- {i.id} [{i.status}] p{i.priority} {i.summary}" for i in data["intents"]
    ) or "（无）"
    return f"目标：\n{goals}\n意图：\n{intents}"


async def _run_planner(
    db: Session,
    task: AgentTask,
    identity: Any,
    turn_runner: Any,
) -> int:
    """让 planner 补意图；返回新增的 open 意图数。"""
    from app.chat import engine
    from app.chat import service as chat_service

    before = db.scalar(
        select(func.count())
        .select_from(AgentIntent)
        .where(AgentIntent.task_id == task.id, AgentIntent.status == "open")
    ) or 0
    planner = chat_service.create_session(
        db,
        created_by=task.created_by,
        title=f"[planner] {task.title[:80]}",
        agent_task_id=task.id,
    )
    planner.persona = "planner"
    planner.require_approval = False
    db.commit()

    prompt = (
        "你是任务执行器的规划轮，**不要执行**，只读图并补意图。\n"
        f"任务 id：`{task.id}`\n"
        f"目标：{task.objective or '—'}\n"
        f"范围/约束：{task.scope or '—'}\n\n"
        f"当前图：\n{_graph_text(db, task)}\n\n"
        "要求：\n"
        "1. 已有 open/running 意图覆盖的方向不要重复加；\n"
        "2. 只补真正未覆盖、能推进目标的新方向，最多 4 条；\n"
        "3. 用 `add_agent_intent` 写入 task_id；summary 写具体目标和做法；\n"
        "4. 若已有证据足以证明某个目标达成，用 `prove_agent_goal` 标 met；\n"
        "5. 若所有目标都已覆盖且无新方向，不要加意图，直接结束。"
    )

    async def default_runner(session: Any) -> None:
        await engine.run_turn_durable(
            db,
            session=session,
            content=prompt,
            identity=identity,
            explicit_tools=list(PLANNER_TOOLS),
            allowed_tools=set(PLANNER_TOOLS),
        )

    if turn_runner is None:
        await default_runner(planner)
    else:
        await turn_runner(planner, prompt)

    db.expire_all()
    after = db.scalar(
        select(func.count())
        .select_from(AgentIntent)
        .where(AgentIntent.task_id == task.id, AgentIntent.status == "open")
    ) or 0
    return max(0, int(after - before))


async def _run_worker(
    db: Session,
    task: AgentTask,
    intent: AgentIntent,
    identity: Any,
    turn_runner: Any,
) -> bool:
    """执行一条意图；返回是否成功启动。"""
    from app.chat import engine
    from app.chat import service as chat_service

    worker_session_id: str | None = None
    try:
        worker = chat_service.create_session(
            db,
            created_by=task.created_by,
            title=f"[worker] {intent.summary[:80]}",
            agent_task_id=task.id,
        )
        worker.persona = "worker"
        worker.require_approval = False
        db.commit()
        worker_session_id = worker.id

        graph_service.claim_intent(db, intent_id=intent.id, worker_session_id=worker.id)

        prompt = (
            "你已经被任务执行器分配了这一条意图，**不要再次 claim**。\n"
            f"任务：{task.title}\n"
            f"最终目标：{task.objective or '—'}\n"
            f"范围/约束：{task.scope or '—'}\n"
            f"意图 `{intent.id}`：{intent.summary}\n\n"
            "直接执行这一条意图。结束时必须调用 `finish_agent_intent` 写回：\n"
            "- done：完成，result 写真实证据与结论；\n"
            "- exhausted：合理手段走完仍无结果；\n"
            "- blocked：环境/工具/权限阻塞，说明缺什么；\n"
            "- stopped：被人工停止。\n"
            "不要领取第二条意图，不要修改任务/目标状态。"
        )

        if turn_runner is None:
            async def default_runner(session: Any) -> None:
                await engine.run_turn_durable(
                    db,
                    session=session,
                    content=prompt,
                    identity=identity,
                    explicit_tools=list(WORKER_TOOLS),
                    allowed_tools=set(WORKER_TOOLS),
                )

            await default_runner(worker)
        else:
            await turn_runner(worker, prompt)
        return True
    except asyncio.CancelledError:
        if worker_session_id:
            try:
                graph_service.finish_intent(
                    db,
                    intent_id=intent.id,
                    status="stopped",
                    result="任务执行器被取消",
                    worker_session_id=worker_session_id,
                )
            except Exception:  # noqa: BLE001
                db.rollback()
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("worker 执行意图 %s 失败", intent.id, exc_info=True)
        db.rollback()
        try:
            graph_service.finish_intent(
                db,
                intent_id=intent.id,
                status="blocked",
                result=f"执行器失败：{type(exc).__name__}: {exc}",
                worker_session_id=worker_session_id,
            )
        except Exception:  # noqa: BLE001
            db.rollback()
        return False
    finally:
        try:
            db.expire_all()
        except Exception:  # noqa: BLE001
            pass


async def run_task(
    *,
    task_id: str,
    identity: Any,
    turn_runner: Any = None,
) -> int:
    """后台执行整个任务；返回实际启动的 worker 数。"""
    from app.core.db import get_session_factory

    db = get_session_factory()()
    started = 0
    try:
        task = db.get(AgentTask, task_id)
        if task is None:
            return 0
        for _ in range(MAX_PLANNER_ROUNDS):
            task = db.get(AgentTask, task_id)
            if task is None or task.status in {"completed", "cancelled"}:
                break

            intents = open_intents(db, task)
            if not intents:
                if not _has_open_goals(db, task):
                    break
                added = await _run_planner(db, task, identity, turn_runner)
                if added == 0:
                    break
                intents = open_intents(db, task)

            for intent in intents:
                if started >= MAX_INTENTS_PER_RUN:
                    break
                if await _run_worker(db, task, intent, identity, turn_runner):
                    started += 1

            if started >= MAX_INTENTS_PER_RUN:
                break
        return started
    finally:
        db.close()


#: 兼容 P2a 的公开名字。
run_open_intents = run_task

__all__ = ["PLANNER_TOOLS", "WORKER_TOOLS", "open_intents", "run_open_intents", "run_task"]
