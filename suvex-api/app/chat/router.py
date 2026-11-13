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
| PATCH  | `/chat/sessions/{id}/settings`  | read   | 改六项会话设置（部分提交）|
| GET    | `/chat/sessions/{id}/mcp-status`| read   | 已挂载 MCP 的真实试连结果 |
| GET    | `/chat/personas`                | read   | 人格列表                 |
| POST   | `/chat/sessions/{id}/attachments` | read | 上传附件                 |
| POST   | `/chat/sessions/{id}/messages/{mid}/approval` | read + 该工具所需 scope | 审批 |

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

import json
import logging
import tempfile
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.agents import service as agents_service
from app.auth.deps import Identity, require_read
from app.chat import attachments as chat_attachments
from app.chat import compaction as chat_compaction
from app.chat import engine, resumer, service, turn_runs
from app.chat import interrupts as chat_interrupts
from app.chat import mcp_tools as chat_mcp_tools
from app.chat import sandbox as chat_sandbox
from app.chat import todos as chat_todos
from app.chat import workspaces as chat_workspaces
from app.chat.prompts import CHAT_TOOLS, TOOL_BY_NAME, may_call_tool
from app.chat.schemas import (
    ChatApprovalIn,
    ChatApprovalOut,
    ChatAttachmentOut,
    ChatChallengeRef,
    ChatCompactionOut,
    ChatContextOut,
    ChatFileExportIn,
    ChatFileItemOut,
    ChatFileListOut,
    ChatFileOut,
    ChatFileUploadOut,
    ChatForkIn,
    ChatInterjectIn,
    ChatInterruptIn,
    ChatMcpStatusOut,
    ChatMessageOut,
    ChatPersonaOut,
    ChatPinIn,
    ChatRenameIn,
    ChatSendIn,
    ChatSendOut,
    ChatSessionDetailOut,
    ChatSessionIn,
    ChatSessionOut,
    ChatSettingsIn,
    ChatTodoOut,
    ChatTurnRunOut,
)
from app.chat.stream import (
    SSE_HEADERS,
    SSE_MEDIA_TYPE,
    stream_turn,
)
from app.core.db import get_db, get_session_factory
from app.core.errors import ApiError
from app.core.timeutil import now_utc, to_iso

logger = logging.getLogger("suvex.chat")

router = APIRouter(tags=["对话"])

DbSession = Annotated[Session, Depends(get_db)]
Reader = Annotated[Identity, Depends(require_read)]


@router.get("/chat/sessions", response_model=list[ChatSessionOut])
def list_sessions(
    db: DbSession,
    identity: Reader,
    include_archived: Annotated[
        bool,
        Query(
            alias="includeArchived",
            description="连归档的一起返回（「已归档」区用）。默认只返回未归档的。",
        ),
    ] = False,
) -> list[ChatSessionOut]:
    """我的会话列表（不含消息）。

    ★ 默认**不**返回归档的：归档的语义就是"从侧栏消失"。
      但「已归档」区需要它们，所以留一个显式开关 —— 不开的时候
      连查询条件都不加，而不是拉回来再在内存里过滤。
    """
    rows = service.list_sessions(
        db, created_by=_owner(identity), include_archived=include_archived
    )
    # ★ v1.66：给每个会话标上「此刻是否在跑」，侧栏据此画运行中转圈点。
    #   一次批量查（只查本用户这些 id），不是逐条查 —— 列表是每次进页面都要渲染的。
    running_ids = turn_runs.running_session_ids(
        db, session_ids=[row.id for row in rows]
    )
    out: list[ChatSessionOut] = []
    for row in rows:
        item = ChatSessionOut.model_validate(row)
        if row.id in running_ids:
            item = item.model_copy(update={"running": True})
        out.append(item)
    return out


@router.post("/chat/sessions", response_model=ChatSessionOut, status_code=201)
def create_session(
    payload: ChatSessionIn, db: DbSession, identity: Reader
) -> ChatSessionOut:
    session = service.create_session(db, created_by=_owner(identity), title=payload.title)
    db.commit()
    return ChatSessionOut.model_validate(session)


@router.post(
    "/chat/sessions/for-challenge/{challenge_id}", response_model=ChatSessionOut
)
def open_challenge_session(
    challenge_id: str, db: DbSession, identity: Reader
) -> ChatSessionOut:
    """取「这道题的对话」，没有就建一个（**幂等**）。

    看板行上的「查看」调它：连点两次不应产生两条空会话 ——
    见 `service.get_or_create_challenge_session` 的说明。

    ⚠️ 用的是 POST 而不是 GET：第一次调用**会写库**。用 GET 的话浏览器
      预取/刷新会静默建会话，而用户根本没打算打开它。
    """
    session, _created = service.get_or_create_challenge_session(
        db, created_by=_owner(identity), challenge_id=challenge_id
    )
    db.commit()
    return ChatSessionOut.model_validate(session)


@router.post(
    "/chat/sessions/{session_id}/fork",
    response_model=ChatSessionOut,
    status_code=201,
    summary="从某条消息处复制一段历史，另开一条时间线",
)
def fork_chat_session(
    session_id: str,
    payload: ChatForkIn,
    db: DbSession,
    identity: Reader,
) -> ChatSessionOut:
    source = service.get_session_or_404(
        db, session_id=session_id, created_by=_owner(identity)
    )
    child = service.fork_session(
        db,
        source=source,
        upto_seq=payload.messageSeq,
        title=payload.title,
    )
    db.commit()
    return ChatSessionOut.model_validate(child)


@router.get("/chat/sessions/{session_id}/context", response_model=ChatContextOut)
def get_context_usage(
    session_id: str,
    db: DbSession,
    identity: Reader,
) -> ChatContextOut:
    """当前上下文占用的近似值（供对话页显示百分比）。

    `usedTokens` 是摘要 + 保留原文的字符/4，不含系统提示词和工具 schema；
    `windowTokens` 取 provider 上配置的窗口，没配则按模型名推断。
    真实计费仍以 provider 的 usage 为准。
    """
    from sqlalchemy import select

    from app.providers.models import ProviderAccount

    session = service.get_session_or_404(
        db, session_id=session_id, created_by=_owner(identity)
    )
    provider = None
    if session.provider_id:
        provider = db.get(ProviderAccount, session.provider_id)
    if provider is None:
        provider = db.scalars(
            select(ProviderAccount).order_by(
                ProviderAccount.is_default.desc(), ProviderAccount.created_at
            )
        ).first()
    window = chat_compaction.context_window_tokens(
        provider.model if provider is not None else None
    )
    if provider is not None and provider.context_window_tokens:
        window = provider.context_window_tokens
    # OMP 的顺序：provider 报的当前上下文占用优先；没有才退回 tokenizer/字符估算。
    latest = turn_runs.list_runs(db, session_id=session.id, limit=1)
    measured = latest[0].last_prompt_tokens if latest else 0
    source = "provider" if measured > 0 and latest and latest[0].usage_reported else "estimate"
    used = measured if source == "provider" else chat_compaction.estimate_context_tokens(db, session=session)
    if source == "estimate":
        # OMP 的 breakdown 还包含 system prompt / 工具 schema / 技能。
        # 没有 provider usage 时把它们也按字符估进来，至少不要让比例偏低。
        try:
            system_chars = len(
                engine._system_prompt_for(session, native_tools=True, db=db)
            )
            tools_chars = len(
                json.dumps(engine.build_tools_schema(), ensure_ascii=False)
            )
            used += (system_chars + tools_chars) // 4
        except Exception:  # noqa: BLE001 - 辅助显示，失败就只算消息
            pass
    compactions = chat_compaction.list_for_session(db, session_id=session.id)
    return ChatContextOut(
        usedTokens=used,
        windowTokens=window,
        percent=max(0, min(100, round(used / max(1, window) * 100))),
        compactionCount=len(compactions),
        source=source,
    )


