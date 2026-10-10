"""findings 软删除 + 附件表（任务 10 P6-a / P6-d）

Revision ID: 99b1799eb3c6
Revises: a1a0945e265f
Create Date: 2026-09-25 16:36:55.645280+00:00

## 自动生成的部分

`finding_attachments` 表、它的索引与 `findings.deleted_at` 列都是
`--autogenerate` 抓出来的（新增表 / 新增列 / 新增索引它认得）。

## 手工补的部分

一个**部分索引**（`ix_findings_deleted_at`）。

⚠️ 为什么必须手写：`Index(..., postgresql_where=...)` 这种部分索引
autogenerate **认不全** —— 它会生成一个普通索引，而普通索引与部分索引
是不同的东西。这里显式写出来，避免下次 `--autogenerate` 认为
"该建"而反复生成一次（那正是 `models.py` 里那句注释警告的情形）。

索引的选择理由：列表的常规查询带 `deleted_at IS NULL`（几乎全部行），
而回收站查询是 `deleted_at IS NOT NULL`（极少行）。给**少数的那个**
建部分索引才有价值 —— 给多数派建索引等于给几乎每一行建条目，
而查询计划器最终还是会选择顺序扫描。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# 迁移版本标识，由 alembic 分配，不要手改
revision: str = '99b1799eb3c6'
down_revision: str | None = 'a1a0945e265f'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'finding_attachments',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('finding_id', sa.String(length=64), nullable=False),
        sa.Column('filename', sa.String(length=300), nullable=False),
        sa.Column('content_type', sa.String(length=128), nullable=False),
        sa.Column('size_bytes', sa.Integer(), nullable=False),
        sa.Column('stored_path', sa.String(length=512), nullable=False),
        sa.Column('uploaded_by', sa.String(length=64), nullable=True),
        sa.Column('uploaded_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['finding_id'],
            ['findings.id'],
            name=op.f('fk_finding_attachments_finding_id_findings'),
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_finding_attachments')),
        sa.UniqueConstraint('stored_path', name='uq_finding_attachments_stored_path'),
    )
    op.create_index(
        'ix_finding_attachments_finding_id',
        'finding_attachments',
        ['finding_id'],
        unique=False,
    )

    op.add_column(
        'findings',
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    )
    # ★ 手工补的部分索引，理由见模块 docstring
    op.create_index(
        'ix_findings_deleted_at',
        'findings',
        ['deleted_at'],
        unique=False,
        postgresql_where=sa.text('deleted_at IS NOT NULL'),
    )


def downgrade() -> None:
    op.drop_index('ix_findings_deleted_at', table_name='findings')
    op.drop_column('findings', 'deleted_at')
    op.drop_index('ix_finding_attachments_finding_id', table_name='finding_attachments')
    op.drop_table('finding_attachments')
