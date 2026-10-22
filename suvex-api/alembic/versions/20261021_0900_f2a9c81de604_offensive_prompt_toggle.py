"""越权与注入手法块开关（v0.42）

Revision ID: f2a9c81de604
Revises: b7f1c93a2d85
Create Date: 2026-10-21 09:00:00.000000+00:00

## 这一版只加一列

`sandbox_config.enable_offensive_prompt`（BOOL，默认 false）—— 打开后
「解题」与「渗透扫描」两条路径的提示词末尾都追加
`prompts/offensive_playbook.md` 那一块手法矩阵。

## ★ 为什么加在 `sandbox_config`，而不是像前两版那样加在 `ctf2_config`

前两版（`e5c8a2d71b93` 的 agentsec、`b7f1c93a2d85` 的 ir）都是
`ctf2_config`，因为它们只影响解题。这一列不一样：它要**被两条路径
同时读到**。

· 解题侧读 `ctf2_config`（`effective_ctf2_settings`）。
· 渗透扫描侧**完全不读** `ctf2_config` —— `app/scans/` 下对 `ctf2`
  的引用全是注释性的。

而 `sandbox_config` 是唯一**被两边共同读取**的单例行：扫描侧走
`app/scans/service.py` 的 `read_sandbox_config`，解题侧走
`app/kernel/runner.py` 的同名函数。开关放在这里，"两边看到的是同一个
值"由结构保证，而不是靠两处各读一次再祈祷它们同步。

## 为什么 server_default='false'

与前面两版同一条理由：`sandbox_config` 的单例行在既有部署里必已存在，
`nullable=False` 且无 server_default 会让 `ALTER TABLE` 在非空表上
直接失败。而 `false` 是唯一正确的填充值 —— 关着时两条路径的提示词
**逐字节不变**，老行拿到 false = 行为与引入前完全一致。

⚠️ 模型侧（`settings/models.py` 的 `SandboxConfig.enable_offensive_prompt`）
只写 `default=False`、**不写 `server_default`**：迁移负责把老行填成
false，模型负责新写入的默认值。同 `KernelConfig.enable_agentsec_scenarios`。

## 为什么不进 `ExecutionSettings`

`ExecutionSettings` 那三字段（`max_concurrent_scans` / `scan_max_turns` /
`scan_mode`）的语义是「DB 有值就用 DB，没有就跟着 env 走」—— 它们是
**容量类**配置，可空、NULL 有意义。这一列是**行为开关**，非空、
没有 env 兜底，所以两侧直接读 `read_sandbox_config(db)` 上的布尔列即可。
把它塞进 `ExecutionSettings` 会凭空给它造出一个不存在的 env 优先级。

## 为什么不用 CHECK 约束

`verdict` 那类枚举列需要 CHECK（值是有限集合）。布尔列只有两个取值，
数据库的 `Boolean` 类型本身已经把它限死了，再加 CHECK 是重复。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f2a9c81de604"
down_revision: str | None = "b7f1c93a2d85"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sandbox_config",
        sa.Column(
            "enable_offensive_prompt",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("sandbox_config", "enable_offensive_prompt")
