"""finding status check accepts six values

Revision ID: （由 alembic 分配）
Revises: 6d1460fcd7b4

## 为什么需要这条迁移（v0.16）

任务 10 P0 把 `FindingStatus` 从 4 个值扩到 6 个（新增 `retesting` /
`closed`），改了：
  · `core/vocab.FindingStatus` 与流转表 ✓
  · 前端 `finding.ts` ✓
  · 报告模块的状态标签 ✓

**但漏了数据库上那条 CHECK 约束** —— `ck_findings_status_valid` 仍然
只允许 4 个值。于是把一条漏洞标成「已关闭」时会直接撞数据库：

    CheckViolation: new row for relation "findings"
    violates check constraint "ck_findings_status_valid"

这是**最难在单元测试之外发现的那类漂移**：模型层是对的（它用
`values(FindingStatus)` 动态生成），只有真跑到数据库上才炸。而
`alembic --autogenerate` 对 CHECK 约束**内容的**变化检测不可靠 ——
它只比对"有没有这条约束"，不比对里面的表达式。

所以这条迁移是手写的，并且显式 drop + create：
`ALTER TABLE ... DROP CONSTRAINT` 之后重建，而不是试图修改。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b1f7c0a25e38"
down_revision: str | None = "6d1460fcd7b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 旧定义（任务 5 定下的四个状态）。回滚时要用它。
_OLD = "status IN ('new', 'confirmed', 'fixed', 'false_positive')"

# 新定义。**手写而不是从 vocab 动态取** —— 迁移是历史快照，
# 它必须永远表达"那一刻的事实"。从 `values(FindingStatus)` 取的话，
# 将来再加状态时，这条已经跑过的迁移会跟着改变含义（而且是在
# 别人的数据库上不会再跑一次的那种改变）。
_NEW = "status IN ('new', 'confirmed', 'fixed', 'retesting', 'closed', 'false_positive')"


def upgrade() -> None:
    # ⚠️ 传**裸名** `status_valid`，不是 `ck_findings_status_valid`。
    #
    # 项目的 `NAMING_CONVENTION`（`app/core/db.py`）是
    # `ck_%(table_name)s_%(constraint_name)s` —— 它会给传进来的名字
    # **再包一层前缀**。传全名会得到 `ck_findings_ck_findings_status_valid`，
    # 报 "constraint ... does not exist"，而错误消息里的那个双前缀
    # 正是线索。项目在任务 8 的审计表上踩过一次同一个坑。
    op.drop_constraint("status_valid", "findings", type_="check")
    op.create_check_constraint("status_valid", "findings", _NEW)


def downgrade() -> None:
    op.drop_constraint("status_valid", "findings", type_="check")
    # ⚠️ 回滚时如果库里已经有 `retesting` / `closed` 的行，这里会失败 ——
    # 那是对的：那些值在新约束下是非法的，静默把它们改成别的才是错的。
    op.create_check_constraint("status_valid", "findings", _OLD)
