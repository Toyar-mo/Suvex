"""`suvex_config.disabled_tools` —— 内置工具的启用开关（v0.82）。

## 为什么加这一列

v0.81 把 MCP 连接做成了可管理的清单，但那是**外部**工具。智能体内置的
13 个工具（`run_shell` / `http_request` / `recon_scan` …）一直只硬编码在
`runner.TOOL_NAMES` 里，部署者既看不见清单、也不能按任务收窄。

这个列的用途**不是安全隔离**（隔离在沙箱、hostguard、URL 白名单里），
而是"这一次任务我不想让模型用某几样东西"。写错它的后果是能力变差，
不是越权。

## 为什么可空，而不是 `default '[]'`

同 `mcp_servers`（迁移 `b3e6a1d94f27`）的纪律：`NULL` = "从没配过"。
这一次的差别比那一列更实在 —— 见 `KernelConfig.disabled_tools` 的说明，
"关掉哪些"这个方向决定了新增工具默认可用，而 `NULL` 让这个默认
在数据库层就能被区分出来。

⚠️ 但**写入路径**仍然把空清单统一存成 `NULL`（见
`kernel.service.save_tool_settings`），所以库里只有一种空表示。
两句话不矛盾：可空是为了"从没配过"这个状态可表达，统一写 `NULL`
是为了不让 `[]` 和 `NULL` 同时出现在库里去表达同一个意思。

## 为什么不上 CHECK 约束

清单里的工具名会随 `TOOL_NAMES` 增长。把候选写进 CHECK 意味着
**每加一个工具都要改一次迁移**，而漏改的症状是"新工具存不进去"
（400 而不是静默）—— 那还算好的。更糟的是它给了 CHECK 一个
"这是枚举"的错误暗示：这里允许存任意字符串，未知名字由**读取侧**
忽略并记 warning（见 `kernel.tools.resolve_enabled`），
因为删掉一个工具时不该让旧的配置行变成非法数据。

Revision ID: a9d4f2c71e83
Revises: f4a7c9e2d183
Create Date: 2026-10-24 01:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "a9d4f2c71e83"
down_revision = "f4a7c9e2d183"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "suvex_config",
        sa.Column(
            "disabled_tools",
            # 与 `app/core/columns.py::JsonColumn` 同一个类型：PostgreSQL 上
            # 是 JSONB，其余是 JSON。写死 JSONB 会让 SQLite 上的测试跑不起来。
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
            comment="被显式关掉的内置工具名清单（管理员配置，模型不可改）",
        ),
    )


def downgrade() -> None:
    op.drop_column("suvex_config", "disabled_tools")
