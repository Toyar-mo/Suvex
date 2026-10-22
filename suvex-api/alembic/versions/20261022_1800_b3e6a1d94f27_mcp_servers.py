"""MCP server 配置列（v0.72）。

学 strix 的 `list_mcps`/`describe_mcp`/`call_mcp` 三件套 —— 给智能体一个
**通用**的外部工具接入方式，而不是每接一个工具就改一次代码。

## 为什么是一列 JSON 而不是一张表

MCP 配置是**部署者手填的一小段**（通常 0–3 条），读的时候总是整体读、
写的时候总是整体写（设置页一次保存整份），没有"按某条查询"的需求。
开一张表要多一套 CRUD、外键、迁移，而收益只是"看起来更规范"。

⚠️ 但它确实放弃了"DB 层能校验条目形状"——所以解析在应用层做：
`app/kernel/mcp.py::parse_server_configs` 逐条校验，坏条目跳过并记
warning（不静默丢弃，否则部署者会以为某条生效了）。

## 为什么可空而不是 `default '[]'`

可空 = `NULL` = "从没配过"，与 `[]`（"配过，但现在清空了"）语义不同。
这一次差别不大，但我们刚在沙箱镜像那件事上吃过"用默认值掩盖真实状态"
的亏，所以按信息量保留两态。

Revision ID: b3e6a1d94f27
Revises: f1a9c4e83d27
Create Date: 2026-10-22 18:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "b3e6a1d94f27"
down_revision = "f1a9c4e83d27"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "suvex_config",
        sa.Column(
            "mcp_servers",
            # 与 `app/core/columns.py::JsonColumn` 同一个类型：PostgreSQL 上是
            # JSONB，其余是 JSON。写死 JSONB 会让 SQLite 上的测试跑不起来。
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
            comment="可用的 MCP server 列表（部署者配置，模型不可改）",
        ),
    )


def downgrade() -> None:
    op.drop_column("suvex_config", "mcp_servers")
