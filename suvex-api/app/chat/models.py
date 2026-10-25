"""`chat_*` 领域（对话页，v0.93）。

## 它解决的是什么问题

平台已有的每一个入口都是**表单**：建扫描要填六个字段、改技能要开弹窗、
加 MCP 要手写 command 与 args。表单适合「我已经知道自己要做什么」，
不适合「我想先问一句」。

这一页补的是后者：一个能听懂自然语言、并且**真的能把事做了**的对话。

## ★ 为什么消息单独一张表，而不是把 transcript 塞进会话行

会话行上的一个 JSON 列写起来最省事，但它有三条硬伤：

* 一条消息都可能很长（工具输出被截断后仍有几千字），几百轮之后
  单行会涨到 MB 级，而**每次读会话都要把它整个读出来**；
* 无法分页 —— 「只看最近 50 条」需要把全部读进内存再切；
* 无法按消息做查询（比如「这个会话里调过哪些工具」）。

所以拆成 `chat_messages`，会话行只留元信息。

## ★ 为什么 `seq` 是必需的，为什么不用时间戳排序

`core/columns.py:27` 已经记过这件事：同一事务里多条记录的
`now_utc()` 会拿到**完全相同的值**（默认值由应用层给，而是在同一帧里取的）。
一轮对话可能同时落一条 assistant 消息 + 若干条 tool 消息，
它们的时间戳会撞在一起，于是 `ORDER BY created_at` 的顺序是**未定义**的 ——
表现是「工具结果偶尔跑到提问前面」。

`seq` 是会话内单调递增的整数，由应用层在插入时按会话加锁取下一个值。
排序只认它。

## ★ 为什么没有「对话内改设置」的表字段

见 `_tmp/chat/设计-对话页.md` §5：让模型改模型自己的配置，风险面太大。
这一版明确不做，所以模型里也没有对应的东西。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.columns import JsonColumn, UtcDateTime
from app.core.db import Base
from app.core.timeutil import now_utc

#: 角色。只有三种 —— 工具结果并进 `tool` 角色，而不是再拆一个
#: 「工具请求」与「工具结果」两种。理由见下面 `ChatMessage.content` 的注释。
CHAT_ROLES = ("user", "assistant", "tool")

#: 一条消息正文的最大长度。超过就截断（不是拒绝）。
#:
#: 为什么截断而不是拒绝：工具输出可能真的很大（一次 `nmap` 的输出），
#: 而"因为输出太长所以这一轮对话失败"是不可接受的 —— 用户没做错任何事。
#: 截断点保留**头部**（与 `run_shell` 的截断口径一致，见 runner.py 里
#: 那条「只留头」的修复）。
MAX_MESSAGE_CHARS = 60_000

#: 会话标题的最大长度。标题是自动从首条提问生成的，超长就截。
MAX_TITLE_CHARS = 80


class ChatSession(Base):
    """一次对话。"""

    __tablename__ = "chat_sessions"
    __table_args__ = (
        # 列表页的唯一热查询：某人的会话，按更新时间倒序。
        Index("ix_chat_sessions_owner_updated", "created_by", "updated_at"),
        # 归档的会话默认不出现，但要能捞回来。
        Index("ix_chat_sessions_archived", "archived_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    #: 自动生成的标题（取首条 user 消息前 80 字）。
    #: ⚠️ 它可以被用户改名，所以**不能**用来推导任何逻辑。
    title: Mapped[str] = mapped_column(String(MAX_TITLE_CHARS), nullable=False, default="")

    #: 创建者。与其它领域的 `created_by` 同一口径（存 username，不做外键 ——
    #: 账号被删后历史记录仍要能回答"这是谁建的"）。
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)

    #: 软归档。**不物理删除**：对话里可能有"我是怎么定位到这个漏洞的"这种
    #: 事后价值极高的记录，而删掉是不可逆的。归档 = 从默认列表里消失。
    archived_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=now_utc, onupdate=now_utc
    )

    #: 消息数。冗余但必要：列表页要显示"12 条"，而为一个数字
    #: 对每个会话跑一次 COUNT 是 N+1。
    #: ⚠️ 它必须由写消息的那一处同步维护，不能指望它自愈。
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    messages: Mapped[list[ChatMessage]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="ChatMessage.seq",
        lazy="selectin",
    )

    @property
    def archived(self) -> bool:
        """给 `ChatSessionOut.archived` 读的派生属性。

        ★ 为什么要有它，而不是让 schema 直接读 `archivedAt`：
          契约里给前端的字段是 `archived: bool`（前端只关心"在不在归档区"），
          而库里存的是时刻。让前端自己判 `archivedAt !== null` 会把
          「归档」的判据分散到每一个调用点 —— 将来若改成"归档 30 天后
          自动清空"，前端那些判断就全错了，而且不会报错。
        """
        return self.archived_at is not None

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<ChatSession id={self.id!r} title={self.title!r} n={self.message_count}>"


class ChatMessage(Base):
    """会话里的一条消息。"""

    __tablename__ = "chat_messages"
    __table_args__ = (
        # ★ 顺序的唯一依据。`(session_id, seq)` 唯一 —— 让"两条消息同一个序号"
        #   从数据库层就不可能，而不是靠应用层小心翼翼。
        UniqueConstraint("session_id", "seq", name="chat_message_session_seq"),
        CheckConstraint(
            f"role IN ({', '.join(repr(r) for r in CHAT_ROLES)})",
            name="chat_message_role_valid",
        ),
        # 读一个会话的全部消息：`WHERE session_id = ? ORDER BY seq`
        Index("ix_chat_messages_session_seq", "session_id", "seq"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64),
        # 会话被物理删除时消息跟着走。★ 但正常的删除路径是归档
        # （`ChatSession.archived_at`），这里的 cascade 只是给
        # 测试清理与"用户明确要求彻底删"用的兜底。
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )

    #: 会话内单调递增。见模块 docstring「为什么要 seq」。
    seq: Mapped[int] = mapped_column(Integer, nullable=False)

    role: Mapped[str] = mapped_column(String(16), nullable=False)

    #: 正文。
    #:
    #: ★ `tool` 角色下它存的是**工具结果的文本**，而工具名与参数在
    #:   `tool_name` / `tool_args` 里。这样一条工具调用只占**一条**记录：
    #:   请求与结果不会因为分两条而在中途被用户打断时错位。
    #:   （把「调用」和「结果」拆成两条的模型在流式场景下很容易出现
    #:    「有调用没结果」的半截记录，而界面必须为这种半截状态写专门分支。）
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")

    #: 模型的推理段（`reasoning_content` / Anthropic thinking），可空。
    #: 它**不**进下一轮上下文（见 service 里的说明），只用于展示。
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: 工具名。`role == 'tool'` 时非空。
    tool_name: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: 工具参数。存下来是为了界面能显示"它拿着什么参数去调的"，
    #: 也是事后复核的唯一依据。
    tool_args: Mapped[dict | None] = mapped_column(JsonColumn, nullable=True)

    #: 这次工具调用是否失败。用它而不是去解析 content 里的文案 ——
    #: 文案会改，布尔不会。
    #: 可空：`role != 'tool'` 的行没有这个概念，NULL 表示"不适用"，
    #: 与"成功了"（False）是两件事。
    tool_failed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=now_utc)

    session: Mapped[ChatSession] = relationship(back_populates="messages")

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<ChatMessage {self.session_id!r}#{self.seq} {self.role} n={len(self.content)}>"


__all__ = [
    "CHAT_ROLES",
    "MAX_MESSAGE_CHARS",
    "MAX_TITLE_CHARS",
    "ChatMessage",
    "ChatSession",
]
