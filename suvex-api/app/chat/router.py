"""`chat` 领域路由（v0.93）。

| 方法   | 路径                            | 鉴权   | 说明                     |
| ------ | ------------------------------- | ------ | ------------------------ |
| GET    | `/chat/sessions`                | read   | 我的会话列表             |
| POST   | `/chat/sessions`                | read   | 新建会话                 |
| GET    | `/chat/sessions/{id}`           | read   | 会话详情（带消息）       |
| PATCH  | `/chat/sessions/{id}`           | read   | 改标题                   |
| DELETE | `/chat/sessions/{id}`           | read   | 归档（软删）             |
| POST   | `/chat/sessions/{id}/messages`  | read   | 发一条消息，跑一轮       |
| GET    | `/chat/tools`                   | read   | 这个对话能做什么         |

## ★ 为什么写操作的门槛是 `read` 而不是 `admin`

会话是**私有便签**：新建/改名/归档只碰自己那几行（`get_session_or_404`
按 `created_by` 过滤，别人的会话看起来就是 404）。要求 `admin` 会让普通
用户连自己的对话都不能存 —— 那不是权限，那是 bug。

**真正危险的不是"发消息"，而是消息里那次工具调用**。它需要什么权限，
由 `engine._need_scope` 在**执行那一刻**按工具各自的 `scope` 判
（`create_scan` 要 `scan`，`save_skill` 要 `admin`）。

## ★ 为什么工具级权限不在这里判

端点级别判不了：同一个 `POST /messages` 可能只是问"你好"（不要权限），
也可能让模型去改技能库（要 admin）。在 router 层一刀切要么过宽
（普通用户不能对话）、要么过窄（普通用户能借对话改技能库 —— 后者
才是真漏洞）。

所以分工是：**router 判"能不能用这个功能"，engine 判"能不能做这件事"**。
两道都在服务端，谁也不依赖前端。`tests/test_chat_router.py` 里有一条
专门测"read 账号让模型去改技能库会被拒"，钉住的是第二道。
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth.deps import Identity, require_read
from app.chat import engine, service
from app.chat.prompts import CHAT_TOOLS
from app.chat.schemas import (
    ChatMessageOut,
    ChatRenameIn,
    ChatSendIn,
    ChatSendOut,
    ChatSessionDetailOut,
    ChatSessionIn,
    ChatSessionOut,
)
from app.core.db import get_db
from app.core.errors import ApiError

logger = logging.getLogger("suvex.chat")

router = APIRouter(tags=["对话"])

DbSession = Annotated[Session, Depends(get_db)]
Reader = Annotated[Identity, Depends(require_read)]


@router.get("/chat/sessions", response_model=list[ChatSessionOut])
def list_sessions(db: DbSession, identity: Reader) -> list[ChatSessionOut]:
    """我的会话列表（不含消息）。"""
    rows = service.list_sessions(db, created_by=_owner(identity))
    return [ChatSessionOut.model_validate(row) for row in rows]


@router.post("/chat/sessions", response_model=ChatSessionOut, status_code=201)
def create_session(
    payload: ChatSessionIn, db: DbSession, identity: Reader
) -> ChatSessionOut:
    session = service.create_session(db, created_by=_owner(identity), title=payload.title)
    db.commit()
    return ChatSessionOut.model_validate(session)


@router.get("/chat/sessions/{session_id}", response_model=ChatSessionDetailOut)
def get_session(session_id: str, db: DbSession, identity: Reader) -> ChatSessionDetailOut:
    session = service.get_session_or_404(
        db, session_id=session_id, created_by=_owner(identity)
    )
    messages = service.list_messages(db, session=session)
    return ChatSessionDetailOut(
        **ChatSessionOut.model_validate(session).model_dump(),
        messages=[ChatMessageOut.model_validate(m) for m in messages],
    )


@router.patch("/chat/sessions/{session_id}", response_model=ChatSessionOut)
def rename_session(
    session_id: str, payload: ChatRenameIn, db: DbSession, identity: Reader
) -> ChatSessionOut:
    session = service.get_session_or_404(
        db, session_id=session_id, created_by=_owner(identity)
    )
    service.rename_session(db, session=session, title=payload.title)
    db.commit()
    return ChatSessionOut.model_validate(session)


@router.delete("/chat/sessions/{session_id}", response_model=ChatSessionOut)
def archive_session(session_id: str, db: DbSession, identity: Reader) -> ChatSessionOut:
    """归档（**软删**）。

    ⚠️ 不硬删：消息里可能有用户觉得重要的结论、扫描 id。硬删之后那些
       留痕就没了，而"清理会话列表"这个诉求用归档已经满足。
    """
    session = service.get_session_or_404(
        db, session_id=session_id, created_by=_owner(identity)
    )
    service.archive_session(db, session=session, archived=True)
    db.commit()
    return ChatSessionOut.model_validate(session)


@router.post("/chat/sessions/{session_id}/messages", response_model=ChatSendOut)
async def send_message(
    session_id: str, payload: ChatSendIn, db: DbSession, identity: Reader
) -> ChatSendOut:
    """发一条消息，跑一轮对话（可能含多次工具调用）。

    ## 为什么是 `async def`

    `engine.run_turn` 里要 `await gateway.chat(...)`。写成同步函数会让
    模型调用的几十秒**占满整个线程池**，同进程里其他请求排队。FastAPI
    对 `async def` 端点跑在事件循环上，不会阻塞别的请求。

    ⚠️ 反过来说，这里的 `db` 是**同步** Session，我们在事件循环里做
       同步 IO（SQLAlchemy）。这是本项目既有形态（`db.query` 到处都是），
       不是这次引入的问题；真要改得整体换 async 引擎，超出本轮范围。
    """
    session = service.get_session_or_404(
        db, session_id=session_id, created_by=_owner(identity)
    )
    if session.archived:
        raise ApiError.validation(
            "这个会话已归档，不能再发消息。新建一个会话继续。",
            {"sessionId": "已归档"},
        )

    content = payload.content.strip()
    if not content:
        # schema 的 `MessageBody` 已经有 min_length，但全空格也能过 ——
        # 一条只有空格的消息会让模型不知道在回什么。
        raise ApiError.validation("消息不能为空。", {"content": "必填"})

    try:
        result = await engine.run_turn(
            db,
            session=session,
            content=content,
            identity=identity,
            provider_id=payload.providerId,
        )
    except engine.ChatEngineError as exc:
        # ★ 把引擎的可读错误翻成 4xx 而不是 500：`ChatEngineError` 的两条
        #   来源（"没配接入点"、"调模型失败"）都是**用户可以处理**的，
        #   而 500 在界面上只会显示"服务器出错了"。
        db.rollback()
        raise ApiError.validation(str(exc)) from exc

    return ChatSendOut(messages=[ChatMessageOut.model_validate(m) for m in result.messages])


@router.get("/chat/tools")
def list_tools(identity: Reader) -> list[dict]:
    """这个对话能做什么。

    ★ 存在的理由：不给它，前端就只能把能力**硬编码**在页面上。那样
      加一个工具要改两处，而漏改的那一处会让界面显示的能力与实际不符 ——
      本项目反复踩的"配了却静默失效"在 UI 侧的变体。

    `available` 是按**当前身份**算的：没有 `admin` scope 的人看到
    `save_skill` 会显示不可用。这与 `engine._need_scope` 的判据**必须
    相同** —— 两处不一致会让界面说"能"而执行时说"不能"。
    """
    out: list[dict] = []
    for tool in CHAT_TOOLS:
        available = True if tool.scope is None else identity.can(tool.scope)
        out.append(
            {
                "name": tool.name,
                "label": tool.label,
                "description": tool.description,
                "params": tool.params,
                "scope": tool.scope,
                "available": available,
            }
        )
    return out


def _owner(identity: Identity) -> str:
    """会话归属键。

    ## 为什么用 `user_id` 而不是 `label`

    `Identity.label` 在令牌通道下是**令牌的 label**（"CI 流水线"），
    在会话通道下是用户 id。用 label 归属会让"两个不同用户都叫同一个
    label"变成共用一个会话列表。

    ## 为什么令牌也能开对话（不直接拒）

    令牌通道没有 `user_id`。这里退化成用令牌 label 归属 —— 意味着
    同一个令牌的多次调用能看到同一个列表，不同令牌之间隔离。这是
    合理的：CI 用令牌开对话，本来就是"同一个自动化身份"。
    """
    return identity.user_id or f"token:{identity.label}"


__all__ = ["router"]
