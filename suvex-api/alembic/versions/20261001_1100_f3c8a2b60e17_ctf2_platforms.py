"""ctf2_platforms：可插拔的平台接入配置（v0.25）

Revision ID: f3c8a2b60e17
Revises: e2b7c5a91d34
Create Date: 2026-10-01 11:00:00.000000+00:00

## 为什么需要这张表

在这之前，"平台"这个概念在代码里是**常量**：`client.DEFAULT_BASE_URL`
写死了 CTF2 的地址，认证方式、端点路径、字段名也全都写死在 Python 里。

比赛要接的那个平台（`api_doc.md`）与 CTF2 的差别不是"地址不同"：

| | CTF2 | 比赛平台 |
| --- | --- | --- |
| 认证 | `Authorization: Bearer <PAT>` | `X-Agent-AccessKey` |
| 成功判据 | HTTP 状态码 | **HTTP 200 + `code == "00000"`** |
| 题目寻址 | 三元组 `(practice_id, challenge_id, sub_flag_id)` | 单个 `exerciseId` |
| 题目列表 | **不存在**（靠公开解法侧门捞 id） | `/ctf/exercise-list` |
| 环境 | 独立状态端点 | 藏在题目详情里 |
| 答案长度上限 | 1000 | **256** |

这些差异必须有个地方放。`ctf2_platforms` 就是那个地方：一行 = 一个
已接入的平台，`spec` 列存它的声明式配置（JSON）。

## `spec` 为 NULL 的语义

内置的 CTF2 驱动带着 CTF2 那种畸形逻辑，**表达不成配置**，所以它的
`spec` 是 NULL，由 `driver = 'ctf2'` 指明走哪段代码。

## ★ 这次迁移**不插任何初始数据**

一个自然的想法是"给内置的 CTF2 插一行，语义统一"。**不这么做**，因为
那会让"账号用的是哪个平台"出现两种表示法（`platform_id IS NULL` 与
`platform_id = <内置行 id>`）。两种表示法并存时，"这个账号到底用哪个
驱动"就变成一个每次都要查两处的问题。

所以约定只有一条：**`platform_id IS NULL` = 用内置的 CTF2**。
列表接口在返回时合成一个虚拟的内置条目供界面展示 —— 那是展示层的事，
不该沉淀成一行需要维护的数据。

## `ctf2_accounts.platform_id` 不加外键

外键会让"删掉一份自动生成的配置"牵连到账号（或反过来拦住删除），
而这两件事在语义上无关。存在性校验放在 service 层。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# 迁移版本标识，由 alembic 分配，不要手改
revision: str = "f3c8a2b60e17"
down_revision: str | None = "e2b7c5a91d34"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 可移植 JSONB（与 `app/core/columns.py` 的 `JsonColumn` 同形）。
_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "ctf2_platforms",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("slug", sa.String(64), nullable=False, unique=True),
        sa.Column("display_name", sa.String(120), nullable=False, server_default=""),
        sa.Column("base_url", sa.String(500), nullable=False, server_default=""),
        sa.Column("driver", sa.String(16), nullable=False, server_default="generic"),
        sa.Column("spec", _JSON, nullable=True),
        sa.Column("source", sa.String(16), nullable=False, server_default="generated"),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_raw", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("source IN ('builtin', 'generated')", name="source_valid"),
        sa.CheckConstraint(
            "status IN ('building', 'draft', 'verified', 'failed')", name="status_valid"
        ),
        sa.CheckConstraint("driver IN ('ctf2', 'generic')", name="driver_valid"),
    )
    op.add_column(
        "ctf2_accounts", sa.Column("platform_id", sa.String(64), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("ctf2_accounts", "platform_id")
    op.drop_table("ctf2_platforms")