@router.get("/chat/sessions/{session_id}", response_model=ChatSessionDetailOut)
def get_session(
    session_id: str,
    db: DbSession,
    identity: Reader,
    limit: Annotated[
        int | None,
        Query(
            ge=1,
            le=2000,
            description="只取最近 N 条。给了它才可能返回 hasEarlier=true。",
        ),
    ] = None,
    before: Annotated[
        int | None,
        Query(
            ge=1,
            description="取 seq 小于它的最近 N 条 —— 「加载更早」用的游标。",
        ),
    ] = None,
    after: Annotated[
        int | None,
        Query(
            ge=0,
            description="只取 seq 大于它的消息 —— 增量刷新用，别重下整个会话。",
        ),
    ] = None,
) -> ChatSessionDetailOut:
    session = service.get_session_or_404(
        db, session_id=session_id, created_by=_owner(identity)
    )
    messages = service.list_messages(
        db, session=session, limit=limit, before_seq=before, after_seq=after
    )
    # 增量刷新（after）永远不是"更早的一段"，hasEarlier 只对整页/翻页有意义
    has_earlier = bool(messages) and after is None and messages[0].seq > 1
    return ChatSessionDetailOut(
        **ChatSessionOut.model_validate(session).model_dump(),
        messages=[ChatMessageOut.model_validate(m) for m in messages],
        hasEarlier=has_earlier,
        challenge=_challenge_ref(db, session),
        compactions=[
            ChatCompactionOut.model_validate(row)
            for row in chat_compaction.list_for_session(db, session_id=session.id)
        ],
    )


