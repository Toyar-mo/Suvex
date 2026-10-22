"""ctf2_config：CTF2 解题策略（v0.20）

Revision ID: f7b3c2a91e04
Revises: e4a7b2c9d135
Create Date: 2026-09-28 14:00:00.000000+00:00

## 为什么是**一张新表**，不是往 sandbox_config 加三列

见 `app/settings/models.py` 里 `KernelConfig` 的说明：那张表回答
"扫描跑起来能动什么"（跨领域通用的执行参数），这张表回答
"CTF2 的解题怎么对外提交"（领域专属的业务策略）。

判据是"换掉 CTF2 这个领域，这些列还存在吗"—— 不存在，
所以它不该住在那张表里。

## ★ CHECK 是**手写**的，而且上限写成字面量

任务清单里点名要求过，本项目也真付过代价（把 `FindingStatus` 从
4 个值扩到 6 个时漏了 CHECK，于是新状态写不进去 —— 复盘见迁移
`b1f7c0a25e38`）。根因是 `--autogenerate` 对 CHECK **只比对
"有没有这条约束"，不比对里面的表达式**。

⚠️ `max_wrong_submissions <= 10` 里的 10 与
`app/kernel/service.py` 的 `MAX_WRONG_SUBMISSIONS_CEILING` 是**同一个数**，
但这里刻意写字面量：CHECK 表达式建好就固化在库里，而常量可以改 ——
写常量会让"改了常量但没写迁移"表现为"应用层通过、数据库拒绝"。
**改任一处必须同时改另一处**，这条注释就是那个提醒。

## 为什么单例行**不在迁移里插**

与 `sandbox_config` 的处理一致：迁移里写死的初值会在"将来默认值变了"
之后变成一份**没人记得来历的历史值**（它按当时那版默认值建的，
而代码已经改了）。让第一次读它的代码去建行
（`ctf2.service.get_ctf2_config`），那一行就永远带着"建立时那版
默认值"的出处。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# 迁移版本标识，由 alembic 分配，不要手改
revision: str = "f7b3c2a91e04"
down_revision: str | None = "e4a7b2c9d135"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ctf2_config",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("submit_mode", sa.String(length=16), nullable=False),
        sa.Column("max_wrong_submissions", sa.Integer(), nullable=False),
        sa.Column("default_sandbox_profile", sa.String(length=32), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "submit_mode IN ('safe', 'auto')",
            name=op.f("ck_ctf2_config_submit_mode_valid"),
        ),
        sa.CheckConstraint(
            "default_sandbox_profile IN ('ctf2-sandbox', 'ctf2-sandbox-re')",
            name=op.f("ck_ctf2_config_default_sandbox_profile_valid"),
        ),
        sa.CheckConstraint(
            "max_wrong_submissions >= 0 AND max_wrong_submissions <= 10",
            name=op.f("ck_ctf2_config_max_wrong_submissions_within_ceiling"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ctf2_config")),
    )


def downgrade() -> None:
    # ⚠️ 回滚会**丢掉用户设过的提交策略**（含"是否允许自动提交"）。
    #    这是不可逆的，但降级本来就是这样 —— 记在这里，
    #    让执行降级的人知道降完之后设置页会回到 safe 默认。
    op.drop_table("ctf2_config")
