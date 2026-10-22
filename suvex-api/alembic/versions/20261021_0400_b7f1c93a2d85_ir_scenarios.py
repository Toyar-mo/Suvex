"""应急响应场景包：配置开关（v0.41）

Revision ID: b7f1c93a2d85
Revises: e5c8a2d71b93
Create Date: 2026-10-21 04:00:00.000000+00:00

## 这一版只加一列

`ctf2_config.enable_ir_scenarios`（BOOL，默认 false）—— 要不要启用
`ir-*` 那三个应急响应考察点（见 `vocab.IrCategory`）。

## ★ 为什么是**单独一列**，不复用 `enable_agentsec_scenarios`

表面上看两列控制的是同一类事（"要不要启用一个场景扩展包"），
但它们的开关语义**必须独立**：

`validate_note_category` 的守备逻辑是「某族的分类合法 ⟺ 该族的开关
打开」。如果两族共用一个开关，那么打开 agentsec 之后 `ir-*` 分类
**也会通过校验**（因为开关是开的），但 `build_system_prompt` 里
注入哪一块是按**分类**判的 —— 于是拿到 `ir-*` 分类的任务会被放行，
却一句 IR 纪律都没注入。

症状是：一道应急响应任务按攻击模式的提示词跑，去"拿 flag"。
它不会报错，只会做错事。这属于本项目反复记录的那一类静默失效
（同 `vocab.AGENTSEC_CATEGORY_PREFIX` 的说明）。

一列一个开关之后，"分类合法"与"纪律注入"用的是同一个判据，
不可能分叉。

## 为什么 server_default='false'

与上一版（`e5c8a2d71b93`）完全同一条理由：`ctf2_config` 的单例行在
既有部署里必已存在，`nullable=False` 且无 server_default 会让
`ALTER TABLE` 在非空表上直接失败。而 `false` 是唯一正确的填充值 ——
那三个考察点本来就要求显式打开（理由见 `settings/models.py` 的
`KernelConfig.enable_ir_scenarios`），老行拿到 false = 行为与引入前
完全一致。

## 为什么不改 `ctf2_solve_notes` 的 CHECK

`verdict` 的三个取值（`confirmed` / `ruled_out` / `needs_follow_up`）
是**域无关**的证据分级，`agentsec-*` 与 `ir-*` 共用同一组
（见 `vocab.EVIDENCE_GRADES` 的说明）。所以这一版碰不到那个约束 ——
这也正是当初把它设计成域无关的直接好处：加第四个场景包时，
证据分级那一层零改动。

## 为什么不改 `category` 的列宽

`ctf2_solve_notes.category` 是 `String(64)`，`ir-root-cause` 只有
13 个字符，比现有的 `agentsec-prompt-injection`（24）短。
没有理由动它。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7f1c93a2d85"
down_revision: str | None = "e5c8a2d71b93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ctf2_config",
        sa.Column(
            "enable_ir_scenarios",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("ctf2_config", "enable_ir_scenarios")
