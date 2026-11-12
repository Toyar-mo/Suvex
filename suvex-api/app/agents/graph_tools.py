"""聊天工具：让角色真正驱动执行图（P1）。

planner 建任务/目标/意图；worker 领取意图、执行后用 finish 写回；主 agent
看图和标目标完成。工具只做参数解析与所有权检查，状态机在 graph_service。
"""

from __future__ import annotations

from typing import Any

from app.agents import graph_service as service


def _owner(ctx: Any) -> str:
    return ctx.session.created_by


async def create_agent_task(ctx: Any, args: dict[str, Any]) -> str:
    task = service.create_task(
        ctx.db,
        created_by=_owner(ctx),
        title=str(args.get("title") or "").strip(),
        objective=str(args.get("objective") or "").strip(),
        scope=str(args.get("scope") or "").strip(),
    )
    return f"任务已创建：`{task.id}`（{task.title}）。下一步用 `add_agent_goal` 拆目标。"


async def add_agent_goal(ctx: Any, args: dict[str, Any]) -> str:
    task = service.get_task(
        ctx.db, task_id=str(args.get("task_id") or "").strip(), created_by=_owner(ctx)
    )
    goal = service.add_goal(ctx.db, task=task, text=str(args.get("text") or ""))
    return f"目标已加入：`{goal.id}`（#{goal.seq}）。"


async def add_agent_intent(ctx: Any, args: dict[str, Any]) -> str:
    task = service.get_task(
        ctx.db, task_id=str(args.get("task_id") or "").strip(), created_by=_owner(ctx)
    )
    goal_id = str(args.get("goal_id") or "").strip() or None
    intent = service.add_intent(
        ctx.db,
        task=task,
        summary=str(args.get("summary") or ""),
        goal_id=goal_id,
        priority=int(args.get("priority") or 5),
    )
    return f"意图已加入：`{intent.id}`（优先级 {intent.priority}）。worker 用 `claim_agent_intent` 领取。"


async def list_agent_graph(ctx: Any, args: dict[str, Any]) -> str:
    task = service.get_task(
        ctx.db, task_id=str(args.get("task_id") or "").strip(), created_by=_owner(ctx)
    )
    data = service.graph(ctx.db, task=task)
    lines = [f"任务 `{task.id}`：{task.title}（{task.status}）"]
    lines.append("目标：")
    for row in data["goals"]:
        lines.append(f"- `{row.id}` #{row.seq} [{row.status}] {row.text}")
    lines.append("意图：")
    for row in data["intents"]:
        lines.append(
            f"- `{row.id}` [{row.status}] p{row.priority} {row.summary}"
            + (f" → {row.result[:200]}" if row.result else "")
        )
    return "\n".join(lines)


async def claim_agent_intent(ctx: Any, args: dict[str, Any]) -> str:
    intent = service.claim_intent(
        ctx.db,
        intent_id=str(args.get("intent_id") or "").strip(),
        worker_session_id=ctx.session.id,
    )
    return (
        f"已领取意图 `{intent.id}`：{intent.summary}\n"
        "完成或判断走不通后用 `finish_agent_intent` 写回；不要领取第二条。"
    )


async def finish_agent_intent(ctx: Any, args: dict[str, Any]) -> str:
    intent = service.finish_intent(
        ctx.db,
        intent_id=str(args.get("intent_id") or "").strip(),
        status=str(args.get("status") or "").strip(),
        result=str(args.get("result") or ""),
        worker_session_id=ctx.session.id,
    )
    return f"意图 `{intent.id}` 已写回，状态 {intent.status}。"


async def prove_agent_goal(ctx: Any, args: dict[str, Any]) -> str:
    goal = service.prove_goal(
        ctx.db,
        goal_id=str(args.get("goal_id") or "").strip(),
        evidence=str(args.get("evidence") or ""),
    )
    return f"目标 `{goal.id}` 已标为 {goal.status}。最后一个目标完成时任务会自动结束。"


__all__ = [
    "add_agent_goal",
    "add_agent_intent",
    "claim_agent_intent",
    "create_agent_task",
    "finish_agent_intent",
    "list_agent_graph",
    "prove_agent_goal",
]
