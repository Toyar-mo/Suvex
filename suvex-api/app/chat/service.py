"""对话页的会话与消息服务。

## 这一层负责什么、不负责什么

**负责**：会话 CRUD、消息落库、`seq` 分配的原子性、标题自动生成。
**不负责**：调模型、执行工具。那两件事在 `engine.py` ——
分开是为了让这一层能被单测直接测（不需要模型，也不需要沙箱）。

## ★ `seq` 的分配为什么必须在一个事务里先读后写

`UniqueConstraint("session_id", "seq")` 只在**提交时**报错。如果两个请求
并发往同一会话写消息，两边都读到 `max(seq) = 5`，都写 6 ——
后提交的那个拿到 `IntegrityError`，而那时它已经执行过工具了。

这里的做法：写入前 `SELECT ... FOR UPDATE` 锁住会话行，再取 `max(seq)+1`。
锁的是**会话行**（`chat_sessions`），不是消息表 —— 因为要串行化的是
"同一会话的下一个序号"，而会话行天然是这个串行化点。

⚠️ 代价：同一会话的消息写入是串行的。这是**刻意的**：
   一轮对话本来就是一问一答，并发往同一会话写没有合理语义。

## ★ 为什么标题自动生成放在这里而不是 router

因为它依赖"首条消息的内容"，而那是 `append_user_message` 之后才知道的。
放在 router 里意味着 router 要自己判断"这是不是第一条" —— 那是业务规则，
不是传输层的事。而且定时任务/脚本调这个 service 时也会得到同样的标题。
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.chat.models import MAX_MESSAGE_CHARS, MAX_TITLE_CHARS, ChatMessage, ChatSession
from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.timeutil import now_utc

_log = logging.getLogger(__name__)

#: 自动标题的长度。比 `MAX_TITLE_CHARS` 短一点 ——
#: 80 字作为**自动**标题在侧栏里会被截得只剩前半句，
#: 而用户手动改名时可以到 80 字（那是他自己决定的）。
_AUTO_TITLE_CHARS = 32


def _clip_title(raw: str) -> str:
    """把一段文本压成可当标题的一行。

    做三件事，顺序有意义：
    1. 压掉换行与连续空白 —— 标题是单行元素，带换行会把侧栏顶开；
    2. 去首尾空白（去掉之后才判断空不空）；
    3. 截断并补省略号。
    """
    line = " ".join(raw.split())
    if len(line) <= _AUTO_TITLE_CHARS:
        return line
    return line[:_AUTO_TITLE_CHARS] + "…"


def _clip_body(text: str) -> str:
    """正文超长时**留头**截断。

    ★ 与 `run_shell` 的截断口径一致（只留头）：工具输出/日志的关键信息
      通常在开头（命令回显、错误栈的第一行），而结尾可能只是一堆重复行。
      反过来的口径（留尾）会把"它到底执行了什么"截掉 —— 那正是复核时
      最需要看到的。
    """
    if len(text) <= MAX_MESSAGE_CHARS:
        return text
    head = text[: MAX_MESSAGE_CHARS - 200]
    return f"{head}\n\n…（另有 {len(text) - len(head)} 字已截断）"


def create_session(db: Session, *, created_by: str, title: str | None = None) -> ChatSession:
    """新建一个空会话。"""
    session = ChatSession(
        id=new_id("chat"),
        title=(title or "")[:MAX_TITLE_CHARS],
        created_by=created_by,
        message_count=0,
    )
    db.add(session)
    db.flush()
    return session


def list_sessions(
    db: Session, *, created_by: str, include_archived: bool = False
) -> list[ChatSession]:
    """某人的会话列表，最近更新的在前。

    ★ `created_by` 是**必须**的过滤条件，不是可选优化。
      不加它，任何登录用户都能看到别人的对话 —— 而对话里可能有
      目标地址、凭据、内部路径。这是这一层最容易漏的一个条件，
      所以参数是必填而非默认。
    """
    stmt = select(ChatSession).where(ChatSession.created_by == created_by)
    if not include_archived:
        stmt = stmt.where(ChatSession.archived_at.is_(None))
    stmt = stmt.order_by(ChatSession.updated_at.desc())
    return list(db.scalars(stmt))


def get_session_or_404(db: Session, *, session_id: str, created_by: str) -> ChatSession:
    """按 id 取会话，**并校验归属**。

    ★ 归属不符时返回 404 而不是 403：区分"存在但不是你的"会告诉
      探测者哪些 id 是真的。与契约 §3.1「登录失败不区分用户不存在与
      口令错」是同一条理由。
    """
    session = db.get(ChatSession, session_id)
    if session is None or session.created_by != created_by:
        raise ApiError.not_found("这个对话")
    return session


def rename_session(db: Session, *, session: ChatSession, title: str) -> ChatSession:
    """改标题。空标题回 400 —— 空标题会让侧栏出现一条看不见的条目。"""
    cleaned = " ".join(title.split())[:MAX_TITLE_CHARS]
    if not cleaned:
        raise ApiError.validation("标题不能为空。", {"title": "至少写一个字"})
    session.title = cleaned
    session.updated_at = now_utc()
    db.flush()
    return session


def archive_session(db: Session, *, session: ChatSession, archived: bool = True) -> ChatSession:
    """归档/取消归档。**不物理删除**（理由见 models 里 `archived_at` 注释）。"""
    session.archived_at = now_utc() if archived else None
    session.updated_at = now_utc()
    db.flush()
    return session


def _next_seq(db: Session, *, session: ChatSession) -> int:
    """取这个会话的下一个序号。

    ★ 先用 `SELECT ... FOR UPDATE` 锁住会话行。见模块 docstring。
      注意 `with_for_update()` 在 SQLite 上是 no-op —— 而 SQLite 的写本身
      是串行的，所以那条路径上没有并发问题（代价是并发性能，可接受）。
    """
    db.execute(
        select(ChatSession.id).where(ChatSession.id == session.id).with_for_update()
    ).scalar_one()
    current = db.scalar(
        select(func.max(ChatMessage.seq)).where(ChatMessage.session_id == session.id)
    )
    return (current or 0) + 1


def _append(
    db: Session,
    *,
    session: ChatSession,
    role: str,
    content: str,
    reasoning: str | None = None,
    tool_name: str | None = None,
    tool_args: dict | None = None,
    tool_failed: bool | None = None,
) -> ChatMessage:
    """插入一条消息并维护会话的冗余计数与更新时间。

    所有写入都必须经过这里 —— 那是 `message_count` 不会漂移的唯一保证。
    """
    message = ChatMessage(
        id=new_id("msg"),
        session_id=session.id,
        seq=_next_seq(db, session=session),
        role=role,
        content=_clip_body(content),
        reasoning=reasoning,
        tool_name=tool_name,
        tool_args=tool_args,
        tool_failed=tool_failed,
    )
    db.add(message)

    session.message_count = (session.message_count or 0) + 1
    session.updated_at = now_utc()

    # ★ 首条 user 消息用来定标题。判据是"seq 等于 1 且角色是 user"，
    #   而不是"标题为空" —— 后者在用户手动建了空标题会话后会失效，
    #   于是标题永远生不出来。
    if message.seq == 1 and role == "user" and not session.title:
        session.title = _clip_title(content)

    db.flush()
    return message


def append_user_message(db: Session, *, session: ChatSession, content: str) -> ChatMessage:
    """落一条用户消息。"""
    return _append(db, session=session, role="user", content=content)


def append_assistant_message(
    db: Session,
    *,
    session: ChatSession,
    content: str,
    reasoning: str | None = None,
) -> ChatMessage:
    """落一条模型回复。

    ⚠️ 正文可以是空串：模型有时**只**产出工具调用、不写任何话。
      那种情况下这条消息仍有意义（它保住了"模型在这一轮说过什么"的顺序），
      所以不拒绝空串。界面按 `content === ''` 渲染成"正在调用工具…"。
    """
    return _append(db, session=session, role="assistant", content=content, reasoning=reasoning)


def append_tool_message(
    db: Session,
    *,
    session: ChatSession,
    tool_name: str,
    tool_args: dict | None,
    content: str,
    failed: bool = False,
) -> ChatMessage:
    """落一条工具执行记录。"""
    return _append(
        db,
        session=session,
        role="tool",
        content=content,
        tool_name=tool_name,
        tool_args=tool_args,
        tool_failed=failed,
    )


def list_messages(db: Session, *, session: ChatSession, limit: int | None = None) -> list[ChatMessage]:
    """按 seq 取消息。

    `limit` 给定时取**最近** N 条，但返回仍是升序 ——
    "最近 50 条按时间正序"是聊天界面的标准语义。
    """
    stmt = select(ChatMessage).where(ChatMessage.session_id == session.id)
    if limit is not None:
        stmt = stmt.order_by(ChatMessage.seq.desc()).limit(limit)
        rows = list(db.scalars(stmt))
        rows.reverse()
        return rows
    return list(db.scalars(stmt.order_by(ChatMessage.seq.asc())))


def to_wire(messages: list[ChatMessage]) -> list[dict]:
    """消息 → 前端能直接吃的 dict。

    在这里做一次转换而不是让 router 拼，是为了让 `engine` 里的
    增量推送与 `router` 的全量拉取**用同一段代码**产出同一种形状。
    """
    from app.chat.schemas import ChatMessageOut

    return [ChatMessageOut.model_validate(m).model_dump(by_alias=True) for m in messages]


__all__ = [
    "append_assistant_message",
    "append_tool_message",
    "append_user_message",
    "archive_session",
    "create_session",
    "get_session_or_404",
    "list_messages",
    "list_sessions",
    "rename_session",
    "to_wire",
]
