"""任务执行器（P2a）：一键把 open 意图交给 worker 会话。

P1 只到图；这里补上"点一下真的跑"。仍然是**受控队列**：不自动唤醒
planner、不自动加意图，只执行当前已经存在的 open 意图，最多 4 条。
每条意图：

1. 先原子领取（写 worker_session_id）；
2. 起一个 persona=worker 的独立会话，限制工具；
3. 跑一轮，worker 通过 `finish_agent_intent` 写回；
4. 异常时把意图标 blocked，而不是留下永远 running 的僵尸。

自动 planner 唤醒是 P2b；在此之前，planner 仍由人在对话里跑。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents import graph_service
from app.agents.graph_models import AgentIntent, AgentTask

log = logging.getLogger(__name__)

MAX_INTENTS_PER_RUN = 4
WORKER_TOOLS = (
    "run_shell",
    "browser",
    "verify_http",
    "list_scans",
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


def open_intents(db: Session, task: AgentTask, limit: int = MAX_INTENTS_PER_RUN) -> list[AgentIntent]:
    return list(
        db.execute(
            select(AgentIntent)
            .where(AgentIntent.task_id == task.id, AgentIntent.status == "open")
            .order_by(AgentIntent.priority.desc(), AgentIntent.seq)
            .limit(limit)
        ).scalars()
    )


async def run_open_intents(
    *,
    task_id: str,
    identity: Any,
    turn_runner: Any = None,
) -> int:
    """后台执行：返回实际启动的意图数。调用方负责 `create_task`。"""
    from app.core.db import get_session_factory

    db = get_session_factory()()
    started = 0
    try:
        task = db.get(AgentTask, task_id)
        if task is None or task.status in {"completed", "cancelled"}:
            return 0
        intents = open_intents(db, task)
        for intent in intents:
            worker_session_id: str | None = None
            try:
                # 先建会话再领取：领取要求有 worker 归属，失败时会话可归档。
                from app.chat import service as chat_service

                worker = chat_service.create_session(
                    db,
                    created_by=task.created_by,
                    title=f"[worker] {intent.summary[:80]}",
                )
                worker.persona = "worker"
                worker.require_approval = False
                db.commit()
                worker_session_id = worker.id

                graph_service.claim_intent(
                    db,
                    intent_id=intent.id,
                    worker_session_id=worker.id,
                )
                started += 1

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

                from app.chat import engine

                runner = turn_runner
                if runner is None:
                    async def runner(session: Any) -> None:  # type: ignore[misc]
                        await engine.run_turn_durable(
                            db,
                            session=session,
                            content=prompt,
                            identity=identity,
                            explicit_tools=list(WORKER_TOOLS),
                            allowed_tools=set(WORKER_TOOLS),
                        )

                    await runner(worker)
                else:
                    await runner(worker, prompt)
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
            except Exception as exc:  # noqa: BLE001 - 单条失败不影响后续
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
            finally:
                try:
                    db.expire_all()
                except Exception:  # noqa: BLE001
                    pass
    finally:
        db.close()
    return started
