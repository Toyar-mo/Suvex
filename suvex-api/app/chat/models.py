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

## ★ 为什么设置挂在会话行上、而不是「对话内改设置」的独立表

v0.93 的初版这里写着「这一版明确不做，所以模型里也没有对应的东西」。
v0.94 做了，但做法是**会话行上加列**，不是建 settings 表 —— 理由见
`project` / `persona` 那几个字段上方的注释：六列标量、每轮必读，
独立表只换来"加列时好看一点"，代价是每轮多一次 JOIN。

仍然不做的：**让模型改自己的设置**（`providerId` / `persona` 只能由
用户在界面上改）。模型能改自己的接入点意味着它能把自己换到一个更
听话的模型上去，那是这条边界存在的全部理由。
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
    text,
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

#: 角色（人格）id 的合法取值。
#:
#: ★ 与 `CHAT_ROLES`（消息角色 `user`/`assistant`/`tool`）**毫无关系**，
#:   纯属中文都译作"角色"。这一组是「这个对话里模型扮演谁」。
#:   定义在这里而不是 `prompts.py`，因为它要被写进 CHECK 约束 ——
#:   而 CHECK 约束的那份列表必须是**数据库层**也认的。
#:   `prompts.py` 里有一条断言钉住"两处必须同一组"。
CHAT_PERSONAS = (
    "default",
    "recon",
    "pentest",
    "binary",
    "postex",
    "container",
    "forensics",
    # ★ v0.99：从 CyberStrikeAI 的 13 个角色里补的四个方向。
    #
    # ## 为什么只补这四个，而不是把 13 个全搬
    #
    # 它那 13 个里有三个语义几乎一样（Web应用扫描 / Web框架测试 /
    # 综合漏洞扫描），而我们已有的 `pentest` 覆盖了同一片区域 ——
    # 全搬会造出「三个看起来一样的人格」，用户选的时候会犹豫，
    # 而选择困难本身就是功能缺陷。它的 `默认` 就是我们的 `default`。
    #
    # 剩下真正**方向不重叠**的是这四个：
    #   api     —— 接口层：GraphQL / JWT / 越权 / 批量赋值。pentest 讲的是
    #              "打穿一条链"，api 讲的是"接口契约哪里缺了检查"。
    #   cloud   —— 云与编排控制面：元数据、IAM、K8s RBAC。container 讲的是
    #              "镜像与运行时配置"，cloud 讲的是"控制面与身份"。
    #   ctf     —— CTF 解题节奏：先判题型、先拿稳的分、时间盒。
    #              与 pentest 的"按攻击路径推进"是两种工作方式。
    #   aillm   —— LLM/智能体应用：提示注入、工具滥用、RAG 投毒。
    #              这是 agentsec 场景包对应的方向，此前**没有任何人格**。
    #
    # ⚠️ 加值必须同时改三处：这里、`prompts.CHAT_PERSONAS_SPEC`、
    #   以及一条扩 CHECK 约束的迁移。三处不一致会在不同位置以不同方式坏：
    #     · 少了这里 → CHECK 拒绝写入，用户点保存直接报错（还算明显）；
    #     · 少了 prompts → 能存进库但 `PERSONA_BY_ID` 查不到，
    #       静默退回默认人格（**这是最坏的一种**，界面上看不出异常）。
    #    `prompts.py` 文件末尾那条断言钉住前两处一致。
    "api",
    "cloud",
    "ctf",
    "aillm",
)

#: 归属项目名的最大长度。够放中文项目名（20 个汉字），也够放英文长名。
MAX_PROJECT_CHARS = 64

#: 人工审批的状态取值。
#:
#: `pending` 是唯一"还没结束"的状态 —— 它意味着那条工具消息**还没执行过**。
#: `approved` / `rejected` 都是终态：前者执行过（结果在 `content` 里），
#: 后者没执行（`content` 里写着为什么没做）。
APPROVAL_STATUSES = ("pending", "approved", "rejected")