@router.get("/chat/sessions/{session_id}/export")
def export_session(
    session_id: str, db: DbSession, identity: Reader
) -> StreamingResponse:
    """把整段对话导出成 **JSONL**（一行一条消息），用作审计留痕。

    比赛（羊城杯这类）要求「保留完整 AI 对话记录备审」，而页面上的
    对话只有人看得到、导不出来。这个端点把会话原样吐成 **ndjson**：
    第一行是会话头（`{"type":"session",...}`），之后每行一条消息
    （`{"type":"message","uuid":...,"parentUuid":...,"message":{...}}`），
    字段形状对齐常见编码 agent 的 `.jsonl` 记录习惯（Claude Code 那类），
    交给评委看就是"这支队每一步怎么想的、调了什么工具、拿到什么"。

    为什么是流式：一个长会话可能有几百上千条消息，一次性 `json.dumps`
    整份再回会让内存和首字节都变差；这里按行 yield，客户端边收边写盘。

    只读：不落库、不改会话。归档的会话**也能导出**（备审时往往正是要
    归档的旧记录），但仍受归属校验约束——只能导自己的会话。
    """
    session = service.get_session_or_404(
        db, session_id=session_id, created_by=_owner(identity)
    )
    messages = service.list_messages(db, session=session)

    def _gen():
        header = {
            "type": "session",
            "id": session.id,
            "title": session.title,
            "createdAt": to_iso(session.created_at),
            "project": session.project,
            "persona": session.persona,
            "providerId": session.provider_id,
            "archived": session.archived_at is not None,
        }
        yield json.dumps(header, ensure_ascii=False) + "\n"
        parent: str | None = None
        for m in messages:
            row = {
                "type": "message",
                "uuid": m.id,
                "parentUuid": parent,
                "seq": m.seq,
                "timestamp": to_iso(m.created_at),
                "role": m.role,
                "surface": m.surface,
                "text": m.content,
            }
            if m.reasoning:
                row["reasoning"] = m.reasoning
            if m.tool_name:
                row["toolName"] = m.tool_name
            if m.tool_args is not None:
                row["toolArgs"] = m.tool_args
            if m.tool_failed is not None:
                row["toolFailed"] = m.tool_failed
            if m.approval_status:
                row["approvalStatus"] = m.approval_status
                row["approvalDecidedBy"] = m.approval_decided_by
                row["approvalDecidedAt"] = (
                    to_iso(m.approval_decided_at) if m.approval_decided_at else None
                )
            parent = m.id
            yield json.dumps(row, ensure_ascii=False) + "\n"

    filename = f"session-{session.id}.jsonl"
    return StreamingResponse(
        _gen(),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
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


@router.post("/chat/sessions/{session_id}/pin", response_model=ChatSessionOut)
def pin_session(
    session_id: str, payload: ChatPinIn, db: DbSession, identity: Reader
) -> ChatSessionOut:
    """置顶 / 取消置顶。"""
    session = service.get_session_or_404(
        db, session_id=session_id, created_by=_owner(identity)
    )
    service.set_pinned(db, session=session, pinned=payload.pinned)
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
    # ★ 归档 = 这个会话不会再被发消息，它那个沙箱必须当场销毁。
    #
    #   不销毁会怎样：容器一直挂着（无限制网络 + 可写工作区），而
    #   `_SANDBOXES` 是进程内字典 —— 只要进程不重启它就不会自己消失，
    #   用户以为"删掉对话"了，实际容器还在。这是真泄漏，不是洁癖。
    #
    #   放在 commit 之后：先让"归档"这件事落库，再来清理资源。
    #   反过来写的话，销毁失败（或进程正好在这里退出）会留下一个
    #   归档了但容器还活着的组合，且没有第二次机会。
    #
    #   `drop_sandbox` 自己会吞掉 `destroy()` 里的异常，但这里再包一层：
    #   归档**已经 commit 了**，此刻任何异常都会变成一个 500 —— 用户看到
    #   "处理失败"，而会话其实已经删掉了。那是最糟的一种反馈：他以为没删
    #   成，可能再点一次，或去找管理员说"这个功能坏了"。
    #   资源清理失败该留在日志里，不该翻成用户的错误。
    try:
        chat_sandbox.drop_sandbox(session_id)
    except Exception:  # noqa: BLE001 - 清理失败不该让已落库的归档报错
        logger.warning("归档会话 %s 时销毁沙箱失败", session_id, exc_info=True)
    # ★ 附件也要清。两棵树：暂存区（用户传了但还没发的）和交接目录
    #   （已经搬进沙箱、被挂载的那份）。
    #
    #   为什么归档该清它们：附件是**一次性耗材**（见 `attachments` 模块
    #   docstring），而归档意味着这个会话不会再被发消息 —— 留着只是磁盘
    #   垃圾，且其中可能有用户不希望长期驻留的材料（pcap、日志、密钥文件）。
    #
    #   ⚠️ `drop_handed_over` 原先**没有任何调用方**（v0.109 前：`drop_session`
    #      也是零调用方）。于是"归档"只销毁了容器，文件一直留在
    #      `<tmp>/suvex-chat-attachments/<session_id>/` 下 —— 每次归档都在
    #      磁盘上多留一份用户上传的原始文件，直到系统重启清 /tmp。
    try:
        chat_attachments.drop_session(session_id=session_id)
        chat_attachments.drop_handed_over(session_id=session_id)
        # ★ 持久工作区也清：归档意味着这个会话不会再被发消息，留一整个
        #   宿主目录只是磁盘垃圾（里面可能有用户不想长期驻留的材料）。
        chat_workspaces.drop_workspace(session_id)
    except Exception:  # noqa: BLE001 - 同一条理由：清理失败不该翻成 500
        logger.warning("归档会话 %s 时清理附件失败", session_id, exc_info=True)
    return ChatSessionOut.model_validate(session)


@router.post("/chat/sessions/{session_id}/unarchive", response_model=ChatSessionOut)
def unarchive_session(session_id: str, db: DbSession, identity: Reader) -> ChatSessionOut:
    """从「已归档」里恢复。

    ★ 只恢复**消息**。归档时（v0.106/v0.108）沙箱与附件已经当场销毁了，
      这一点无法回退 —— `/work` 里跑出来的产物、上传的原始文件都回不来。
      所以前端必须在归档前给出二次确认，而不是让用户以为可以后悔。
    """
    session = service.get_session_or_404(
        db, session_id=session_id, created_by=_owner(identity)
    )
    service.archive_session(db, session=session, archived=False)
    db.commit()
    return ChatSessionOut.model_validate(session)


@router.post("/chat/sessions/{session_id}/purge", response_model=ChatSessionOut)
def purge_session(session_id: str, db: DbSession, identity: Reader) -> ChatSessionOut:
    """彻底删除一个会话（**物理删除**，不可恢复）。

    ## 第一道闸：必须先归档
    与 `scans` 的 `purge_scan` 同一条规则、同一个理由：给"手滑"留一段
    可回退的路。用户想永久删，就得先点一次「删除此对话」（= 归档），
    再在「已归档」里点「彻底删除」——中间隔着一次列表刷新，一次误点
    不会直接抹掉几百条消息。

    ## 清理顺序：先落库，再删文件
    与 `purge_scan` 相同：`db.commit()` 成功之后才删磁盘上的东西。
    反过来的话，删文件成功而 commit 失败，就留下一个"文件没了但会话还在"
    的幽灵 —— 那种状态没有任何界面能修。
    """
    session = service.get_session_or_404(
        db, session_id=session_id, created_by=_owner(identity)
    )
    if session.archived_at is None:
        raise ApiError.conflict(
            "CHAT_SESSION_NOT_ARCHIVED",
            "这个对话不在「已归档」里。请先删除它（进已归档），再执行彻底删除。",
        )
    # ★ 先取快照再删行：`purge_session` 之后这个对象已经不在会话里，
    #   再对它做 `model_validate` 是在读一个已删除的行。
    snapshot = ChatSessionOut.model_validate(session)
    removed = service.purge_session(db, session=session)
    db.commit()
    try:
        chat_sandbox.drop_sandbox(session_id)
    except Exception:  # noqa: BLE001 - 清理失败不该让已落库的删除报错
        logger.warning("彻底删除会话 %s 时销毁沙箱失败", session_id, exc_info=True)
    try:
        chat_attachments.drop_session(session_id=session_id)
        chat_attachments.drop_handed_over(session_id=session_id)
        chat_workspaces.drop_workspace(session_id)
    except Exception:  # noqa: BLE001 - 同上
        logger.warning("彻底删除会话 %s 时清理附件失败", session_id, exc_info=True)
    logger.info("彻底删除会话 %s：%d 条消息", session_id, removed)
    return snapshot


@router.post("/chat/sessions/purge-archived", summary="清空已归档会话")
def purge_archived_sessions(db: DbSession, identity: Reader) -> dict[str, int]:
    """一键彻底删除当前用户的全部已归档会话。

    与单条彻底删除同样的顺序：先落库，再清理沙箱/附件/工作区。已归档是
    唯一入口（列表里只能看到归档会话），所以这里不需要额外的"必须先归档"闸。
    """
    sessions = [
        session
        for session in service.list_sessions(
            db, created_by=_owner(identity), include_archived=True
        )
        if session.archived_at is not None
    ]
    ids = [session.id for session in sessions]
    messages = 0
    for session in sessions:
        messages += service.purge_session(db, session=session)
    db.commit()

    for session_id in ids:
        try:
            chat_sandbox.drop_sandbox(session_id)
        except Exception:  # noqa: BLE001 - 清理失败不该让已落库的删除报错
            logger.warning("清空归档时销毁会话 %s 的沙箱失败", session_id, exc_info=True)
        try:
            chat_attachments.drop_session(session_id=session_id)
            chat_attachments.drop_handed_over(session_id=session_id)
            chat_workspaces.drop_workspace(session_id)
        except Exception:  # noqa: BLE001 - 同上
            logger.warning("清空归档时清理会话 %s 的附件失败", session_id, exc_info=True)
    logger.info("清空归档：删除 %d 个会话 / %d 条消息", len(ids), messages)
    return {"deleted": len(ids), "messages": messages}


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
    # ★ v1.92：归档会话不挡发送 —— **发消息即恢复**。
    #
    # 归档的语义是"从默认列表收起来"，不是"只读"。用户点进已归档会话
    # 接着问，最合理的动作是恢复它并继续；回一句"先恢复"多了一次跳转，
    # 而那条提示本身也说明这个输入框看起来能用却不能用。
    if session.archived:
        session.archived_at = None
        db.commit()

    content = payload.content.strip()
    if not content:
        # schema 的 `MessageBody` 已经有 min_length，但全空格也能过 ——
        # 一条只有空格的消息会让模型不知道在回什么。
        raise ApiError.validation("消息不能为空。", {"content": "必填"})

    try:
        result = await engine.run_turn_durable(
            db,
            session=session,
            content=content,
            identity=identity,
            provider_id=payload.providerId,
            attachment_ids=payload.attachmentIds,
            explicit_tools=payload.explicitTools,
            locale=payload.locale,
        )
    except engine.ChatEngineError as exc:
        # ★ 把引擎的可读错误翻成 4xx 而不是 500：`ChatEngineError` 的两条
        #   来源（"没配接入点"、"调模型失败"）都是**用户可以处理**的，
        #   而 500 在界面上只会显示"服务器出错了"。
        db.rollback()
        raise ApiError.validation(str(exc)) from exc

    return ChatSendOut(
        messages=[ChatMessageOut.model_validate(m) for m in result.messages],
        awaitingApproval=result.awaiting_approval,
    )


@router.post(
    "/chat/sessions/{session_id}/messages/stream",
    summary="发一条消息，边跑边推（SSE）",
)
def send_message_stream(
    session_id: str,
    payload: ChatSendIn,
    identity: Reader,
) -> StreamingResponse:
    """发一条消息，把它产出的每一步**实时**推给前端（v0.100）。

    ## 为什么是 `def` 而不是 `async def`

    这个函数体内**没有**任何 `await`：它只做校验、然后把一个 async
    生成器交给 `StreamingResponse`。

    ## ★★ 为什么这里**不注入** `db`，而是自己在生成器里开一个会话

    这是整条流最容易写错的地方，而写错的表现是**间歇性的**
    （本地好用、线上偶发 500），所以先讲清：

    FastAPI 的 `Depends(get_db)` 是 `yield` 依赖。它的清理
    （`session.close()`）发生在**响应开始之后、生成器真正跑之前** ——
    也就是说，如果生成器里继续用那个 `db`，它拿到的是一条
    **已关闭**的会话，第一个 `db.commit()` 就会炸。

    所以校验用同一个短会话（那时还没开始流），真正的对话在生成器内部
    自己开一个会话、自己关掉。两者职责分开：入口校验一个短事务，
    流式执行一个长事务。

    ## 为什么端点是 POST 但用 SSE

    `EventSource` 只能发 GET，而发消息要带正文与附件 id。
    这里是**用 fetch 读流**（见前端 `data/chat-stream.ts`），
    所以 POST + `text/event-stream` 完全可行，且不需要把消息塞进 URL。

    代价是浏览器不会自动重连 —— 对对话来说这是**对的**：
    一轮对话没有"补发"的语义，断了就该重发一句，而不是把模型再跑一遍
    （那会再烧一次 token，而且工具可能有副作用）。
    """
    # ── 入口校验：都在短事务里做完，与流式执行分开 ──────────
    db = get_session_factory()()
    try:
        session = service.get_session_or_404(
            db, session_id=session_id, created_by=_owner(identity)
        )
        if session.archived:
            # 同非流式端点：发送即恢复，不再让用户先绕去列表点「恢复」。
            session.archived_at = None
            db.commit()

        # ★ v1.49：这个会话**已经有一轮在跑**时，不许再起一轮。
        #
        # 改造前这里什么都不查，于是 `run_turn_durable` → `_run_guarded` →
        # `turn_run_store.start()` 会**无条件新建一条 turn**。当一条长任务
        # 正在跑（浏览器那条 SSE 可能已经断了、但服务端心跳还在），用户以为
        # 「对话停了」就再发一句「继续」—— 结果同一会话上**两条 turn 并发**：
        # 老的那条继续干，新建的那条抢不到活儿、round=0 空转 20 多秒后 `done`。
        # 用户看到的就是「问一句停一次」：每次发话只换回一条空转的结束。
        #
        # 线上证据（chat_01M6BWQ69MZJNBAB7C7QMVXPV2）：turn_01M6E99VZ… 从
        # 14:06 一直 running（客户端断开但服务端在跑），而 14:50/14:52 两条
        # 「继续」各自新建的 turn 都是 `done / round_index=0 / tools_used=0`。
        #
        # 拒绝而不是静默新建：并发写同一个会话本就没有正确语义（两轮会各自
        # 读同一份历史、各自追加消息，seq 会交错）。要插话用 `/interject`
        # （排队到下一轮开头），要停/改方向用 `/interrupt`，要接手死掉的轮次
        # 用 `/turns/{run_id}/resume` —— 这三条路都能选，唯独不该悄悄再开一轮。
        #
        # ★ v1.68.4：并发检查放在**内容校验之前** —— 它判的是会话状态、与这条
        #   消息内容无关；且死轮收尾（give_up）也在这里发生，不该因为用户一次
        #   空输入就跳过。
        latest = turn_runs.list_runs(db, session_id=session_id, limit=1)
        if latest and latest[0].status in turn_runs.OPEN_STATUSES:
            now = now_utc()
            # 只有**真在跑**（running 且未僵尸）才挡；`interrupted` 一律不挡 ——
            # 它没在跑，只是"没答完等接手"。用户发消息时：
            #   * 若这条 interrupted 已死（resume 额度用尽，resumer 永不再接手），
            #     先把它判 failed 收尾，再放行新消息（否则会话被死轮永久卡住，
            #     用户既发不出去、又没有 resume 按钮可用 —— 2026-11-05 实锤）。
            #   * 若还可接手，也放行：新消息自会另起一轮（旧的由 resumer 或
            #     stale 回收处理），不再用一条 409 把用户按在原地。
            if turn_runs.is_blocking_new_turn(latest[0], now=now):
                raise ApiError.conflict(
                    "TURN_ALREADY_RUNNING",
                    "这个对话还有一轮正在跑。想补一句话用「插话」，想让它停下或改方向用"
                    "「中断」，想接手已经断掉的那一轮点「继续」——它们都会接在同一轮上。",
                )
            if turn_runs.is_abandoned_open(latest[0], now=now):
                turn_runs.give_up(
                    db,
                    latest[0],
                    reason="上一轮已中断且自动接手次数用尽，放行新消息",
                )
                db.commit()
            elif latest[0].status == "interrupted":
                # ★ v1.80：用户主动发新消息 = 放弃旧轮。
                #
                # 之前这里只"放行"，旧轮仍留给 resumer 自动接手 —— 于是同一会话
                # 出现**两个执行体**：新轮与旧轮各自读历史、各自写结论。实测表现
                # 是回复重复、工具计数错乱、两股流互相覆盖，看起来像"老是断"。
                #
                # 主动发消息的语义就是"我不等了"；要保留旧轮就用「继续」按钮
                # （`/turns/{id}/resume`，它接在同一轮上，不会新开一条）。
                turn_runs.finish(db, latest[0], status="cancelled")
                db.commit()

        content = payload.content.strip()
        if not content:
            raise ApiError.validation("消息不能为空。", {"content": "必填"})

    finally:
        # 校验完就关：这个会话的作用到此为止。**不要**把它交给生成器。
        db.close()

    async def run(emit):
        """真正跑一轮。自己开会话、自己关。"""
        turn_db = get_session_factory()()
        try:
            turn_session = service.get_session_or_404(
                turn_db, session_id=session_id, created_by=_owner(identity)
            )
            return await engine.run_turn_durable(
                turn_db,
                session=turn_session,
                content=content,
                identity=identity,
                provider_id=payload.providerId,
                attachment_ids=payload.attachmentIds,
                explicit_tools=payload.explicitTools,
                emit=emit,
                locale=payload.locale,
            )
        finally:
            turn_db.close()

    return StreamingResponse(
        stream_turn(
            run,
            to_payload=lambda result: {
                "messageCount": len(result.messages),
                "awaitingApproval": result.awaiting_approval,
                "hitToolLimit": result.hit_tool_limit,
            },
        ),
        media_type=SSE_MEDIA_TYPE,
        headers=SSE_HEADERS,
    )


@router.post(
    "/chat/sessions/{session_id}/interject",
    response_model=ChatMessageOut,
    summary="在模型跑的时候插一句话（排队到下一轮开头）",
)
def interject(
    session_id: str,
    payload: ChatInterjectIn,
    db: DbSession,
    identity: Reader,
) -> ChatMessageOut:
    """把用户的一句话写进会话，**让它出现在正在跑的那一轮的下一轮开头**。

    ## 为什么这是可行的（而不是需要打断循环）

    `engine.run_turn` 的每一轮开头都会重新
    `chat_service.list_messages(db, ...)` 读一遍历史。所以在它跑的
    过程中往库里写一条 user 消息，**下一轮模型自然就看到它了** ——
    不需要中断、不需要往运行中的协程里塞东西。

    这也是为什么它做成**独立端点**而不是一个 WebSocket：
    一次 HTTP 写入就够，没有任何长连接状态要维护。

    ## ★ 为什么不做"立即中断当前工具"

    那需要处理一堆中间态：工具正在跑（可能已经产生了副作用）、
    它的结果还没落库、消息 seq 已经分配……而每一种中间态都对应一类
    "库里留下半条记录"的事故。而用户真正需要的是
    「我这句话别丢、模型下一轮能看到」—— 排队即可满足。

    ## ⚠️ 只在**当前真的有轮次在跑**时才允许插话（v1.24 修）

    改造前这里不判"有没有在跑"，理由写的是"没在跑就等价于发一条新消息"。
    那句话是错的：**一条 user 消息本身不会起一轮**，只有 `run_turn` 会。
    没有在跑的轮次时把话写进库，那句话永远不会被回答 —— 界面上留下一条
    「插话 · 模型下一轮会看到」的僵尸气泡，加一个永不消失的「模型在推理…」。

    实测事故：会话 A 的长任务在跑（78 秒），用户切到新建的会话 B 打「你好」
    按 Enter → 走了 interject → 200 且落库 → B 里永远没有回复。

    所以这里改成**拒绝**（409 `NO_RUNNING_TURN`），让客户端退回"正常发送"。
    拒绝比默默写一条没人回答的消息好：前者用户能立刻看到这句话被正常回复。
    """
    session = service.get_session_or_404(
        db, session_id=session_id, created_by=_owner(identity)
    )
    if session.archived:
        raise ApiError.validation(
            "这个会话已归档，不能再插话。", {"sessionId": "已归档"}
        )
    content = payload.content.strip()
    if not content:
        raise ApiError.validation("插话不能为空。", {"content": "必填"})

    latest = turn_runs.list_runs(db, session_id=session_id, limit=1)
    if not latest or latest[0].status not in turn_runs.OPEN_STATUSES:
        raise ApiError.conflict(
            "NO_RUNNING_TURN",
            "这个对话当前没有在跑的回复。请直接发送（Enter），"
            "不要用插话 —— 插话只在模型正在回答时才有意义。",
        )

    message = service.append_user_message(db, session=session, content=content)
    db.commit()
    return ChatMessageOut.model_validate(message)


@router.post(
    "/chat/sessions/{session_id}/interrupt",
    summary="中断正在跑的那一轮（并继续 / 彻底停止）",
)
def interrupt(
    session_id: str,
    payload: ChatInterruptIn,
    db: DbSession,
    identity: Reader,
) -> dict:
    """让**正在跑的那一轮**停下来（v1.25）。

    用户原话：「打断当前会话也没做，看人家 CyberStrikeAI 的设计」——
    而 `interject`（插话）只往库里写一句话，**当前输出不会停**，用户
    看到的是「我让它停，它还在写」。

    ## 两种模式

    - `continue`：停掉当前流式输出 → 把 `note` 注入上下文 → 自动接着跑。
      用户中途要改方向（"别扫端口了，去看 /api/login"）时用这个。
    - `stop`：这一轮就地结束，执行记录落 `cancelled`，后台不接手。

    ## 时序（必须说实话）

    - 模型**正在流式输出**时：每个增量都查一次中断信号 → **立刻**停。
    - **工具正在执行**时：我们是在等一个阻塞的子进程，打断不了 →
      那个工具返回后才生效。所以接口返回里带 `latency`，前端据此
      告诉用户"当前工具跑完就停"。
    """
    session = service.get_session_or_404(
        db, session_id=session_id, created_by=_owner(identity)
    )
    if session.archived:
        raise ApiError.validation("这个会话已归档，不能中断。", {"sessionId": "已归档"})

    latest = turn_runs.list_runs(db, session_id=session_id, limit=1)
    if not latest or latest[0].status not in turn_runs.OPEN_STATUSES:
        raise ApiError.conflict(
            "NO_RUNNING_TURN",
            "这个对话当前没有在跑的回复，不需要中断。",
        )

    note = (payload.note or "").strip()
    if payload.mode == "stop":
        chat_interrupts.request_stop(session_id)
        return {
            "mode": "stop",
            "status": "scheduled",
            "message": "已请求停止：当前步骤结束（若正在跑工具，等它返回）后这一轮就结束。",
        }

    chat_interrupts.request_continue(session_id, note)
    return {
        "mode": "continue",
        "status": "scheduled",
        "hasNote": bool(note),
        "message": (
            "已请求中断：正在输出的内容会立刻停下，"
            + ("你的补充会并入上下文，" if note else "")
            + "然后自动接着跑。"
        ),
    }


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
        # ★ 与 `engine._need_scope`、`decide_approval` 同一个判据。
        #   这里算的是"界面上这个工具灰不灰"，而引擎算的是"真的能不能跑" ——
        #   两处一旦不同步，用户会看到"按钮可点，点了说没权限"，或者反过来
        #   "灰的但其实能跑"（后者更糟：用户以为没指望了）。
        available = may_call_tool(tool.name, identity)
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


@router.patch("/chat/sessions/{session_id}/settings", response_model=ChatSessionOut)
def update_settings(
    session_id: str, payload: ChatSettingsIn, db: DbSession, identity: Reader
) -> ChatSessionOut:
    """改六项会话设置。**部分提交**：只传要改的那几项。

    ## 为什么单独一个端点而不是并入 `PATCH /chat/sessions/{id}`

    那个端点改的是**标题**（`ChatRenameIn`）。把六项设置塞进去会让请求体
    变成"标题与六个可选字段混在一起"，而前端在改标题时得小心别把
    六个 `None` 一起发出去（会被解释成"清空"）。分成两个端点，两边的
    请求体都只有一个意思。

    ## 为什么不是 `admin`

    改的是**自己的**会话（`get_session_or_404` 按 `created_by` 过滤）。
    要求 `admin` 会让普通用户不能给自己的对话选人格。

    ⚠️ 但 `skillNames` / `mcpServerNames` 挂载的是**平台资源**。这里不判
       scope，因为挂载本身不执行任何东西 —— 它只是"下一轮把这几篇正文
       附在系统提示词里"。真正要权限的是 `read_skill` / `add_mcp_server`
       这类**动作**，那些在 `engine._need_scope` 里判。
    """
    session = service.get_session_or_404(
        db, session_id=session_id, created_by=_owner(identity)
    )
    service.update_settings(db, session=session, payload=payload)
    db.commit()
    return ChatSessionOut.model_validate(session)


@router.get(
    "/chat/sessions/{session_id}/mcp-status",
    response_model=ChatMcpStatusOut,
)
async def mcp_status(
    session_id: str,
    db: DbSession,
    identity: Reader,
) -> ChatMcpStatusOut:
    """本会话挂载的 MCP server **现在**能不能连上（逐个真试连）。

    ## 为什么单独一个端点，而不是在会话详情里带一个字段

    会话详情是"读库"，它是快的、可缓存的；而这里每次都要拉起子进程或
    打远端 URL，慢且结果随时会变（server 挂了、PATH 变了）。把试连塞进
    详情会让**打开任何一个对话**都可能卡十几秒 —— 用户只是想看历史。

    前端在打开「对话设置」、以及改完 `mcpServerNames` 之后调它一次，
    就能显示"已挂载 N 台 / 实际连通 M 台 / 失败原因"，而不是只存一个
    偏好、让用户去猜到底有没有生效。
    """
    session = service.get_session_or_404(
        db, session_id=session_id, created_by=_owner(identity)
    )
    catalog = await chat_mcp_tools.discover_mounted(db, session)
    return ChatMcpStatusOut.model_validate(catalog.status_payload())


@router.get("/chat/approvals", response_model=list[ChatApprovalOut], summary="跨会话审批记录")
def list_chat_approvals(
    identity: Reader,
    db: DbSession,
    status: str | None = None,
    limit: int = 100,
) -> list[ChatApprovalOut]:
    from app.chat import approvals as approvals_mod

    return [
        ChatApprovalOut.model_validate(item)
        for item in approvals_mod.list_approvals(
            db, created_by=identity.user_id or "", limit=min(max(limit, 1), 200), status=status
        )
    ]


@router.get("/chat/personas", response_model=list[ChatPersonaOut])
def list_personas(identity: Reader, db: DbSession) -> list[ChatPersonaOut]:
    """可选人格列表（弹窗画卡片、下拉画选项都要它）。

    ★ 从 `CHAT_PERSONAS_SPEC` 读而不是前端硬编码：人格的 label 与 summary
      是**提示词的一部分**（`render_persona` 用同一份数据），两边各写一份
      迟早会漂成"界面上叫渗透测试、提示词里说 recon"。
    """
    return [ChatPersonaOut(**item) for item in agents_service.persona_catalog(db)]


@router.post(
    "/chat/sessions/{session_id}/attachments",
    response_model=ChatAttachmentOut,
    status_code=201,
)
async def upload_attachment(
    session_id: str,
    db: DbSession,
    identity: Reader,
    file: Annotated[UploadFile, File(description="要带给模型看的文件")],
) -> ChatAttachmentOut:
    """上传一个附件，拿到 id（发消息时放进 `attachmentIds`）。

    ## 为什么是 `async def`

    `UploadFile.read()` 是 async 的（Starlette 的实现）。用同步函数读
    会在事件循环里做阻塞 IO。

    ## 为什么先落盘、不直接塞进消息

    用户可能传三个文件再问一句话。落盘 → 拿到 id → 发消息时一起带上，
    是唯一能表达"这三个属于同一条提问"的顺序。直接塞进消息的话，
    每个文件都得单独发一条空消息。

    ## 生命周期

    上传后 `STAGED_TTL_SECONDS`（1 小时）内有效。发消息时：
    - 正文被读进上下文；
    - 文件本身被 `hand_over()` **搬**到会话级交接目录，该目录以只读方式
      挂进沙箱的 `/work/attachments`（v0.109 起）。所以 `run_shell` 里的
      `file` / `binwalk` / `tcpdump -r` 能直接分析它。

    ⚠️ 这里原先写的是「发消息时被读走正文并**立刻删除**」。那句话在
       v0.107 之前是对的，之后就成了错的 —— 而且正是用户截图里那个
       「`/work/attachments/` 是空的」的直接来源：文件在模型有机会去看
       之前就已经没了。

    清理时机改为：会话归档（`archive_session` 里清两棵树）、或交接目录
    随会话消失。
    """
    session = service.get_session_or_404(
        db, session_id=session_id, created_by=_owner(identity)
    )
    if session.archived:
        raise ApiError.validation(
            "这个会话已归档，不能再传附件。", {"sessionId": "已归档"}
        )

    staged_now = chat_attachments.list_staged(session_id=session.id)
    if len(staged_now) >= chat_attachments.MAX_STAGED_PER_SESSION:
        raise ApiError.validation(
            f"这个会话暂存的附件太多（{len(staged_now)} 个）。"
            f"上限 {chat_attachments.MAX_STAGED_PER_SESSION} 个，"
            "先发一条消息把这些用掉。",
            {"attachments": "暂存已满"},
        )

    # 顺手清掉过期的：用户可能一小时前传了几个、再也没发消息。
    # 放在这里而不是定时任务里 —— 没有上传就不会有垃圾，而这里
    # 正是"新垃圾要进来"的时刻。
    chat_attachments.sweep(session_id=session.id)

    content = await file.read()
    if (
        sum(item.size_bytes for item in staged_now) + len(content)
        > chat_attachments.MAX_STAGED_BYTES_PER_SESSION
    ):
        raise ApiError.validation(
            "这个会话暂存的附件总量太大。"
            f"上限 {chat_attachments.MAX_STAGED_BYTES_PER_SESSION // 1024 // 1024} MB，"
            "先发一条消息把这些用掉。",
            {"attachments": "暂存总量已满"},
        )
    try:
        staged = chat_attachments.store(
            session_id=session.id,
            filename=file.filename or "attachment",
            content=content,
            attachment_id=uuid.uuid4().hex[:16],
        )
    except ValueError as exc:
        # `store` 把"太大"/"是空的"抛 ValueError，翻成用户能读懂的 400。
        raise ApiError.validation(str(exc), {"file": "不符合要求"}) from exc

    return ChatAttachmentOut(id=staged.id, name=staged.name, sizeBytes=staged.size_bytes)


@router.get("/chat/sessions/{session_id}/todos", response_model=list[ChatTodoOut])
def list_session_todos(
    session_id: str, db: DbSession, identity: Reader
) -> list[ChatTodoOut]:
    """这个会话的结构化计划（`chat_todos`，v1.89）。

    ★ 只读：计划由模型在对话里用 `create_todo` / `update_todo` 维护，
      前端只负责显示。没有计划时返回空数组（不是 404 —— "没有计划"
      是正常状态，不是资源不存在）。
    """
    session = service.get_session_or_404(
        db, session_id=session_id, created_by=_owner(identity)
    )
    return [
        ChatTodoOut(seq=row.seq, text=row.text, status=row.status)
        for row in chat_todos.list_todos(db, session.id)
    ]


@router.get("/chat/sessions/{session_id}/files", response_model=list[ChatFileOut])
def list_workspace_files(
    session_id: str, db: DbSession, identity: Reader
) -> list[ChatFileOut]:
    """列出这个会话在**宿主工作区**里的产物。

    工作区就是沙箱里 `/work/persist/` 的宿主那一侧：模型写进那里的文件
    不随容器生灭，会话归档/彻底删除时由 `drop_workspace` 一起清掉。
    """
    session = service.get_session_or_404(
        db, session_id=session_id, created_by=_owner(identity)
    )
    return [
        ChatFileOut(
            path=item.path,
            name=item.name,
            sizeBytes=item.size_bytes,
            modifiedAt=item.modified_at,
        )
        for item in chat_workspaces.list_files(session.id)
    ]


@router.get("/chat/sessions/{session_id}/files/download")
def download_workspace_file(
    session_id: str,
    db: DbSession,
    identity: Reader,
    path: Annotated[str, Query(min_length=1, max_length=1024)],
) -> FileResponse:
    """下载工作区里的一个文件。`path` 是相对工作区根目录的 POSIX 路径。"""
    session = service.get_session_or_404(
        db, session_id=session_id, created_by=_owner(identity)
    )
    target = chat_workspaces.resolve_file(session.id, path)
    if target is None:
        raise ApiError.not_found("这个文件")
    return FileResponse(
        path=str(target),
        filename=target.name,
        media_type="application/octet-stream",
    )


#: 批量导出 ZIP 的总大小上限。单文件上限由 `resolve_file` 把关（512 MB），
#: 这里挡的是"一次勾 200 个 512 MB"。
MAX_EXPORT_BYTES = 1024 * 1024 * 1024
#: ZIP 先写内存，超过这个体积再落盘（`SpooledTemporaryFile`）。
_EXPORT_SPOOL_BYTES = 64 * 1024 * 1024


@router.post("/chat/files/export")
def export_workspace_files(
    payload: ChatFileExportIn,
    db: DbSession,
    identity: Reader,
) -> StreamingResponse:
    """把勾选的工作区文件打成一个 ZIP（v1.80）。

    ## 为什么服务端打包

    浏览器的多文件下载会被拦截/弹权限，而且勾十个就要发十个请求。
    ZIP 是"一次请求、一个文件"，下载目录里也不会多出一堆散文件。

    ## 安全边界完全复用 `resolve_file`

    每条 `(sessionId, path)` 都先按会话鉴权（`get_session_or_404`
    的 `created_by` 过滤），再交给 `resolve_file` 做目录穿越/符号链接/
    大小检查。ZIP 里用 `{sessionId}/{path}` 作条目名：不同会话里同名
    的 `report.md` 不会互相覆盖，解压后仍能看出属于哪个会话。

    ## 缺一个就整体 404

    勾选之后、导出之前文件被删/被归档是可能的。静默跳过会让用户
    拿到一个"少了一个文件"的 ZIP 却毫无提示 —— 这比报错难查得多。
    所以只要有一条解析不出来，就整体 404 并说清有几个，让前端刷新。

    ## 为什么用 SpooledTemporaryFile

    `BytesIO` 会把整个 ZIP 读进内存；一次导出几百 MB 时那是几个进程
    的内存峰值。`SpooledTemporaryFile` 先写内存，超过 64 MB 自动落盘，
    响应仍是流式的（`StreamingResponse` 按 1 MB 块读，不在内存里拼完）。
    """
    sessions: dict[str, Session] = {}
    selected: list[tuple[str, str, Path]] = []
    seen: set[tuple[str, str]] = set()
    missing = 0
    total_bytes = 0
    for item in payload.items:
        key = (item.sessionId, item.path)
        if key in seen:  # 前端用 Set 去重，这里再挡一次（API 是公开契约）。
            continue
        seen.add(key)
        session = sessions.get(item.sessionId)
        if session is None:
            session = service.get_session_or_404(
                db, session_id=item.sessionId, created_by=_owner(identity)
            )
            sessions[item.sessionId] = session
        target = chat_workspaces.resolve_file(session.id, item.path)
        if target is None:
            missing += 1
            continue
        try:
            total_bytes += target.stat().st_size
        except OSError:
            missing += 1
            continue
        selected.append((session.id, item.path, target))

    if missing:
        raise ApiError.not_found(f"有 {missing} 个文件已不存在，请刷新列表后重试。")
    if not selected:
        raise ApiError.validation("没有可导出的文件。")
    if total_bytes > MAX_EXPORT_BYTES:
        raise ApiError.validation(
            f"选中的文件合计超过 {MAX_EXPORT_BYTES // (1024 * 1024)} MB，"
            "请分批导出。"
        )

    spool = tempfile.SpooledTemporaryFile(  # noqa: SIM115 - 响应流式读取期间必须保持打开
        max_size=_EXPORT_SPOOL_BYTES
    )
    try:
        with zipfile.ZipFile(spool, "w", zipfile.ZIP_DEFLATED) as archive:
            for session_id, path, target in selected:
                archive.write(target, arcname=f"{session_id}/{path.lstrip('/')}")
        spool.seek(0)
    except OSError as exc:
        spool.close()
        raise ApiError.validation("打包失败：文件可能刚被删除或占用。") from exc

    filename = f"suvex-files-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    return StreamingResponse(
        iter(lambda: spool.read(1024 * 1024), b""),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        background=BackgroundTask(spool.close),
    )


