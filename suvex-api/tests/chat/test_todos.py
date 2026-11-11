"""会话级计划（v1.89）：`chat_todos` + 三个工具 + 提示词注入。

## 验收点

1. 工具真的写库、序号单调、状态可改（`_execute_tool` 走完整路径）；
2. 计划**每轮重新渲染**进系统提示词 —— 这是它相对"模型上一轮说的话"的
   全部价值。没有条目时提示词逐字不变（否则每个会话都要付这段 token）。
"""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy.orm import Session

from app.chat import engine, prompts
from app.chat import service as chat_service
from app.chat import todos as todo_store
from tests.test_chat import ADMIN


def _ctx(db: Session):  # noqa: ANN202 - 测试内部小工具
    session = chat_service.create_session(db, created_by="user_admin")
    db.commit()
    return engine._ToolContext(db=db, session=session, identity=ADMIN)


async def test_create_todo_numbers_steps_in_order(db_session: Session) -> None:
    ctx = _ctx(db_session)
    first, failed1 = await engine._execute_tool(
        ctx, "create_todo", {"text": "枚举 /api 下未鉴权的端点"}
    )
    second, failed2 = await engine._execute_tool(
        ctx, "create_todo", {"text": "对命中的端点做对照验证"}
    )

    assert not failed1 and not failed2
    assert "#1" in first
    assert "#2" in second

    out, _failed = await engine._execute_tool(ctx, "list_todos", {})
    assert "#1" in out and "#2" in out
    assert "[pending]" in out


async def test_update_todo_changes_status_and_reports_unknown_seq(db_session: Session) -> None:
    ctx = _ctx(db_session)
    await engine._execute_tool(ctx, "create_todo", {"text": "第一步"})

    out, failed = await engine._execute_tool(
        ctx, "update_todo", {"seq": 1, "status": "in_progress"}
    )
    assert not failed, out
    assert "[in_progress]" in out or "in_progress" in out

    missing, missing_failed = await engine._execute_tool(
        ctx, "update_todo", {"seq": 9, "status": "done"}
    )
    assert missing_failed
    assert "list_todos" in missing


async def test_invalid_status_is_rejected_with_the_allowed_set(db_session: Session) -> None:
    ctx = _ctx(db_session)
    await engine._execute_tool(ctx, "create_todo", {"text": "第一步"})

    out, failed = await engine._execute_tool(
        ctx, "update_todo", {"seq": 1, "status": "finished"}
    )

    assert failed
    assert "pending" in out and "in_progress" in out and "done" in out


def test_prompt_renders_the_plan_only_when_it_exists() -> None:
    """没有计划时逐字不变；有计划时要带上序号与状态。"""
    plain = prompts.build_system_prompt(native_tools=False)

    plan = [
        SimpleNamespace(seq=1, status="done", text="枚举端点"),
        SimpleNamespace(seq=2, status="pending", text="验证越权"),
    ]
    with_plan = prompts.build_system_prompt(native_tools=False, todos=plan)

    assert "本次对话的计划" not in plain
    assert "本次对话的计划" in with_plan
    assert "#2 [pending] 验证越权" in with_plan
    assert "1 done / 0 in_progress / 1 pending" in with_plan


async def test_system_prompt_reads_todos_from_the_database(db_session: Session) -> None:
    """★ 关键接线：计划存在库里，提示词每轮去库里读 —— 不是读上一轮的话。"""
    ctx = _ctx(db_session)
    await engine._execute_tool(ctx, "create_todo", {"text": "先看黑板"})
    db_session.commit()

    prompt = engine._system_prompt_for(ctx.session, db=db_session)

    assert "先看黑板" in prompt
    assert "#1 [pending]" in prompt


def test_create_todo_refuses_past_the_cap(db_session: Session) -> None:
    """到上限明确拒绝 —— 静默丢弃会让模型以为记上了。"""
    session = chat_service.create_session(db_session, created_by="user_admin")
    db_session.commit()
    for index in range(todo_store.MAX_TODOS):
        todo_store.create_todo(db_session, session_id=session.id, text=f"step {index}")
    db_session.commit()

    assert todo_store.create_todo(db_session, session_id=session.id, text="overflow") is None


def test_todo_endpoint_lists_the_plan(ready_client, db_session: Session) -> None:  # noqa: ANN001
    """前端唯一的数据来源：`GET /chat/sessions/{id}/todos`。

    ★ 没有计划时必须返回 `[]` 而不是 404 —— "还没写计划"是正常状态。
    """
    # 走端点建会话：`created_by` 由登录身份写入，直接造行容易和 `_owner` 对不上。
    created = ready_client.post("/api/v1/chat/sessions", json={"title": "计划页"})
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]

    empty = ready_client.get(f"/api/v1/chat/sessions/{session_id}/todos")
    assert empty.status_code == 200, empty.text
    assert empty.json() == []

    todo_store.create_todo(db_session, session_id=session_id, text="先枚举端点")
    db_session.commit()

    listed = ready_client.get(f"/api/v1/chat/sessions/{session_id}/todos")
    body = listed.json()
    assert listed.status_code == 200, listed.text
    assert body == [{"seq": 1, "text": "先枚举端点", "status": "pending"}]
