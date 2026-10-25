"""`chat_sessions` 加六列「对话设置」+ 修 `title` 长度漂移（v0.94）。

## 为什么加这六列

对话页从 v0.93 的"能聊天"要做到 v0.94 的"能当工作台用"：用户要能选项目、
选人格、选模型、挂技能、挂 MCP、开人工审批。这些设置都挂在**会话**上
（理由见 `app/chat/models.py` 里 `project` 上方的注释：用户切角色时
他的意思是"这个对话接下来都这样"，不是"这一句这样"）。

## 为什么这里顺手修 `title` 的长度

**这是一个真实的漂移，不是风格问题。**

- `app/chat/models.py` 里是 `String(MAX_TITLE_CHARS)`，`MAX_TITLE_CHARS = 80`
- `app/chat/schemas.py` 用 `Field(max_length=MAX_TITLE_CHARS)` 卡 80
- `app/chat/service.py` 的自动标题是 32 字（`_AUTO_TITLE_CHARS`），
  用户手改标题也被 `cleaned[:MAX_TITLE_CHARS]` 截到 80

三处应用层代码都按 80 设计，但 `d4e8b2f6a1c9`（v0.93 建表）里写的是
`sa.String(length=200)`。结果是**模型与库在这一点上不一致**，而它不会
报错：Postgres 按 200 收，应用层按 80 截，两边都"正常工作"，只是
`alembic revision --autogenerate` 从此永远想生成一条 `ALTER COLUMN`
把它抹平 —— 一条永远清不掉的噪音 diff，会训练人忽略 autogenerate 的
输出，那正是本仓库反复强调要避免的"配了却静默失效"的同一种形状。

收窄到 80 是安全的：迁移前实测本机库 `chat_sessions` 1 行、
`max(length(title)) = 7`、`count(>80) = 0`。**但迁移本身不能假设这一点**
（别人的库里可能真有一条 200 字的标题），所以 upgrade 里先 `UPDATE`
截断再 `ALTER`，顺序反了会在长标题上炸。

## 枚举值抄一遍而不 import 模型

与 `d4e8b2f6a1c9` 同样的理由：迁移是历史快照，必须永远生成当初那张表。
import `app.chat.models` 会让将来改模型时这个迁移悄悄生成不一样的表。

Revision ID: e8f3c2a71b54
Revises: d4e8b2f6a1c9
Create Date: 2026-10-26 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "e8f3c2a71b54"
down_revision = "d4e8b2f6a1c9"
branch_labels = None
depends_on = None

#: ⚠️ 与 `app/chat/models.py::CHAT_PERSONAS` 必须逐字一致。
#: `app/chat/prompts.py` 里有一条断言钉住"两处同一组"，它会先炸，
#: 这样至少是导入期就发现，而不是等到某个人的对话退化成默认人格。
_CHAT_PERSONAS = (
    "default",
    "recon",
    "pentest",
    "binary",
    "postex",
    "container",
    "forensics",
)

#: 与 `app/chat/models.py::MAX_TITLE_CHARS` 一致。
_TITLE_CHARS = 80

#: 与 `app/chat/models.py::MAX_PROJECT_CHARS` 一致。
_PROJECT_CHARS = 64


def upgrade() -> None:
    # ── 1. 先修 title 长度漂移 ─────────────────────────────────
    #
    # ★ 截断必须在 ALTER 之前。顺序反了的话，库里只要有一条超过 80 字的
    #   标题，`ALTER COLUMN TYPE varchar(80)` 就会直接失败（Postgres 不会
    #   帮你截，它报 value too long）。先 UPDATE 让数据合法，再加约束。
    op.execute(
        sa.text(
            "UPDATE chat_sessions SET title = left(title, :n) "
            "WHERE length(title) > :n"
        ).bindparams(n=_TITLE_CHARS)
    )
    op.alter_column(
        "chat_sessions",
        "title",
        existing_type=sa.String(length=200),
        type_=sa.String(length=_TITLE_CHARS),
        existing_nullable=False,
    )

    # ── 2. 六列对话设置 ────────────────────────────────────────
    #
    # 全部 nullable=True（`require_approval` 除外，它有 server_default）：
    # 已有的会话行必须能原样存活。给 `persona` / `provider_id` 设
    # NOT NULL + 默认值也行，但那样"默认人格"与"没选过人格"在库里就
    # 分不清了 —— 而界面上它们是两件事（前者是用户主动选了 default）。
    # NULL 在这里是个有意义的值，不是"忘了填"。
    op.add_column(
        "chat_sessions",
        sa.Column("project", sa.String(length=_PROJECT_CHARS), nullable=True),
    )
    op.add_column(
        "chat_sessions",
        sa.Column("persona", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "chat_sessions",
        sa.Column("provider_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "chat_sessions",
        sa.Column(
            "require_approval",
            sa.Boolean(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "chat_sessions",
        sa.Column("skill_names", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "chat_sessions",
        sa.Column(
            "mcp_server_names", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
    )

    # ── 3. persona 的 CHECK ────────────────────────────────────
    #
    # ★ 与 `chat_message_role_valid` 一样的理由：pydantic 只挡 HTTP 入口。
    #   这条也挡得住"迁移脚本写错"、"手工 UPDATE 写错"这类不经 API 的写入。
    #   那种值一旦进库，`prompts.py` 查不到人格会**静默退回默认** ——
    #   界面上一切正常，而用户选的人格根本没生效。
    #
    # ★ `persona IS NULL` 必须放行：NULL = "没选过"，不是非法值。
    op.create_check_constraint(
        "chat_session_persona_valid",
        "chat_sessions",
        "persona IS NULL OR persona IN "
        f"({', '.join(repr(p) for p in _CHAT_PERSONAS)})",
    )

    # ── 3. `chat_messages` 上的审批三列 ─────────────────────────
    #
    # ## 为什么审批状态要落到工具消息行上
    #
    # 「人工审批」的语义是：模型想调 `create_scan`，系统**先不执行**，
    # 把这件事挂起来等用户点确认。而"挂起来"必须能跨 HTTP 请求 ——
    # 用户可能十分钟后才回来点。所以它必须进库。
    #
    # 落在**工具消息行**上而不是单独建 `chat_approvals` 表：
    # 一条待审批的调用与那条工具消息是**一对一**的，而且审批完之后
    # 那条消息的内容就要变成执行结果。分表会造出"工具消息说等待确认、
    # 审批表说已批准、但结果消息还没写"这种半状态，界面上就得处理
    # 三种中间态。同一行上改三个字段则永远是原子的。
    #
    # `NULL` = 这条工具消息与审批无关（绝大多数消息）。不用 `'none'`
    # 这种假值，是为了让"有没有审批"这件事用一个 `IS NULL` 就能问。
    op.add_column(
        "chat_messages",
        sa.Column("approval_status", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "chat_messages",
        sa.Column("approval_decided_by", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "chat_messages",
        sa.Column("approval_decided_at", sa.DateTime(timezone=True), nullable=True),
    )

    # CHECK：只允许三种状态。`pending` 是唯一"还没结束"的状态。
    # 写死而不是放过，理由与 persona 那条一样 —— 一个拼错的状态值
    # 会让界面上那条消息永远显示"等待确认"，而没人能点。
    op.create_check_constraint(
        "chat_message_approval_status_valid",
        "chat_messages",
        "approval_status IS NULL OR approval_status IN "
        "('pending', 'approved', 'rejected')",
    )

    # 待审批查询的索引：进会话时要找出"这个对话有没有挂着的确认"。
    # 部分索引（只索引 pending）比全列索引小得多，而这条查询只关心 pending。
    op.create_index(
        "ix_chat_messages_pending_approval",
        "chat_messages",
        ["session_id"],
        postgresql_where=sa.text("approval_status = 'pending'"),
    )

    # ── 4. 项目分组的查询索引 ──────────────────────────────────
    #
    # 侧栏要按项目分组列会话：`WHERE created_by = ? AND archived_at IS NULL
    # GROUP BY project`。已有 `ix_chat_sessions_owner_updated` 能覆盖
    # `created_by` 前缀，但带上 project 之后仍要回表 —— 会话量级不大
    # （一个人几十上百条），这个索引不是为了性能，是为了**让 GROUP BY
    # 有确定的顺序**，避免同名项目的分组在两次刷新之间换位置。
    op.create_index(
        "ix_chat_sessions_owner_project",
        "chat_sessions",
        ["created_by", "project"],
    )


def downgrade() -> None:
    # ★ 逆序。先拆索引和 CHECK，再拆列，最后才动 title。
    op.drop_index("ix_chat_sessions_owner_project", table_name="chat_sessions")

    op.drop_index(
        "ix_chat_messages_pending_approval", table_name="chat_messages"
    )
    op.drop_constraint(
        "chat_message_approval_status_valid", "chat_messages", type_="check"
    )
    op.drop_column("chat_messages", "approval_decided_at")
    op.drop_column("chat_messages", "approval_decided_by")
    op.drop_column("chat_messages", "approval_status")

    op.drop_constraint("chat_session_persona_valid", "chat_sessions", type_="check")

    op.drop_column("chat_sessions", "mcp_server_names")
    op.drop_column("chat_sessions", "skill_names")
    op.drop_column("chat_sessions", "require_approval")
    op.drop_column("chat_sessions", "provider_id")
    op.drop_column("chat_sessions", "persona")
    op.drop_column("chat_sessions", "project")

    # ⚠️ downgrade 把 title 放回 200 **不会**恢复被 upgrade 截掉的内容 ——
    #    那些字节已经没了。这是单向的信息损失，写在剩余的列上而不是
    #    假装它能还原。真需要原文的话，upgrade 之前得自己备份。
    op.alter_column(
        "chat_sessions",
        "title",
        existing_type=sa.String(length=_TITLE_CHARS),
        type_=sa.String(length=200),
        existing_nullable=False,
    )