@router.post(
    "/chat/sessions/{session_id}/files",
    response_model=ChatFileUploadOut,
    status_code=201,
)
async def upload_workspace_file(
    session_id: str,
    db: DbSession,
    identity: Reader,
    file: Annotated[UploadFile, File(description="要存进会话工作区的文件")],
) -> ChatFileUploadOut:
    """把一个文件上传进这个会话的**工作区**（v1.38「文件管理」页的上传）。

    ## 与 `/attachments` 的区别（别混）

    | | `/attachments` | `/files`（这里） |
    |---|---|---|
    | 方向 | 用户 → 模型，发消息时读正文进上下文 | 用户 → 工作区，长期留存 |
    | 落点 | 暂存区，发消息后交接进 `/work/attachments` | `/work/persist/uploads/` |
    | TTL | 1 小时 | 无，随会话归档/删除才清 |
    | 用途 | 让模型**看**一份文件 | 给会话**存档**、或在「文件管理」页管理 |

    「文件管理」页要的是后者：用户在页面上传一份东西，之后在列表里能看到、
    能下载、能删 —— 与模型是否引用它无关。所以不复用 attachments 的暂存
    语义，直接写进工作区。

    ## 为什么 `async def`

    `UploadFile.read()` 是 async 的；在同步函数里读会在事件循环里做阻塞 IO。
    """
    session = service.get_session_or_404(
        db, session_id=session_id, created_by=_owner(identity)
    )
    if session.archived:
        raise ApiError.validation(
            "这个会话已归档，不能再往工作区里放文件。", {"sessionId": "已归档"}
        )

    content = await file.read()
    try:
        saved = chat_workspaces.save_file(
            session.id, file.filename or "upload", content
        )
    except ValueError as exc:
        raise ApiError.validation(str(exc), {"file": "不符合要求"}) from exc
    return ChatFileUploadOut(
        path=saved.path, name=saved.name, sizeBytes=saved.size_bytes
    )