class ChatSession(Base):
    """一次对话。"""

    __tablename__ = "chat_sessions"
    __table_args__ = (
        # 列表页的唯一热查询：某人的会话，按更新时间倒序。
        Index("ix_chat_sessions_owner_updated", "created_by", "updated_at"),
        # 归档的会话默认不出现，但要能捞回来。
        Index("ix_chat_sessions_archived", "archived_at"),
        # ★ persona 的合法值在**数据库层**也钉一遍，不只是 pydantic。
        #
        #   理由与 `chat_message_role_valid` 完全一致：pydantic 只挡 HTTP
        #   入口。这条也挡得住"迁移脚本写错一个值"、"手工 UPDATE 写错"
        #   这类没有经过 API 的写入 —— 而那种值一旦进库，`prompts.py`
        #   查不到对应人格时会静默退回默认，界面上看起来一切正常。
        #   `persona IS NULL` 必须放行：NULL 是"默认人格"，不是非法值。
        CheckConstraint(
            "persona IS NULL OR persona IN "
            f"({', '.join(repr(p) for p in CHAT_PERSONAS)})",
            name="chat_session_persona_valid",
        ),
        # 侧栏按项目分组列会话。见迁移 `e8f3c2a71b54` 里那一节：
        # 它主要不是为了性能，而是让同名项目的分组有确定顺序。
        #
        # ★ 这行**必须**与迁移里 `create_index` 那一段同时存在。
        #   只在迁移里建、不在模型里声明，会让 `alembic check` 永远想
        #   生成一条 `remove_index` —— 一条永远清不掉的噪音 diff，
        #   会训练人忽略 autogenerate 的输出。（这次正是这么发现的。）
        Index("ix_chat_sessions_owner_project", "created_by", "project"),
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

    #: 置顶时刻。`None` = 没置顶。
    #:
    #: ★ 为什么存时刻而不是一个 `pinned bool`：
    #:   置顶多条时需要**它们之间**的顺序。用 bool 就只能拿 `updated_at`
    #:   兜底，而 `updated_at` 会因为"又聊了两句"而变 —— 用户置顶的三条
    #:   会自己换位置。存时刻则「最近置顶的在上」是确定的，且不需要第二列。
    #:   撤销置顶就是写回 `NULL`。
    pinned_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=now_utc, onupdate=now_utc
    )

    #: 消息数。冗余但必要：列表页要显示"12 条"，而为一个数字
    #: 对每个会话跑一次 COUNT 是 N+1。
    #: ⚠️ 它必须由写消息的那一处同步维护，不能指望它自愈。
    #:
    #: `server_default="0"` 必须与迁移 `d4e8b2f6a1c9` 里的声明一致。
    #: 只在迁移里写、模型里漏掉，`alembic check` 就会永远想生成一条
    #: `modify_default` —— 与上面那个索引是同一类漂移。
    message_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # ── 以下是 v0.94 的「对话设置」六列 ──────────────────────────
    #
    # ★ 为什么这些设置挂在**会话**上而不是每条消息上：
    #   用户在侧栏里切了一个「角色」，他的意思是「这个对话接下来都按
    #   这个角色说」，而不是「这一句按新角色、下一句按旧的」。挂消息上
    #   会让同一个对话的上下文在角色之间反复横跳，而模型看到的 system
    #   提示词每轮都变 —— 那既贵又会让它自己都糊涂。
    #
    # ★ 为什么不给它们建一张 `chat_session_settings` 表：
    #   六列全是标量/短列表，且**每个会话必读**（跑一轮就要全部拿到）。
    #   独立表意味着每轮多一次 JOIN 或一次额外 SELECT，收益只有"加列的
    #   时候好看一点"。等到真的出现「一个会话多条设置历史」这种需求再拆。

    #: 归属的项目名。`None` = 未归属（侧栏归到「无项目」）。
    #:
    #: ★ 存**名字**而不是 project_id：项目是给用户分组用的便签，
    #:   改个名不该让历史对话从分组里掉出去。而真建一张 projects 表
    #:   就要处理"删项目时那些对话怎么办"，那是与这一页无关的复杂度。
    project: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: 角色（persona）id。见 `prompts.CHAT_ROLES`。`None` = 默认角色。
    #: ⚠️ 与 `models.CHAT_ROLES`（消息角色 user/assistant/tool）是**两件
    #:   完全不同的事**，名字撞了纯属都叫"角色"。这个存的是"人格"。
    persona: Mapped[str | None] = mapped_column(String(32), nullable=True)

    #: 用哪个模型接入点。`None` = 用平台默认接入点。
    #:
    #: ★ 存 id 而不是快照 model 名：接入点被删了就该退回默认，
    #:   而不是拿一个已经不存在的 model 去请求。
    provider_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: 是否要求人工审批有副作用的操作（`create_scan` / `save_skill` /
    #: `delete_skill` / `add_mcp_server` / `start_ctf_solve`）。
    #:
    #: ★ 默认 `False`（关闭）：这是给「我要盯着它一步一步做」的场景用的，
    #:   默认打开会让每一次建扫描都要多点一次，用户很快就会去关掉它 ——
    #:   而那之后就再也不开了。默认关、让在意的人自己开，比默认开、
    #:   被所有人关掉更有用。
    require_approval: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )

    #: 这个对话里额外挂载的技能名（会把它们的正文注入系统提示词）。
    #:
    #: ★ 与 `load_skill` 工具的区别：那个是模型**按需**读，这个是用户
    #:   **预先**指定"这个对话就按这几篇方法论来"。前者靠模型判断该读什么，
    #:   后者靠人判断 —— 有些流程人比模型清楚。
    skill_names: Mapped[list | None] = mapped_column(JsonColumn, nullable=True)

    #: 这个对话里额外挂载的 MCP 服务器名（来自 `settings.mcp_servers`）。
    #:
    #: ★ 存名字而不是存配置：配置的唯一真相是设置页那一列。这里存名字
    #:   意味着"设置页删了某个 server，这个对话自动就不挂它了"，
    #:   而不是留一份过期副本继续连。
    mcp_server_names: Mapped[list | None] = mapped_column(JsonColumn, nullable=True)

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

    @property
    def pinned(self) -> bool:
        """给 `ChatSessionOut.pinned` 读的派生属性。理由与 `archived` 同。"""
        return self.pinned_at is not None

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
        # ★ 审批状态只允许三种值。写死而不是放过：一个拼错的状态值
        #   会让界面上那条消息**永远显示"等待确认"而没人能点** ——
        #   用户以为对话卡住了，刷新也没用。
        CheckConstraint(
            "approval_status IS NULL OR approval_status IN "
            f"({', '.join(repr(s) for s in APPROVAL_STATUSES)})",
            name="chat_message_approval_status_valid",
        ),
        # 读一个会话的全部消息：`WHERE session_id = ? ORDER BY seq`
        Index("ix_chat_messages_session_seq", "session_id", "seq"),
        # 进会话时找"有没有挂着的确认"。
        # ★ 部分索引：只索引 pending 的行。绝大多数消息 approval_status
        #   是 NULL，全列索引会大得没道理，而这条查询只关心 pending。
        Index(
            "ix_chat_messages_pending_approval",
            "session_id",
            postgresql_where=text("approval_status = 'pending'"),
        ),
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

    #: ★ v0.112：**只给模型看、不给用户看**的附加材料，可空。
    #:
    #: 目前的唯一来源是附件注入（`engine._render_attachments`）：附件正文、
    #: 容器内真实路径、以及"用 binwalk / tcpdump 去分析"这类**我们写给
    #: 模型的指令**。这些东西拼进 `content` 的后果是界面上看起来像用户
    #: 自己打了一大段 —— 与 `_narration_before_action` 那条判例同一个形状
    #: （内部协议进了用户视野，复核时无法判断某句话是模型说的还是系统拼的）。
    #:
    #: 组装模型上下文时由 `_render_history` 拼回 `content` 之后，所以模型
    #: 看到的东西与以前逐字节一致；界面只渲染 `content`，于是那段材料
    #: 从用户视野里消失。
    #:
    #: ★ `NULL` 与 `''` 语义不同：NULL = 没有注入材料（绝大多数消息）。
    #:   读侧一律 `(row.model_extra or "")`，不写 `is NULL` 的特殊分支。
    model_extra: Mapped[str | None] = mapped_column(Text, nullable=True)

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

    # ── 人工审批三列（v0.94）────────────────────────────────────
    #
    # `role == 'tool'` 且 `approval_status == 'pending'` 的行，表示
    # 「模型想调这个工具，还在等用户点确认」。此时 `content` 里写的是
    # "等待确认"，而**工具还没执行过**。
    #
    # ★ 为什么是同一行上的三个字段而不是一张 `chat_approvals` 表：
    #   审批与那条工具消息是一对一的，而且批准后这条消息的内容就要
    #   变成执行结果。分表会造出"消息说等待、表说已批、结果还没写"
    #   这种半状态，界面得为它写专门分支。同一行改三个字段永远原子。
    #   详细理由见迁移 `e8f3c2a71b54` 里那一节。

    #: `None`（与审批无关）/ `'pending'` / `'approved'` / `'rejected'`。
    #:
    #: ★ 不用 `'none'` 这种假值：让"有没有审批"用一个 `IS NULL` 就能问。
    approval_status: Mapped[str | None] = mapped_column(String(16), nullable=True)

    #: 谁点的确认/拒绝。按用户 id 存，不存用户名 —— 用户名可以改。
    approval_decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: 什么时候点的。与 `approval_decided_by` 同时有值或同时为空。
    approval_decided_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime, nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=now_utc)

    session: Mapped[ChatSession] = relationship(back_populates="messages")

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<ChatMessage {self.session_id!r}#{self.seq} {self.role} n={len(self.content)}>"


__all__ = [
    "APPROVAL_STATUSES",
    "CHAT_PERSONAS",
    "CHAT_ROLES",
    "MAX_MESSAGE_CHARS",
    "MAX_PROJECT_CHARS",
    "MAX_TITLE_CHARS",
    "ChatMessage",
    "ChatSession",
]
