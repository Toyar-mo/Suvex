"""解题遥测记录「这次带没带手法块」（v0.47）

Revision ID: a4f8d2c95b17
Revises: c3d8f4b16a72
Create Date: 2026-10-21 13:00:00.000000+00:00

## 这一版只加一列

`ctf2_solve_telemetry.offensive`（BOOL，默认 false）。

## ★ 为什么必须落库，而不是"需要的时候再倒推"

v0.42 加的 `sandbox_config.enable_offensive_prompt` 是个**全局开关**，
随时可能被改。要回答"打开它到底有没有用"，就得把历史解题按"当时带没带
手法块"切成两组去比。而 `solved` / `turns` / `tool_calls` 只说结果、
不说条件 —— 事后按开关的修改时间去倒推是**不可靠**的：一旦中途改过，
两组就会互相污染，而且没有任何信号提示污染已经发生。

所以把"这次有没有带"在**当时**记下来，一行一个事实。

## 记的是"块真的进了提示词"，不是"开关当时是开的"

两者不等。`agentsec-*` / `ir-*` 七类即使开关开着也**按前缀排除**
（见 `runner.build_system_prompt`），提示词里并没有这个块。真正决定
带不带的是 `SolveSpec.offensive`（开关 ∧ 非场景题），所以这一列记的就是
它 —— 只有这样切出来的两组才互斥、没有歧义。

## 为什么 server_default='false'

与前三版同一条理由：`ctf2_solve_telemetry` 在既有部署里已有历史行，
`nullable=False` 且无 server_default 会让 `ALTER TABLE` 在非空表上直接
失败。而 `false` 是唯一诚实的填充值 —— v0.42 之前不存在这个开关，
那些行本来就该算"没带"。

⚠️ 模型侧（`ctf2/models.py` 的 `KernelSolveTelemetry.offensive`）只写
`default=False`、**不写 `server_default`**：迁移负责把老行填成 false，
模型负责新写入的默认值。同 `SandboxConfig.enable_offensive_prompt`。

## 为什么不用 CHECK 约束

布尔列只有两个取值，`Boolean` 类型本身已经限死，再加 CHECK 是重复。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a4f8d2c95b17"
down_revision: str | None = "c3d8f4b16a72"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ctf2_solve_telemetry",
        sa.Column(
            "offensive",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("ctf2_solve_telemetry", "offensive")