@router.get("/chat/files", response_model=ChatFileListOut)
def list_all_workspace_files(
    db: DbSession,
    identity: Reader,
    session_id: Annotated[str | None, Query(alias="sessionId", max_length=128)] = None,
    project_id: Annotated[str | None, Query(alias="projectId", max_length=128)] = None,
    name: Annotated[str | None, Query(max_length=256)] = None,
    source: Annotated[str, Query(max_length=32)] = "all",
) -> ChatFileListOut:
    """「文件管理」页：跨会话列出**我的**产物文件（v1.37）。

    数据来源与会话级 `/chat/sessions/{id}/files` 相同 —— 每个会话在宿主上
    的持久工作区（沙箱 `/work/persist` 那一侧），不随容器回收而丢失。

    ## 为什么是服务端过滤，而不是前端拉全量

    工作区文件数可能上万（一个会话里跑数据集的场景）。`sessionId`/`projectId`/
    `name` 三个过滤都下推到这一层：命中单会话时连别的会话目录都不扫，
    避免把整机所有会话的文件都读一遍再丢掉。

    ## 为什么只列**未归档**会话

    `service.list_sessions` 默认就不返回归档的；归档时 `drop_workspace`
    也早把目录删了。两处一致，不需要在这里再判一次 `archived_at`。
    """
    if source not in ("all", "workspace"):
        raise ApiError.validation(
            "未知的文件来源。", {"source": "只支持 all / workspace"}
        )
    if session_id:
        sessions = [
            service.get_session_or_404(
                db, session_id=session_id, created_by=_owner(identity)
            )
        ]
    else:
        sessions = service.list_sessions(db, created_by=_owner(identity))

    needle = name.casefold() if name else None
    out: list[ChatFileItemOut] = []
    truncated = False
    for session in sessions:
        if project_id and (session.project or "") != project_id:
            continue
        for item in chat_workspaces.list_files(session.id):
            if needle and needle not in item.name.casefold():
                continue
            out.append(
                ChatFileItemOut(
                    sessionId=session.id,
                    sessionTitle=session.title,
                    project=session.project,
                    source="workspace",
                    path=item.path,
                    name=item.name,
                    sizeBytes=item.size_bytes,
                    modifiedAt=item.modified_at,
                )
            )
            if len(out) >= chat_workspaces.MAX_LISTED_FILES:
                truncated = True
                break
        if truncated:
            break
    # 最近修改的排前面 —— 「文件管理」页第一眼要找的通常是刚产出的东西。
    out.sort(key=lambda row: row.modifiedAt, reverse=True)
    # ★ 截断必须说出来：`truncated` 以前算完就丢了（返回裸数组），
    #   前端只能把 5000 当成"一共就这些"。
    return ChatFileListOut(
        items=out,
        truncated=truncated,
        limit=chat_workspaces.MAX_LISTED_FILES,
    )


