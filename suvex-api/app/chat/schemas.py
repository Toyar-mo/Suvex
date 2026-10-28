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

from app.chat.attachments import MAX_ATTACHMENTS_PER_MESSAGE
from app.chat.models import (
    APPROVAL_STATUSES,
    CHAT_PERSONAS,
    CHAT_ROLES,
    MAX_MESSAGE_CHARS,
    MAX_PROJECT_CHARS,
    MAX_TITLE_CHARS,
)
from app.core.schemas import CamelModel, StrictModel
from app.core.timeutil import IsoDateTime

#: 一条消息正文的上限。**由 pydantic 在入口拒**，而不是等到落库前截断 ——
#: 「你发的东西太长」是用户能立刻理解并改小的错误；静默截断会让模型
#: 收到一段被砍掉一半的提问还浑然不觉。
MessageBody = Annotated[str, Field(min_length=1, max_length=MAX_MESSAGE_CHARS)]

#: 「中断并继续」的说明上限（v1.25）。
#:
#: 它会被拼进一条 user 消息注入上下文。给 2000 是刻意的**小**：这不是让用户
#: 写小作文的入口，而是一句方向修正（"别扫端口了，去看 /api/login"）。
#: 太长会挤占上下文预算，而预算正是这类长任务最先耗尽的东西。
MAX_INTERRUPT_NOTE_CHARS = 2000

#: 角色。与 `models.CHAT_ROLES` 必须同源，模块末尾有一条断言钉住。
ChatRole = Literal["user", "assistant", "tool"]

#: 人格 id。与 `models.CHAT_PERSONAS` 必须同源（模块末尾有断言）。
#:
#: ★ 用 `Literal` 而不是 `str`：多传一个不存在的 persona 会在 400 那一步
#:   被拒，而不是写进库、再被数据库 CHECK 拒（后者报的是英文约束名，
#:   用户看不懂）。但数据库那条 CHECK 仍然保留 —— 它挡的是不经 API 的写入。
ChatPersonaId = Literal[
    "default",
    "recon",
    "pentest",
    "binary",
    "postex",
    "container",
    "forensics",
    # ★ v0.99：与 `models.CHAT_PERSONAS` / `prompts.CHAT_PERSONAS_SPEC` /
    #   迁移 `f3a1c8d52e07` 同源。文件末尾那条断言钉住这里不能漏。
    "api",
    "cloud",
    "ctf",
    "aillm",
]

#: 审批状态。与 `models.APPROVAL_STATUSES` 同源。
ApprovalStatus = Literal["pending", "approved", "rejected"]


class ChatSessionIn(StrictModel):
    """新建会话。"""

    title: Annotated[str, Field(max_length=MAX_TITLE_CHARS)] | None = Field(
        None, description="可选标题；不传则由首条消息自动生成"
    )
    #: 这两项允许在**建的时候**就给，省掉一次 `PATCH`。
    #:
    #: ★ 只给这两项，不给全部六项：项目与人格是"我要开始干什么"，属于
    #:   发起对话时就知道的事；模型接入点/审批/挂载更适合"进去之后调"。
    #:   给全部六项会让这个 schema 变成 `ChatSettingsIn` 的副本，而
    #:   两份定义迟早会漂。
    project: Annotated[str, Field(max_length=MAX_PROJECT_CHARS)] | None = Field(
        None, description="可选归属项目"
    )
    persona: ChatPersonaId | None = Field(None, description="可选人格")


class ChatSessionOut(CamelModel):
    """会话元信息（列表与详情共用）。**不含消息**。"""

    id: str
    title: str
    createdBy: str
    createdAt: IsoDateTime
    updatedAt: IsoDateTime
    messageCount: int
    archived: bool
    #: 是否置顶。列表已按「置顶在前」排好序，这个字段是给条目上的
    #: 那个小旗子图标与「置顶/取消置顶」菜单文案用的 —— 前端不需要
    #: 自己再判断一次顺序。
    pinned: bool = False

    # ── v0.94 的六个「对话设置」 ────────────────────────────────
    #
    # ★ 它们出现在**列表**响应里（不只是详情），因为侧栏要按 `project`
    #   分组、要在条目上标出人格/审批状态。让列表也带这三个小字段，
    #   比让前端为每个会话再请求一次详情便宜得多。
    project: str | None = None
    persona: str | None = None
    providerId: str | None = None
    requireApproval: bool = False
    skillNames: list[str] | None = None
    mcpServerNames: list[str] | None = None


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
    #: `answer`（对话内容，页面画气泡）/ `process`（过程证据，收进折叠块）。
    #: 默认 `answer`：老客户端/老数据不用改。
    surface: str = "answer"

    # ── 人工审批三字段（v0.94）────────────────────────────────
    # `None` = 这条消息与审批无关。`'pending'` = 模型想调这个工具、
    # 还在等用户点确认，**此时工具还没执行过**，`content` 里写的是
    # "等待确认"。`'approved'`/`'rejected'` 是终态。
    approvalStatus: ApprovalStatus | None = None
    approvalDecidedBy: str | None = None
    approvalDecidedAt: IsoDateTime | None = None


