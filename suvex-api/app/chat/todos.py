"""会话级计划（v1.89）：读写 `chat_todos`，并渲染进系统提示词。

## 它替代的是什么

此前"计划"只活在模型的一段话里。引擎得靠启发式判断（"接下来我打算…"）
决定这句话是**计划**还是**结论**，然后决定打回还是收尾。猜错的两种形态
都真实发生过：把阶段性计划当收尾、把收尾当计划。

这张表不猜：模型自己写、自己改状态，引擎每轮把当前计划渲染进提示词。
"现在到哪一步"于是是一次查询，不是一次阅读理解。

## 边界

* 只服务对话 —— 没有负责人、优先级、依赖关系。扫描侧那套 `create_todo`
  是单次扫描的内存待办，两者用途不同，不要合并；
* 上限 50 条。计划表不是日志：写满说明该拆会话了，而不是该扩容。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.chat.models import TODO_STATUSES, ChatTodo
from app.core.ids import new_id
from app.core.timeutil import now_utc

#: 一条计划的最大长度。超过说明它该拆成两条。
MAX_TODO_TEXT = 500

#: 一个会话最多记这么多步。到顶时 `create_todo` 明确拒绝，而不是静默丢弃 ——
#: 静默丢弃的表现是"模型以为记上了，下一轮又找不到"。
MAX_TODOS = 50


def list_todos(db: Session, session_id: str) -> list[ChatTodo]:
    """按序号升序返回会话的计划。"""
    return list(
        db.execute(
            select(ChatTodo)
            .where(ChatTodo.session_id == session_id)
            .order_by(ChatTodo.seq)
        ).scalars()
    )


def create_todo(db: Session, *, session_id: str, text: str) -> ChatTodo | None:
    """追加一步。超过 `MAX_TODOS` 返回 `None`（调用方负责告诉模型）。"""
    count = db.execute(
        select(func.count()).select_from(ChatTodo).where(ChatTodo.session_id == session_id)
    ).scalar_one()
    if count >= MAX_TODOS:
        return None

    # 序号取 max+1 而不是 count+1：删掉中间某条后，count 会让新条目撞上
    # 一个仍然存在的序号（唯一约束会直接报错）。
    last = db.execute(
        select(func.max(ChatTodo.seq)).where(ChatTodo.session_id == session_id)
    ).scalar()
    seq = int(last or 0) + 1

    moment = now_utc()
    row = ChatTodo(
        id=new_id("todo"),
        session_id=session_id,
        seq=seq,
        text=text.strip()[:MAX_TODO_TEXT],
        status="pending",
        created_at=moment,
        updated_at=moment,
    )
    db.add(row)
    db.flush()
    return row


def update_todo(
    db: Session,
    *,
    session_id: str,
    seq: int,
    status: str | None = None,
    text: str | None = None,
) -> ChatTodo | None:
    """按序号改状态/文字。找不到该序号返回 `None`。"""
    row = db.execute(
        select(ChatTodo).where(ChatTodo.session_id == session_id, ChatTodo.seq == seq)
    ).scalar_one_or_none()
    if row is None:
        return None
    if status is not None:
        if status not in TODO_STATUSES:
            raise ValueError(f"status 只能是 {' / '.join(TODO_STATUSES)}")
        row.status = status
    if text is not None and text.strip():
        row.text = text.strip()[:MAX_TODO_TEXT]
    row.updated_at = now_utc()
    db.flush()
    return row


def render_todos(todos: Iterable[Any]) -> str:
    """渲染成提示词里的计划段。没有条目时返回空串（提示词逐字不变）。"""
    rows = list(todos)
    if not rows:
        return ""

    done = sum(1 for row in rows if row.status == "done")
    doing = sum(1 for row in rows if row.status == "in_progress")
    lines = [f"- #{row.seq} [{row.status}] {row.text}" for row in rows]
    all_done = done == len(rows)
    tail = (
        "\n**计划已全部完成。** 不要再开新的动作：要么用 `create_todo` 明确开始"
        "一个新计划，要么直接给用户最终答复。不要在旧计划完成后默默接着做别的活。\n"
        if all_done
        else ""
    )
    return (
        "\n## 本次对话的计划（结构化，跨轮保留）\n\n"
        "干活前先看这里；完成后用 `update_todo` 把状态改成 `done`，"
        "开始动手时改成 `in_progress`。计划变了就改它 —— "
        "**它会随每一轮重新渲染**，所以它比你在上一轮说的话更可信。\n\n"
        + "\n".join(lines)
        + f"\n\n（{len(rows)} 步：{done} done / {doing} in_progress / "
        f"{len(rows) - done - doing} pending）\n"
        + tail
    )


__all__ = [
    "MAX_TODOS",
    "MAX_TODO_TEXT",
    "create_todo",
    "list_todos",
    "render_todos",
    "update_todo",
]
