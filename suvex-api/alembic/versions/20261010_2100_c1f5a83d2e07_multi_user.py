"""admin_accounts 加 username 与 role —— 多用户（v0.24）

Revision ID: c1f5a83d2e07
Revises: b9e4f17a2c60
Create Date: 2026-10-10 21:00:00.000000+00:00

## 为什么现在加

此前是**单管理员**：登录只输口令，因为"一个账号的系统里要用户名词
只是多一个必填框"（`auth/schemas.py` 的原文）。而多用户之后，
登录必须能区分是谁 —— 否则审计日志里的"谁"只能记成会话 id，
而那回答不了"上周是谁把 submitMode 改成 auto 的"。

`role` 则是权限分档的依据（`admin` / `viewer`，见 `core.vocab.UserRole`）。

## ★ 存量那一行怎么回填

这是本迁移**唯一需要小心**的地方。两张约束同时压过来：

  · `username` 是 **NOT NULL + UNIQUE**，而存量行没有这个值；
  · `role` 是 NOT NULL，存量行也没有。

回填的取值不是随意的：

  · **`role = 'admin'`** —— 存量那一行是升级前唯一的管理员。
    回填成 `viewer` 会让升级后**没人能改设置**，那是一个自锁。
  · **`username = 'admin'`** —— 一个可读、可预期的名字，而且
    升级说明里要写清"首次登录请用 admin + 你原来的口令"。

⚠️ 如果库里**已经有**一行 username='admin'（不可能，但迁移要能重跑
   的假设不该建立在"不可能"上），`UNIQUE` 会拒绝 —— 那时迁移失败并
   回滚，比静默造出重复值好。

## 为什么不给 role 加 CHECK 约束

与项目里其它枚举列的做法不同，这里是刻意的：`role` 的取值集合
**将来最可能变**（加一档"能扫描但不能改设置"是很自然的演进），
而每加一档就要写一个迁移重建 CHECK —— 与 `TargetKind` 那条
"加枚举值要手写迁移"的坑是同一个。

未知角色的处理在 `scopes_for_role`：返回**空集**（表现为 403），
而不是给一个默认角色。安全的方向是"什么都不给"。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c1f5a83d2e07"
down_revision = "b9e4f17a2c60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ① 先加**可空**的列。
    #
    # 不能直接加 NOT NULL —— 存量行会立刻违反约束，而 Postgres 在
    # 同一个事务里既加列又回填时，NOT NULL 的检查发生在**语句执行时**，
    # 不是提交时。所以顺序必须是：加可空列 → 回填 → 设 NOT NULL。
    op.add_column(
        "admin_accounts",
        sa.Column("username", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "admin_accounts",
        sa.Column("role", sa.String(length=16), nullable=True),
    )

    # ② 回填存量行。
    #
    # ⚠️ 用 `WHERE username IS NULL` 而不是无条件 UPDATE：这条迁移
    #    在"全新库"上跑时表是空的，UPDATE 影响 0 行 —— 而那是正常的。
    #    无条件 UPDATE 在那种情况下也不报错，但它表达的意思是
    #    "把所有行都改成 admin"，那在有多行的将来是错的。
    op.execute(
        """
        UPDATE admin_accounts
           SET username = 'admin',
               role = 'admin'
         WHERE username IS NULL
        """
    )

    # ③ 设成 NOT NULL + 唯一索引。
    op.alter_column("admin_accounts", "username", nullable=False)
    op.alter_column("admin_accounts", "role", nullable=False)

    # 唯一索引同时承担「登录查找」与「防重名」两个职责 ——
    # 与 `auth_sessions.token_hash` 同一条思路。
    op.create_index(
        "ix_admin_accounts_username",
        "admin_accounts",
        ["username"],
        unique=True,
    )


def downgrade() -> None:
    """回退到单管理员。

    ⚠️ **这会丢掉所有非 admin 账号与它们的角色信息**，而且不可恢复。

    之所以还是给一个 downgrade：迁移必须能回退，否则出问题时只能
    手工改库 —— 而手工改库没有任何记录。回退前请确认你知道会丢什么。

    注意这里**不删账号行**（只删列）：多出来的账号会退化成
    "只有口令、没有用户名"的状态，而单管理员代码只读第一行 ——
    也就是说它们变成不可登录但数据还在。那比直接删掉更保守。
    """
    op.drop_index("ix_admin_accounts_username", table_name="admin_accounts")
    op.drop_column("admin_accounts", "role")
    op.drop_column("admin_accounts", "username")