@router.delete("/chat/sessions/{session_id}/files", status_code=204)
def delete_workspace_file(
    session_id: str,
    db: DbSession,
    identity: Reader,
    path: Annotated[str, Query(min_length=1, max_length=1024)],
) -> Response:
    """删除工作区里的一个文件（v1.37，「文件管理」页的删除操作）。"""
    session = service.get_session_or_404(
        db, session_id=session_id, created_by=_owner(identity)
    )
    target = chat_workspaces.resolve_file(session.id, path)
    if target is None:
        raise ApiError.not_found("这个文件")
    try:
        target.unlink()
    except OSError as exc:  # noqa: BLE001 - 磁盘错误翻成 4xx 而不是 500
        raise ApiError.validation("删除失败：文件可能正被占用。") from exc
    return Response(status_code=204)


@router.post(
    "/chat/sessions/{session_id}/messages/{message_id}/approval",
    response_model=ChatSendOut,
)
async def decide_approval(
    session_id: str,
    message_id: str,
    payload: ChatApprovalIn,
    db: DbSession,
    identity: Reader,
) -> ChatSendOut:
    """对一条挂起的工具调用做决定，批准就**接着跑完这一轮**。

    ## 权限：两道，与 `engine._need_scope` 同一判据

    端点级只要求 `read`（能进这个会话）。真正的门槛在被批准那个工具
    自己声明的 scope 上：`save_skill` 要 `admin`，`create_scan` 要 `scan`。
    在这里按工具名重判一次，理由与 `send_message` 那段 docstring 一致 ——
    用户可能在**上一轮**（有权限时）挂起、然后在**降权之后**才点确认。

    ⚠️ 如果不重判，路径就是：管理员挂起一个 `save_skill` → 会话被降成
       只读 → 点确认 → 工具照跑。审批变成了提权通道。

    ## 拒绝时

    只改状态，不执行、不继续。用户点拒绝的意思是"这条别做"，此时
    自动让模型接着说一句什么会显得吵。
    """
    session = service.get_session_or_404(
        db, session_id=session_id, created_by=_owner(identity)
    )
    message = service.get_message(db, session=session, message_id=message_id)

    if message.approval_status != "pending":
        raise ApiError.validation(
            "这条调用已经处理过了，不能重复审批。",
            {"messageId": f"当前状态：{message.approval_status}"},
        )

    tool_name = message.tool_name or ""
    spec = TOOL_BY_NAME.get(tool_name)
    # ★ v1.80：动态 MCP 工具（`mcp__<server>__<tool>`）不在 `CHAT_TOOLS` 里，
    #   不能按内置工具的 scope 判。它们的安全判据有两条：
    #     ① 会话是我的（上面 `get_session_or_404` 已经判了）；
    #     ② 这台 server **现在仍挂在这个会话上** —— 用户可能在挂起之后
    #        把它从设置里取消了，这时不该再执行。
    #   其它超出"挂载名单"的工具名（拼错/旧版本残留）仍然拒绝。
    if spec is None:
        if not chat_mcp_tools.is_mcp_tool_name(tool_name):
            # 挂起时记的工具名不在当前工具表里（改了代码/降了版本）。
            # 这时候不能执行 —— 我们不知道它要什么权限。
            raise ApiError.validation(
                f"这个工具（`{tool_name}`）已经不在可用列表里了，不能执行。",
                {"toolName": "未知工具"},
            )
        server = chat_mcp_tools.server_from_tool_name(
            tool_name, session.mcp_server_names or []
        )
        if server is None:
            raise ApiError.validation(
                f"这台 MCP server 已经不在本会话的挂载名单里了（`{tool_name}`），不能执行。",
                {"toolName": "MCP 连接未挂载"},
            )
    # ★ 与 `engine._need_scope` 用**同一个判据**（`prompts.may_call_tool`）。
    #
    #   两处各写一遍 `spec.scope is not None and not identity.can(...)` 的
    #   问题不是"今天会错"，而是"以后会漂"：给 `ChatToolSpec` 加一个
    #   多权限字段时，只会改其中一处，于是"审批放行、执行被拒"（用户点了
    #   确认，什么都没发生）或者反过来漏拦。判据只有一份，漂不了。
    elif not may_call_tool(tool_name, identity):
        raise ApiError.validation(
            f"`{tool_name}` 需要「{spec.scope}」权限，当前账号没有。",
            {"scope": spec.scope},
        )

    service.decide_approval(
        db,
        session=session,
        message=message,
        approve=payload.approve,
        decided_by=identity.user_id or f"token:{identity.label}",
    )
    db.commit()

    if not payload.approve:
        # 拒绝：只回那条改过状态的消息，不调模型。
        return ChatSendOut(messages=[ChatMessageOut.model_validate(message)])

    try:
        result = await engine.resume_approved_durable(
            db,
            session=session,
            message=message,
            identity=identity,
            locale=payload.locale,
        )
    except engine.ChatEngineError as exc:
        db.rollback()
        raise ApiError.validation(str(exc)) from exc

    return ChatSendOut(
        messages=[ChatMessageOut.model_validate(m) for m in result.messages],
        awaitingApproval=result.awaiting_approval,
    )


