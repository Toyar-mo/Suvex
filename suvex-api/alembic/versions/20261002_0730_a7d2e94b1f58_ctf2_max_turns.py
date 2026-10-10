"""ctf2_config：解题回合预算可配置（v0.26）

Revision ID: a7d2e94b1f58
Revises: f3c8a2b60e17
Create Date: 2026-10-02 07:30:00.000000+00:00

## 为什么加这一列

实测（2026-10-01，用户截图）两道题都是「回合用尽（40 轮）仍未给出结论」。
把它们的解题过程拉出来看，真实情况与"轮数不够"的直觉**部分吻合、部分相反**：

| 题目 | 轮数 | 工具调用 | 去重后 | 结局 |
| --- | --- | --- | --- | --- |
| `[FlareOn3]GUI`（.NET 逆向） | 40 | 37 | **37**（全不相同） | 未解出 |
| `WEB5[中等]`（PHP 反序列化） | 40 | 35 | 28 | 未解出 |
| `[ACTF2020 新生赛]Exec` | 6 | 5 | 5 | 解出 |
| `流量！鲨鱼！`（MISC） | 16 | 15 | 15 | 解出 |

两道失败题**都不是在兜圈子**：`GUI` 那 37 条命令没有一条重复，它一路分析
到了 `.NET` 程序集的 `decryptBuffer` / `DecompressBuffer` 资源流结构，
然后就到点了。**它是真的需要更多轮**。

而简单题 6 轮就够 —— 所以这个数**没有全局最优**，只有"按题权衡"。
写死 40 等于我们替用户决定"哪道题不配解出来"。

## 存量行给 80

`server_default='80'`：升级后所有实例的预算从 40 变成 80。

⚠️ 这是一次**成本上升**（最坏情况下每道题的 LLM 调用翻倍），所以它值得
   被写在这里而不是悄悄发生。要收紧就调这一列 —— 它现在在设置页里。

## CHECK 与 vocab 的常量是同一对

`MIN/MAX_CTF2_MAX_TURNS` 与这里的字面量必须一起改。理由与
`max_wrong_submissions_within_ceiling` 完全一致：CHECK 表达式建好就
固化在库里，而常量能改 —— 两者不同步会表现成"应用层放行、数据库拒绝"。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# 迁移版本标识，由 alembic 分配，不要手改
revision: str = "a7d2e94b1f58"
down_revision: str | None = "f3c8a2b60e17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ctf2_config",
        sa.Column("max_turns", sa.Integer(), nullable=False, server_default="80"),
    )
    op.create_check_constraint(
        "max_turns_within_range",
        "ctf2_config",
        "max_turns >= 5 AND max_turns <= 500",
    )


def downgrade() -> None:
    op.drop_constraint("max_turns_within_range", "ctf2_config", type_="check")
    op.drop_column("ctf2_config", "max_turns")