class ChatSessionDetailOut(ChatSessionOut):
    """会话 + 消息。"""

    messages: list[ChatMessageOut]


class ChatSettingsIn(StrictModel):
    """改会话的六项设置（**部分提交**：只传要改的）。

    ## 为什么是"只传要改的"而不是整份提交

    这六个设置由**四个不同控件**分别触发：侧栏改项目、弹窗选人格、
    下拉选模型、开关切审批。整份提交意味着每个控件都要先读到另外五个
    的当前值再一起发 —— 而"读到"与"提交"之间有窗口，两个标签页同时改
    不同设置时后提交的会把前一个覆盖回旧值。

    ## 为什么用 `None` 表示"不改"而不是"清空"

    ★ 这是这一层最容易写错的地方，所以显式区分：
      - **字段缺省**（`"project" not in payload`）→ 不改
      - **显式 `null`** → 清空（退回"未归属项目"/"默认人格"/"平台默认接入点"）

    实现方式是 `model_fields_set` 判"传没传"，而不是判值是否为 `None`。
    两者混用会让"清空项目"变成"没改"，而那正是用户点了「移出项目」
    之后发现它还在原项目里的那种 bug。
    """

    project: Annotated[str, Field(max_length=MAX_PROJECT_CHARS)] | None = Field(
        None, description="归属项目名；显式 null 表示移出项目"
    )
    persona: ChatPersonaId | None = Field(None, description="人格；显式 null 退回默认")
    providerId: str | None = Field(
        None, max_length=64, description="模型接入点；显式 null 退回平台默认"
    )
    requireApproval: bool | None = Field(None, description="是否要求人工审批有副作用的操作")
    skillNames: list[str] | None = Field(
        None, max_length=20, description="挂载的技能名；显式 [] 表示全部取消"
    )
    mcpServerNames: list[str] | None = Field(
        None, max_length=20, description="挂载的 MCP server 名；显式 [] 表示全部取消"
    )


class ChatPersonaOut(CamelModel):
    """一个人格（给前端弹窗/下拉渲染）。"""

    id: str
    label: str
    summary: str


class ChatAttachmentOut(CamelModel):
    """一个已上传的对话附件。"""

    #: 上传后拿到的 id，发消息时放在 `attachmentIds` 里。
    id: str
    name: str
    sizeBytes: int


class ChatApprovalIn(StrictModel):
    """对一条待审批的工具调用做决定。"""

    approve: bool = Field(..., description="true = 执行，false = 拒绝")


class ChatSendIn(StrictModel):
    """发一条用户消息。"""

    content: MessageBody = Field(..., description="用户说的话")

    #: 模型接入点。不传就用**会话上的**设置，会话也没设就用平台默认。
    #:
    #: ★ 为什么允许按条指定而不是固定在会话上：用户可能先用便宜的模型问一句，
    #:   再换强的模型让它干活。按会话固定会让这个常见动作变成"新建一个会话"。
    #:
    #: ★ 但 v0.94 之后**会话上也有** `providerId` 了。两者关系：
    #:   本条传了 → 用本条的（且不改会话设置）；没传 → 用会话的。
    #:   顺序不能反过来：会话设置是"默认值"，逐条指定是"这一次例外"。
    providerId: str | None = Field(None, max_length=64, description="可选：本条用哪个模型接入点")

    #: 这一轮要带给模型看的附件 id（先用 `POST .../attachments` 上传拿到）。
    #:
    #: ★ 传的是 **id 而不是文件名**：文件名可重复（用户可能连传两个
    #:   `access.log`），而 id 是 `attachments.store` 落盘时生成的唯一键。
    #:   用文件名当键会让第二个覆盖第一个，而用户以为两段都在。
    attachmentIds: list[str] | None = Field(
        None, max_length=MAX_ATTACHMENTS_PER_MESSAGE, description="附件 id 列表"
    )

    #: 用户在输入框里 @ 的工具名（v0.94）。
    #:
    #: ★ 传名字而不是"强制只准用这个"：见 `prompts.render_tool_hint` 的说明。
    #:   未命中 `CHAT_TOOL_NAMES` 的名字不会报错，只会在提示词里标明
    #:   "没有这个工具" —— 报 422 会让"用户记错工具名"变成一条发不出去的消息。
    explicitTools: list[str] | None = Field(
        None, max_length=8, description="本轮希望优先使用的工具名"
    )


class ChatSendOut(CamelModel):
    """一轮对话的结果。"""

    #: 本轮**新落库的全部消息**（含刚发出去的 user 消息），按 seq 升序。
    messages: list[ChatMessageOut]

    #: 若这一轮停在等待人工确认上，这里是被挂起那条消息的 id。
    #:
    #: ★ 前端靠它决定"要不要弹确认框"。让它跟着这一轮的响应一起回来，
    #:   而不是让前端去 `messages` 里筛 `approvalStatus === 'pending'` ——
    #:   后者在"历史里还有没处理完的挂起项"时会筛出旧的那条，于是用户
    #:   点了确认却发现执行的是上一轮的另一个工具。
    awaitingApproval: str | None = None


