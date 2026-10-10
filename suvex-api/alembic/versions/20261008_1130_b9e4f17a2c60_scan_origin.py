"""scans.origin：标明一条扫描是哪个子系统建的（v0.27）

Revision ID: b9e4f17a2c60
Revises: a7d2e94b1f58
Create Date: 2026-10-08 11:30:00.000000+00:00

## 这个改动来自一次实测的误操作

2026-10-08，用户对扫描列表里那条 `[CTF2] WEB5[中等]` 点了「重跑」，
然后问"为什么没有开始解"。查下来的完整链条：

    rerun_scan 忠实复制 target `ctf2:1cbe06be-…`
    → strix 收到后当场拒绝（Invalid target）
    → argparse 用法错误，退出码 2
    → 而 EXIT_FINDINGS **也是 2**，被记成「已完成 · 0 秒」
    → CTF2 侧零动作，题目状态一个字没变

他点了两次，于是有两条这样的假记录。

## 为什么加一列，而不是"查关联表推出来"

判据本来可以写成：

    EXISTS(SELECT 1 FROM ctf2_challenges WHERE scan_id = scans.id)

**但那个判据挡不住这次事故**：`rerun_scan` 建出的那条新壳
**没有任何 challenge 指向它** —— 查关联表会把它判成真扫描，
而它恰恰是最需要拦住的。所以标记必须在**建行时**落下。

## 回填用的判据（存量数据）

存量行只能按当时的信息回填，而当时**没有** `origin` 列。可用的证据是：

1. `authorized_by = 'ctf2'` —— CTF2 建壳时写死这个值，
   而用户从界面创建的扫描写的是 `user_…` 或 `token:…`；
2. `target LIKE 'ctf2:%'` —— CTF2 的壳用 `ctf2:<remote_id>` 当 target。

★ 两条**同时**成立才算。只看 `authorized_by` 会漏掉"重跑出来的壳"
（重跑会写用户 id）；只看 `target` 则理论上可能误伤一条用户手工建的、
恰好以 `ctf2:` 开头的扫描。

⚠️ 这意味着**重跑出来的那两条假壳回填不进去**（它们的 `authorized_by`
   是用户 id）。它们由部署脚本单独修正 —— 那是一次性的数据修复，
   不该混进迁移里（迁移要对**任何**实例都正确，而那些实例可能没有
   这两条记录，也可能有人真的建过合法扫描）。

## 为什么不加 NOT NULL

`origin` 为 NULL 表示"一次正常的扫描" —— 那是绝大多数行，
也是唯一合理的默认值。加 NOT NULL + 默认值 `'ui'` 会给将来所有
新子系统都预设一个可能不对的标签，而 NULL 的语义是干净的
"没有特殊来源"。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# 迁移版本标识，由 alembic 分配，不要手改
revision: str = "b9e4f17a2c60"
down_revision: str | None = "a7d2e94b1f58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 `vocab.SCAN_ORIGIN_CTF2` 是同一个字面量。
#
# ⚠️ 迁移**不 import 应用常量**（那是本项目的既定做法：迁移一旦发布就是
#    历史，而应用常量会继续演进）。所以这里独立写着这个字符串，
#    并在上面注明它的来源。
_CTF2 = "ctf2"


def upgrade() -> None:
    op.add_column(
        "scans",
        sa.Column("origin", sa.String(length=16), nullable=True),
    )
    # ── 回填存量行 ────────────────────────────────────────
    #
    # 两个条件同时成立才认（理由见模块 docstring）。
    # 用 `UPDATE ... WHERE` 而不是逐行读改：它是一条语句、
    # 在事务里原子生效，而且**不需要应用层参与**（迁移可能跑在
    # 一个没有 ORM 模型的上下文里）。
    op.execute(
        sa.text(
            """
            UPDATE scans
               SET origin = :origin
             WHERE authorized_by = :origin
               AND target LIKE :prefix
            """
        ).bindparams(origin=_CTF2, prefix="ctf2:%")
    )
    # 部分索引：扫描列表与调度器都要按"排除 CTF2 的壳"过滤，
    # 而这个条件命中的行是少数（当前库里 20 条 / 全部几十条）。
    # 索引建在 origin 上而不是做部分索引 —— 取值会随子系统增加，
    # 部分索引的 WHERE 会把取值写死进索引定义。
    op.create_index("ix_scans_origin", "scans", ["origin"])


def downgrade() -> None:
    op.drop_index("ix_scans_origin", table_name="scans")
    op.drop_column("scans", "origin")
