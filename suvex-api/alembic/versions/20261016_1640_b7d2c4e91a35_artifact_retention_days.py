"""scan_retention_config.artifact_retention_days：归档产物的保留期（缺口清单「扫描产物目录只增不减」）

Revision ID: b7d2c4e91a35
Revises: a1c7e4b92f68
Create Date: 2026-10-16 16:40:00.000000+00:00

## 这次加一列，修的是 README 缺口清单「扫描产物目录只增不减」那一条的**后半段**

"扫描产物目录只增不减"有两个来源，2026-10-11 那次只修了第一个：

| 来源 | 谁负责 | 状态 |
| --- | --- | --- |
| 数据库里**已无记录**的孤儿目录 | `runner.sweep_orphan_artifacts` | ✅ 已修 |
| 记录还在、但**早已归档**的目录 | ★ `runner.sweep_retired_artifacts` | 本次补 |

第二个才是主因：归档是**自动**的（`sweep_retention` 到点就打
`archived_at`），而产物在那之后永远留着。按每天 20 次扫描估，
README 里那个 43 GB/年 的量级就是这么来的。

## 为什么是"加一列"而不是复用 `archive_after_days`

那两个天数回答的是**不同问题**，合并会让两边都变得无法解释：

* `archive_after_days` —— "什么时候把它从列表里收起来"（**可逆**，
  取消归档就回来了）；
* `artifact_retention_days` —— "什么时候把磁盘上的产物删掉"
  （**不可逆**，删了就没了）。

默认值也不同（30 天 vs 90 天），因为不可逆的那一步该留更久。
合并成一个数意味着"想早点归档"的人会连产物一起早早删掉。

## 为什么 `server_default="90"` 而不是只写 `nullable=False`

表里**很可能已经有行**（用户打开过设置页就会建单例行，见
`get_retention_config` 的"读时建行"）。只写 `nullable=False` 而不给
`server_default`，在已有行的表上加列会**直接失败**（现有行违反非空约束）。

⚠️ 这里给的是 `server_default`，与项目"配置的初值放在代码里、不用
数据库默认值"那条纪律**不冲突**：那条纪律管的是**新建行**时用什么初值
（由 `models.py` 的 `default=` 负责，这里也是 90）。`server_default`
在这里的作用只有一个 —— 让**已经存在的行**在加列时有一个合法值。
两处写同一个数字，所以不会漂移出"两套默认值"。

## `downgrade` 是真能回退的

只加了一列，回退就是删它。**不会丢数据**（产物的清理是磁盘行为，
回退之后那些已被清理的目录不会自己回来 —— 但那是磁盘层面的既成事实，
与这次迁移无关）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7d2c4e91a35"
down_revision: str | None = "a1c7e4b92f68"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "scan_retention_config",
        sa.Column(
            "artifact_retention_days",
            sa.Integer(),
            nullable=False,
            server_default="90",
        ),
    )
    op.create_check_constraint(
        "artifact_days_non_negative",
        "scan_retention_config",
        "artifact_retention_days >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "artifact_days_non_negative", "scan_retention_config", type_="check"
    )
    op.drop_column("scan_retention_config", "artifact_retention_days")