class ChatInterjectIn(StrictModel):
    """在模型跑的时候插一句话（v0.100）。

    ## 为什么它比 `ChatSendIn` 少了三个字段

    `providerId` / `attachmentIds` / `explicitTools` 都是**对本次请求生效**
    的东西：换模型、带附件、@ 工具。而插话是往一个**已经跑起来的轮次**
    里补一句上下文 —— 那一轮的模型和附件在它启动时就已经定了，
    改它们不会生效，只会在界面上造成"我换了模型但其实没换"的错觉。

    所以这里只有正文。少即是准确。
    """

    content: MessageBody = Field(..., description="要插进去的话")


class ChatInterruptIn(StrictModel):
    """中断正在跑的那一轮（v1.25）。

    ## 两种模式的区别（不是措辞，是后果）

    - `continue`：**中断并继续**。停掉当前输出，把 `note` 作为补充注入上下文，
      然后自动接着跑。用户要改方向时用这个。
    - `stop`：**彻底停止**。这一轮就地结束，执行记录落 `cancelled`，
      后台不会自动接手。

    `note` 只在 `continue` 时有意义，可为空（空 = "别按原计划走，但也没别的补充"）。
    """

    mode: Literal["continue", "stop"] = Field(
        ..., description="continue=中断并继续；stop=彻底停止"
    )
    note: Annotated[str, Field(max_length=MAX_INTERRUPT_NOTE_CHARS)] = ""


class ChatRenameIn(StrictModel):
    """改标题。"""

    title: Annotated[str, Field(max_length=MAX_TITLE_CHARS)]


class ChatTurnRunOut(CamelModel):
    """一轮执行的账目（v1.19）。

    ## 为什么把 `lastError` 也给界面

    用户报的是「对话又没反应了」—— 界面原来对这种状态的表达是**空白**。
    把原因写出来，用户至少能判断"是我网断了"还是"平台挂了"，而不是
    只能反复刷新。文案已经在服务端 `turn_runs.redact()` 里做过脱敏与
    截断，这里不再二次加工。
    """

    id: str
    sessionId: str
    status: str
    resumeAttempts: int
    lastError: str | None = None
    startedAt: IsoDateTime
    updatedAt: IsoDateTime

    #: 现在点"继续"有没有意义。前端据此决定是否显示那个按钮 ——
    #: 判断放在服务端，避免前后端各写一份"什么算可接手"而慢慢漂开。
    resumable: bool


class ChatPinIn(StrictModel):
    """置顶/取消置顶。

    ★ 用显式的 `pinned` 而不是两个端点（`/pin` 与 `/unpin`）：
      两个端点会让前端多一个分支，而"置顶"与"取消置顶"在服务端只差
      一个布尔值。同一个端点上翻转也顺手支持了将来的"幂等重放"。
    """

    pinned: bool


#: 反向校验：`models.CHAT_PERSONAS` 与 `ChatPersonaId` 必须是同一组。
#: 与下面那条 `CHAT_ROLES` 断言同样的理由：它是编译期断言，成本为零。
#:
#: ★ v0.99：这里必须与 `ChatPersonaId`（上面的 Literal）**一起**改。
#:   这条断言正是为了让"只改一处"在 import 期就炸 —— 而它这次真的
#:   在人格扩容时炸了一次（加四个新人格时漏了这个元组）。
_PERSONA_LITERALS = (
    "default",
    "recon",
    "pentest",
    "binary",
    "postex",
    "container",
    "forensics",
    "api",
    "cloud",
    "ctf",
    "aillm",
)
assert set(CHAT_PERSONAS) == set(_PERSONA_LITERALS), (
    f"models.CHAT_PERSONAS 与 schemas 的 ChatPersonaId 不一致：{CHAT_PERSONAS}"
)
assert set(APPROVAL_STATUSES) == {"pending", "approved", "rejected"}, (
    f"models.APPROVAL_STATUSES 变了：{APPROVAL_STATUSES}"
)


#: 反向校验：`models.CHAT_ROLES` 与上面的 `ChatRole` 必须是同一组。
#: 放在模块级而不是测试里 —— 它是一份编译期断言，成本为零；
#: 放测试里意味着"改了一处忘了另一处"要等跑测试才发现。
assert set(CHAT_ROLES) == {"user", "assistant", "tool"}, (
    f"models.CHAT_ROLES 与 schemas 的 ChatRole 不一致：{CHAT_ROLES}"
)


__all__ = [
    "ApprovalStatus",
    "ChatApprovalIn",
    "ChatAttachmentOut",
    "ChatMessageOut",
    "ChatPersonaId",
    "ChatPersonaOut",
    "ChatRenameIn",
    "ChatSendIn",
    "ChatSendOut",
    "ChatSessionDetailOut",
    "ChatSessionIn",
    "ChatSessionOut",
    "ChatSettingsIn",
]
