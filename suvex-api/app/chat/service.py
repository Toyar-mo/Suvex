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

## ★ v0.94 新增的三块：设置、审批、附件

三者都放在这一层而不是 router，判据与下面的标题生成相同：
**它们是业务规则，不是传输层的事**。而"设置怎么合并"（缺省不等于清空）
与"审批能不能重复点"这类规则如果写在 router 里，那么命令行/脚本调同一套
逻辑时就会漏掉 —— 那正是本项目反复强调的"两条路径两种行为"。

## ★ 为什么标题自动生成放在这里而不是 router

因为它依赖"首条消息的内容"，而那是 `append_user_message` 之后才知道的。
放在 router 里意味着 router 要自己判断"这是不是第一条" —— 那是业务规则，
不是传输层的事。而且定时任务/脚本调这个 service 时也会得到同样的标题。
"""

from __future__ import annotations

import contextlib
import json
import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.chat.models import (
    MAX_MESSAGE_CHARS,
    MAX_PROJECT_CHARS,
    MAX_TITLE_CHARS,
    ChatMessage,
    ChatSession,
)
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


# ══════════════════════════════════════════════════════════
# 对话设置（v0.94）
# ══════════════════════════════════════════════════════════


def update_settings(db: Session, *, session: ChatSession, payload: Any) -> ChatSession:
    """按**部分提交**语义改会话设置。

    ## ★ 判"传没传"用 `model_fields_set`，不是判值是否为 None

    这个区别是这一层唯一容易写错的地方，而错了之后的表现很隐蔽：

    * 用户点「移出项目」→ 前端发 `{"project": null}` → 如果实现按
      "值为 None 就跳过"，项目**不会被清空**，用户会发现它还在原分组里，
      刷新也没用，而接口返回 200。
    * 用户只改人格 → 前端发 `{"persona": "recon"}` → 如果实现按
      "字段缺省就当 null"，另外五项会被**清空**。

    所以：`"project" in payload.model_fields_set` 决定"要不要动"，
    而它是不是 None 决定"改成什么"。

    ## 两个列表字段的额外规则

    `skillNames` / `mcpServerNames` 显式传 `[]` 与传 `null` 等价 ——
    两种都写成 NULL（"这个对话不挂任何东西"在语义上是一件事）。
    存成空列表会让"有没有配置过"变成三态（NULL / `[]` / `[a]`），
    而没有任何一处需要那个区分。
    """
    fields = payload.model_fields_set

    if "project" in fields:
        # ★ 空串归一成 None：界面上把项目名清空、回车，前端可能发 `""`。
        #   存一个空串会让侧栏出现一个看不见名字的分组。
        raw = payload.project
        cleaned = " ".join((raw or "").split())[:MAX_PROJECT_CHARS]
        session.project = cleaned or None

    if "persona" in fields:
        # `default` 与 `null` 都落成 NULL：它们是同一件事（默认人格）。
        # 存两种值会让 `prompts.render_persona` 与界面各判一次。
        persona = payload.persona
        session.persona = None if persona in (None, "default") else persona

    if "providerId" in fields:
        session.provider_id = payload.providerId or None

    if "requireApproval" in fields and payload.requireApproval is not None:
        session.require_approval = bool(payload.requireApproval)

    if "skillNames" in fields:
        names = payload.skillNames
        session.skill_names = [n for n in names if n] if names else None

    if "mcpServerNames" in fields:
        names = payload.mcpServerNames
        session.mcp_server_names = [n for n in names if n] if names else None

    session.updated_at = now_utc()
    db.flush()
    return session


def mounted_skill_texts(session: ChatSession) -> list[tuple[str, str]]:
    """取这个会话挂载的技能正文，返回 `(技能名, 正文)`。

    ★ 找不到的技能**跳过并记 warning**，不抛异常：技能可能被另一个标签页
      删掉了，而"这个对话少了一篇方法论"不该让用户连话都发不出去。
      但必须能查到 —— 用户勾了三篇却只生效两篇，正是那种
      "界面说挂了、实际没挂"的静默失效。

    ★ 总量截断在 `engine` 那一侧做（`MAX_MOUNTED_SKILL_CHARS`），这里
      只负责取数。一个取数函数不该承担"总共能有多大"这个决策 ——
      否则改预算要改两个文件。
    """
    names = session.skill_names or []
    if not names:
        return []
    from app.kernel.skills import load_skill_text

    out: list[tuple[str, str]] = []
    for name in names:
        text = load_skill_text(name)
        # `load_skill_text` 未命中时返回的是"没有名为 X 的技能包。可用：…"，
        # 而不是空串。用它自己的前缀识别，避免把那条错误当成正文注入。
        if text.startswith("没有名为"):
            _log.warning("对话挂载的技能不存在：%s", name)
            continue
        out.append((name, text))
    return out


# ══════════════════════════════════════════════════════════
# 人工审批（v0.94）
# ══════════════════════════════════════════════════════════


def pending_approvals(db: Session, *, session: ChatSession) -> list[ChatMessage]:
    """这个会话里挂着等确认的工具调用，按 seq 升序。

    ★ 用 `seq` 排序而不是 `created_at`：同一秒内写入的两条消息
      `created_at` 可能相同，而 `seq` 是严格递增的。
    """
    stmt = (
        select(ChatMessage)
        .where(
            ChatMessage.session_id == session.id,
            ChatMessage.approval_status == "pending",
        )
        .order_by(ChatMessage.seq.asc())
    )
    return list(db.scalars(stmt))


def get_message(db: Session, *, session: ChatSession, message_id: str) -> ChatMessage:
    """按 id 取一条消息，**并校验它属于这个会话**。

    ⚠️ 归属校验在这里做而不是让 router 做：审批接口的形状是
       `/chat/sessions/{sid}/messages/{mid}/approve`，两个 id 都在 URL 里。
       只按 message_id 查就能读到**别的会话**的消息 —— 那是越权。
    """
    message = db.get(ChatMessage, message_id)
    if message is None or message.session_id != session.id:
        raise ApiError.not_found("这条消息")
    return message


def mark_awaiting_approval(
    db: Session, *, session: ChatSession, tool_name: str, tool_args: dict | None
) -> ChatMessage:
    """落一条"等待用户确认"的工具消息。

    `content` 写成一句给用户看的话（而不是空串）：前端按
    `approvalStatus === 'pending'` 渲染确认按钮，但按钮旁边要有说明
    "它想做什么"。空内容会让界面上出现一张没有任何说明的卡片。
    """
    args_preview = ""
    if tool_args:
        with contextlib.suppress(TypeError, ValueError):
            rendered = json.dumps(tool_args, ensure_ascii=False)
            args_preview = rendered if len(rendered) <= 300 else rendered[:300] + "…"
    content = f"等待确认后执行 `{tool_name}`。"
    if args_preview:
        content += f"\n参数：{args_preview}"

    message = _append(
        db,
        session=session,
        role="tool",
        content=content,
        tool_name=tool_name,
        tool_args=tool_args,
        tool_failed=None,
    )
    message.approval_status = "pending"
    db.flush()
    return message


def decide_approval(
    db: Session,
    *,
    session: ChatSession,
    message: ChatMessage,
    approve: bool,
    decided_by: str,
) -> ChatMessage:
    """记下审批决定。**只改状态，不执行工具**。

    ★ 执行在 `engine.resume_approved` —— 那需要网关/身份那一整套上下文，
      而这一层刻意不知道那些东西（见模块头 docstring「不负责：调模型、
      执行工具」）。分层在这里多一次调用，换来这一层能被无模型单测直接测。

    ## 四道必须的拒绝

    1. 不是 `pending` → 400。重复点确认是最常见的误操作，而放行意味着
       第二次会**再执行一遍**那个有副作用的工具。
    2. 拒绝时不删消息，状态改 `rejected` —— 用户需要看到"我曾经拒过它"。
    3. `decided_by` 必须非空：记不到人头的审批等于没有审计。
    4. 状态与决定人/时间在同一次 flush 里写，不留"状态改了但没人"的中间态。
    """
    if message.approval_status != "pending":
        raise ApiError.validation(
            "这条调用已经处理过了，不能重复审批。",
            {"messageId": f"当前状态：{message.approval_status}"},
        )
    if not decided_by:
        raise ApiError.validation("审批必须记录是谁决定的。", {"decidedBy": "缺失"})

    message.approval_status = "approved" if approve else "rejected"
    message.approval_decided_by = decided_by
    message.approval_decided_at = now_utc()
    if not approve:
        message.tool_failed = True
        message.content = "用户拒绝了这次调用，工具没有执行。"
    session.updated_at = now_utc()
    db.flush()
    return message


def finish_approved_tool(
    db: Session, *, session: ChatSession, message: ChatMessage, output: str, failed: bool
) -> ChatMessage:
    """把一条已批准的工具消息填上执行结果。

    ★ 只接受 `approved` 状态：`rejected` 的消息内容已经是"用户拒绝了"，
      再写执行结果会把它变成一句自相矛盾的话。
    """
    if message.approval_status != "approved":
        raise ApiError.validation(
            "这条调用没有被批准，不能写入执行结果。",
            {"messageId": f"当前状态：{message.approval_status}"},
        )
    message.content = _clip_body(output)
    message.tool_failed = failed
    session.updated_at = now_utc()
    db.flush()
    return message


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
    "decide_approval",
    "finish_approved_tool",
    "get_message",
    "get_session_or_404",
    "list_messages",
    "list_sessions",
    "mark_awaiting_approval",
    "mounted_skill_texts",
    "pending_approvals",
    "rename_session",
    "to_wire",
    "update_settings",
]
