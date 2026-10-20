"""智能体安全场景包：配置开关 + 方法卡 verdict（v0.40）

Revision ID: e5c8a2d71b93
Revises: d8b3f5c21e47
Create Date: 2026-10-20 03:30:00.000000+00:00

## 这一版加两列，它们回答的是**同一个问题的两半**

1. `ctf2_config.enable_agentsec_scenarios`（BOOL，默认 false）——
   **要不要**启用铸网-2026 那四个 `agentsec-*` 题型；
2. `ctf2_solve_notes.verdict`（VARCHAR(20)，默认 `needs_follow_up`）——
   一张方法卡是**怎么"解决"**的（命中 / 排除 / 待跟进）。

它们必须一起加，因为不加第二列，第一列就只有半个意义：那四个题型的
核心产物是"判定结论"，而 `ruled_out`（跑完全套手法后确认不存在）在
旧的 `solved: bool` 里会被记成"未解决"，于是下次同类任务排在
未解出的卡片后面、还可能被直接跳过 —— 卡片的价值就丢了。

## 为什么 `enable_agentsec_scenarios` 用 server_default='false' 而不是只靠 ORM default

表里已经有行（现有部署的 `ctf2_config` 单例行必在）。`nullable=False`
且没有 server_default 会让 `ALTER TABLE` 在非空表上直接失败；而给一个
可变默认值又会让"加列那一刻的老行"拿到一个没被任何人选过的值。

`'false'` 在这里是**唯一正确**的填充值：那四个题型本来就要求显式打开
（理由见 `settings/models.py` 的 `enable_agentsec_scenarios`）。
老行拿到 false = 保持既有行为，正是我们要的。

## 为什么 `verdict` 也回填而不是留 NULL

`verdict` 的语义是"这张卡怎么解决的"。对**已有**的卡，我们确实
不知道它当年是命中还是排除 —— 但把它标成 `needs_follow_up`
（待跟进）是**如实**的：我们不知道，就该说不知道。

⚠️ 不用 NULL 表达"不知道"：那样每次读都要额外处理一种空值，而
`needs_follow_up` 在业务上本来就是"不知道 / 拿不准"这一档
（见 `vocab.AgentsecVerdict`）。两种空在 SQL 里长得一样，
用 NULL 反而多一种歧义。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e5c8a2d71b93"
down_revision: str | None = "d8b3f5c21e47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ctf2_config",
        sa.Column(
            "enable_agentsec_scenarios",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "ctf2_solve_notes",
        sa.Column(
            "verdict",
            sa.String(length=20),
            nullable=False,
            server_default="needs_follow_up",
        ),
    )
    # CHECK 与 `vocab.AgentsecVerdict` 是同一组取值。
    #
    # ⚠️ 与 `ctf2_config` 上其它 CHECK 同一条纪律：取值写**字面量**而不是
    #    import 那个枚举 —— CHECK 表达式一旦建好就固化在数据库里，而枚举
    #    可以改。写枚举会让"加了取值但没写迁移"表现为"应用层通过、数据库
    #    拒绝"。两者必须一起改，这条注释就是那个提醒。
    op.create_check_constraint(
        "verdict_valid",
        "ctf2_solve_notes",
        "verdict IN ('confirmed', 'ruled_out', 'needs_follow_up')",
    )


def downgrade() -> None:
    op.drop_constraint("verdict_valid", "ctf2_solve_notes", type_="check")
    op.drop_column("ctf2_solve_notes", "verdict")
    op.drop_column("ctf2_config", "enable_agentsec_scenarios")
