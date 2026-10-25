"""对话页的出入参。

分层口径与 `app/scans/schemas.py`、`app/schedules/schemas.py` 完全一致：

* **字段名直接写 camelCase**（就是契约里的 wire 名字，前端逐字一致）；
* 响应体继承 `CamelModel`（`validation_alias` 自动指向 ORM 的 snake_case
  属性，于是 `ChatSessionOut.model_validate(orm)` 能直接读）；
* 请求体继承 `StrictModel`（多传字段直接 400）。

⚠️ 别把字段名写成 snake_case。`CamelModel` 的 alias 是**单向**的
（`validation_alias=to_snake_case`），字段名写成 `message_count` 会让
`model_validate(orm)` 读得到、但 `response_model` 出去也是 `message_count` ——
前端拿不到 `messageCount`，症状是"字段全是 undefined"，且不指向这里。

## ★ 为什么「发一条消息」的响应里带**全部新消息**

一轮里落库的可能是**多条**：一条 assistant 文本 + 若干条 tool 记录，
而且它们有先后。只回 assistant 那一条，前端就没有工具卡可渲染，
只能再拉一次列表 —— 多一次往返，且中间会闪一下。

所以响应用 `messages: list[ChatMessageOut]`，顺序即渲染顺序。
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from app.chat.models import CHAT_ROLES, MAX_MESSAGE_CHARS, MAX_TITLE_CHARS
from app.core.schemas import CamelModel, StrictModel
from app.core.timeutil import IsoDateTime

#: 一条消息正文的上限。**由 pydantic 在入口拒**，而不是等到落库前截断 ——
#: 「你发的东西太长」是用户能立刻理解并改小的错误；静默截断会让模型
#: 收到一段被砍掉一半的提问还浑然不觉。
MessageBody = Annotated[str, Field(min_length=1, max_length=MAX_MESSAGE_CHARS)]

#: 角色。与 `models.CHAT_ROLES` 必须同源，模块末尾有一条断言钉住。
ChatRole = Literal["user", "assistant", "tool"]


class ChatSessionIn(StrictModel):
    """新建会话。"""

    title: Annotated[str, Field(max_length=MAX_TITLE_CHARS)] | None = Field(
        None, description="可选标题；不传则由首条消息自动生成"
    )


class ChatSessionOut(CamelModel):
    """会话元信息（列表与详情共用）。**不含消息**。"""

    id: str
    title: str
    createdBy: str
    createdAt: IsoDateTime
    updatedAt: IsoDateTime
    messageCount: int
    archived: bool


class ChatMessageOut(CamelModel):
    """一条消息。"""

    id: str
    seq: int
    role: ChatRole
    content: str
    reasoning: str | None = None
    toolName: str | None = None
    toolArgs: dict | None = None
    toolFailed: bool | None = None
    createdAt: IsoDateTime


class ChatSessionDetailOut(ChatSessionOut):
    """会话 + 消息。"""

    messages: list[ChatMessageOut]


class ChatSendIn(StrictModel):
    """发一条用户消息。"""

    content: MessageBody = Field(..., description="用户说的话")

    #: 模型接入点。不传就用平台当前的默认接入点。
    #:
    #: ★ 为什么允许按条指定而不是固定在会话上：用户可能先用便宜的模型问一句，
    #:   再换强的模型让它干活。按会话固定会让这个常见动作变成"新建一个会话"。
    providerId: str | None = Field(None, max_length=64, description="可选：本条用哪个模型接入点")


class ChatSendOut(CamelModel):
    """一轮对话的结果。"""

    #: 本轮**新落库的全部消息**（含刚发出去的 user 消息），按 seq 升序。
    messages: list[ChatMessageOut]


class ChatRenameIn(StrictModel):
    """改标题。"""

    title: Annotated[str, Field(max_length=MAX_TITLE_CHARS)]


#: 反向校验：`models.CHAT_ROLES` 与上面的 `ChatRole` 必须是同一组。
#: 放在模块级而不是测试里 —— 它是一份编译期断言，成本为零；
#: 放测试里意味着"改了一处忘了另一处"要等跑测试才发现。
assert set(CHAT_ROLES) == {"user", "assistant", "tool"}, (
    f"models.CHAT_ROLES 与 schemas 的 ChatRole 不一致：{CHAT_ROLES}"
)


__all__ = [
    "ChatMessageOut",
    "ChatRenameIn",
    "ChatSendIn",
    "ChatSendOut",
    "ChatSessionDetailOut",
    "ChatSessionIn",
    "ChatSessionOut",
]