@router.get("/chat/sessions/{session_id}/turns", response_model=list[ChatTurnRunOut])
def list_turns(
    session_id: str,
    db: DbSession,
    identity: Reader,
) -> list[ChatTurnRunOut]:
    """这个会话最近几轮的执行账目（v1.19）。

    ## 为什么要有这个只读端点

    用户报的「对话又没反应了」在界面上原本无法自查：没有任何地方能回答
    "刚才那轮跑完了没有、是不是断了"。有了账目，前端才能把"上一轮中断"
    变成一条可操作的提示，而不是让用户对着空白猜。

    ## 权限：`read` + 归属过滤

    和所有会话子资源一致 —— 先按 `created_by` 取会话（别人的会话是 404），
    再列它的执行记录。执行记录里没有消息正文，只有状态与脱敏后的原因。
    """
    service.get_session_or_404(db, session_id=session_id, created_by=_owner(identity))
    runs = turn_runs.list_runs(db, session_id=session_id)
    stamp = now_utc()
    return [
        ChatTurnRunOut(
            id=run.id,
            sessionId=run.session_id,
            status=run.status,
            resumeAttempts=run.resume_attempts,
            lastError=run.last_error,
            startedAt=run.started_at,
            updatedAt=run.updated_at,
            resumable=turn_runs.is_resumable(run, now=stamp),
            # v1.61：进度 —— 前端在本页丢了 SSE 时靠它轮询出"还在推进"。
            roundIndex=run.round_index,
            toolsUsed=run.tools_used,
            heartbeatAt=run.heartbeat_at,
        )
        for run in runs
    ]


