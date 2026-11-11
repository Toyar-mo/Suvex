"""自定义工具配方 + 可用性检测结果（v1.91）。

工具管理页从「只读配方列表」升级成有生命周期的清单：自定义配方可增删改，
每条配方的「装没装 / 版本」来自真实探测并可回看上次结果。

## 为什么是两张表而不是一张

* `kernel_custom_recipes` 是**用户资产**：只有用户添加的才有行，删除即消失；
* `kernel_tool_probes` 是**观测结果**：内置与自定义配方都可能有行，重测时
  原地覆盖。

合并会让"删除自定义配方"必须决定"要不要连检测历史一起删"，而这两件事
没有共同的生命周期 —— 配方没了，上次探到"没装"仍然是有用的诊断信息。

## 为什么不给 probes 加外键

它同时指向代码里的内置配方（没有数据库行）与自定义配方。加外键就只能
约束一半，而"只有一半被约束"比完全没有约束更容易让人误判。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "f5a6b7c8d9e0"
down_revision = "e3f4a5b6c7d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kernel_custom_recipes",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("summary", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("group_name", sa.String(length=64), nullable=False, server_default="自定义"),
        sa.Column("scenario", sa.String(length=16), nullable=False, server_default="both"),
        sa.Column("command", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("args", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("default_limitation", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_kernel_custom_recipes_name"),
    )

    op.create_table(
        "kernel_tool_probes",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("found", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("path", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("version", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("probed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("name"),
    )


def downgrade() -> None:
    op.drop_table("kernel_tool_probes")
    op.drop_table("kernel_custom_recipes")
