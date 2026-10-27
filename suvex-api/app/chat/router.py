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

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.auth.deps import Identity, require_read
from app.chat import attachments as chat_attachments
from app.chat import engine, sandbox as chat_sandbox, service
from app.chat.prompts import CHAT_PERSONAS_SPEC, CHAT_TOOLS, TOOL_BY_NAME, may_call_tool
from app.chat.schemas import (
    ChatApprovalIn,
    ChatAttachmentOut,
    ChatInterjectIn,
    ChatMessageOut,
    ChatPersonaOut,
    ChatRenameIn,
    ChatSendIn,
    ChatSendOut,
    ChatSessionDetailOut,
    ChatSessionIn,
    ChatSessionOut,
    ChatSettingsIn,
)
from app.chat.stream import (
    SSE_HEADERS,
    SSE_MEDIA_TYPE,
    encode_frame,
    stream_turn,
)
from app.core.db import get_db, get_session_factory
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
    except Exception:  # noqa: BLE001 - 同一条理由：清理失败不该翻成 500
        logger.warning("归档会话 %s 时清理附件失败", session_id, exc_info=True)
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
            attachment_ids=payload.attachmentIds,
            explicit_tools=payload.explicitTools,
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
            raise ApiError.validation(
                "这个会话已归档，不能再发消息。新建一个会话继续。",
                {"sessionId": "已归档"},
            )
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
            return await engine.run_turn(
                turn_db,
                session=turn_session,
                content=content,
                identity=identity,
                provider_id=payload.providerId,
                attachment_ids=payload.attachmentIds,
                explicit_tools=payload.explicitTools,
                emit=emit,
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

    ## ⚠️ 它只在**当前有东西在跑**时有"插"的语义

    没有在跑的轮次时，这句话仍然会写进库，行为等价于"发一条新消息"。
    这里不因为"没在跑"而拒绝它：拒绝会让用户以为自己的话没发出去
    （而实际上只是没有正在跑的东西可插）。
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

    message = service.append_user_message(db, session=session, content=content)
    db.commit()
    return ChatMessageOut.model_validate(message)


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


@router.get("/chat/personas", response_model=list[ChatPersonaOut])
def list_personas(identity: Reader) -> list[ChatPersonaOut]:
    """可选人格列表（弹窗画卡片、下拉画选项都要它）。

    ★ 从 `CHAT_PERSONAS_SPEC` 读而不是前端硬编码：人格的 label 与 summary
      是**提示词的一部分**（`render_persona` 用同一份数据），两边各写一份
      迟早会漂成"界面上叫渗透测试、提示词里说 recon"。
    """
    return [
        ChatPersonaOut(id=spec.id, label=spec.label, summary=spec.summary)
        for spec in CHAT_PERSONAS_SPEC
    ]


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

    上传后 `STAGED_TTL_SECONDS`（1 小时）内有效，发消息时被读走正文并
    **立刻删除**（`engine.run_turn` 里那两行）。见 `attachments` 模块
    docstring 的取舍说明。
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
    if spec is None:
        # 挂起时记的工具名不在当前工具表里（改了代码/降了版本）。
        # 这时候不能执行 —— 我们不知道它要什么权限。
        raise ApiError.validation(
            f"这个工具（`{tool_name}`）已经不在可用列表里了，不能执行。",
            {"toolName": "未知工具"},
        )
    # ★ 与 `engine._need_scope` 用**同一个判据**（`prompts.may_call_tool`）。
    #
    #   两处各写一遍 `spec.scope is not None and not identity.can(...)` 的
    #   问题不是"今天会错"，而是"以后会漂"：给 `ChatToolSpec` 加一个
    #   多权限字段时，只会改其中一处，于是"审批放行、执行被拒"（用户点了
    #   确认，什么都没发生）或者反过来漏拦。判据只有一份，漂不了。
    if not may_call_tool(tool_name, identity):
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
        result = await engine.resume_approved(
            db, session=session, message=message, identity=identity
        )
    except engine.ChatEngineError as exc:
        db.rollback()
        raise ApiError.validation(str(exc)) from exc

    return ChatSendOut(
        messages=[ChatMessageOut.model_validate(m) for m in result.messages],
        awaitingApproval=result.awaiting_approval,
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