@router.post(
    "/chat/sessions/{session_id}/compact",
    response_model=ChatCompactionOut,
    summary="立即压缩这个会话的上下文（手动 /compact）",
)
async def compact_session(
    session_id: str,
    db: DbSession,
    identity: Reader,
) -> ChatCompactionOut:
    """不跑模型，只把旧历史压成摘要并落库。

    与自动压缩共用 `compaction.compact`：原始消息不删，只是后续喂模型的
    视图从 `first_kept_seq` 开始。正在跑一轮时拒绝 —— 否则轮次读到的
    边界会在中途变化，模型会看到一份前后不一致的历史。
    """
    session = service.get_session_or_404(
        db, session_id=session_id, created_by=_owner(identity)
    )
    latest = turn_runs.list_runs(db, session_id=session.id, limit=1)
    if latest and latest[0].status == "running" and not turn_runs.is_abandoned_open(
        latest[0], now=now_utc()
    ):
        raise ApiError.conflict(
            "TURN_ALREADY_RUNNING", "这一轮还在跑，等它结束再手动压缩。"
        )
    try:
        gateway = engine._build_gateway(engine._resolve_provider(session, None), db)
    except engine.ChatEngineError as exc:
        raise ApiError.validation(str(exc)) from exc
    row = await chat_compaction.compact(
        db, session=session, gateway=gateway, force=True
    )
    if row is None:
        raise ApiError.validation(
            "没有可压缩的旧历史（消息太少，或摘要调用失败）。"
        )
    return ChatCompactionOut(
        summary=row.summary,
        firstKeptSeq=row.first_kept_seq,
        tokensBefore=row.tokens_before,
        tokensAfter=row.tokens_after,
    )


@router.post(
    "/chat/sessions/{session_id}/turns/{run_id}/resume",
    response_model=ChatSendOut,
)
async def resume_turn(
    session_id: str,
    run_id: str,
    db: DbSession,
    identity: Reader,
) -> ChatSendOut:
    """手动接着跑一轮中断的执行（v1.19）。

    ## 为什么用户点一下比等后台更值得做

    后台接手要等心跳超过 `STALE_SECONDS`（90 秒）才判定"没人跑了"——
    这个延迟是**必须的**（否则会把正在跑的一轮当成死的，造出两轮并发
    写同一个会话）。但用户在他自己眼前看着对话停了的时候，不需要等：
    他知道那一轮已经死了。所以给一个显式入口，把 90 秒的等待变成 0。

    ## 两道闸

    1. `turn_runs.is_resumable` —— 只有 `running`/`interrupted` 且心跳够旧、
       重试没用尽的行才可接手；对已完成/已放弃的轮次点继续必须明确报错，
       而不是"看起来成功了但什么都没发生"。
    2. `resumer.resume_one` 里的 `claim` —— 带条件的 UPDATE，防止后台循环
       和这次点击同时捞到同一行。

    ## 为什么返回 `ChatSendOut`

    接手本质就是"把这一轮跑完"，产出同样是**新落库的消息**。复用同一个
    响应形状，前端拿到后按普通一轮的处理方式追加即可，不必写第二条渲染
    路径。
    """
    session = service.get_session_or_404(
        db, session_id=session_id, created_by=_owner(identity)
    )
    run = turn_runs.get(db, run_id=run_id)
    if run is None or run.session_id != session.id:
        raise ApiError.not_found("找不到这一轮的执行记录。")

    # ★ v1.22：`failed` 也允许重试 —— 那是**人**点的，不是自动接手。
    #   典型场景：密钥过期 → 自动重试被判定无意义而放弃 → 用户改好密钥
    #   后点错误条上的「重试」。若这里继续拒绝，用户唯一能做对的事也被堵死。
    if run.status == "failed":
        turn_runs.reset_for_manual_retry(db, run)
        db.commit()
    elif not turn_runs.is_resumable(run, now=now_utc()):
        raise ApiError.validation(
            "这一轮不需要继续（可能已经跑完、或者还在跑）。",
            {"runId": f"当前状态：{run.status}，重试 {run.resume_attempts} 次"},
        )

    # ★ v2.0：用户显式点「继续」= 重新给一段总预算。自动续跑的总时长
    #   用 `started_at` 判；不重置的话，跑到 4 小时后的手动继续会被
    #   第一轮检查直接判 failed，用户没有任何补救路径。
    run.started_at = now_utc()
    db.commit()

    before = {m.id for m in service.list_messages(db, session=session)}
    ran = await resumer.resume_one(db, run, auto_continue=False)
    if not ran:
        raise ApiError.validation(
            "没能接手这一轮（可能已被后台接手）。",
            {"runId": f"当前状态：{run.status}"},
        )

    fresh = [m for m in service.list_messages(db, session=session) if m.id not in before]
    return ChatSendOut(messages=[ChatMessageOut.model_validate(m) for m in fresh])


def _challenge_ref(db: Session, session: service.ChatSession) -> ChatChallengeRef | None:
    """会话关联题目的只读投影（题目条用）。

    ★ 题目被删了（重新同步、换接入）就返回 `None` —— 前端据此说
      "题目已不存在"，而不是把整条题目条藏掉：藏掉会让用户以为
      这段对话从来没有关联过任何东西。
    """
    if not session.challenge_id:
        return None
    from app.kernel.models import KernelChallenge, KernelCompetition

    challenge = db.get(KernelChallenge, session.challenge_id)
    if challenge is None:
        return None
    # 来源取**题目真实出处**（competition 级），与看板 `_resolve_platform_info`
    # 同一条口径：一个账号可能先后同步过多个练习集，按账号推会把老题全贴错标签。
    platform_name = ""
    if challenge.competition_id:
        competition = db.get(KernelCompetition, challenge.competition_id)
        if competition is not None:
            platform_name = (
                "本地案例"
                if competition.kind == "local"
                else (competition.name or "").strip() or "CTF2（内置）"
            )
    return ChatChallengeRef(
        id=challenge.id,
        title=challenge.title,
        status=challenge.status,
        score=challenge.score,
        remoteId=challenge.remote_id,
        platformName=platform_name,
        candidateFlag=challenge.candidate_flag,
        confirmedFlag=challenge.confirmed_flag,
        scanId=challenge.scan_id,
    )


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
