"""reports scope accepts findings

Revision ID: c8a41d6e9307
Revises: b1f7c0a25e38

## 为什么需要这条迁移（v0.16）

任务 10 P5 给报告加了一种范围：`kind="findings"` —— 用户手选的那几条
漏洞（打通"漏洞发现"页的「生成报告」）。

`reports` 表上有**两条** CHECK 约束管着 scope，两条都要放开：

1. `ck_reports_scope_kind_valid` —— 允许的 kind 枚举，原来只有三个。
2. `ck_reports_scope_exactly_one_shape` —— "恰好一种形状"，
   原来有三个分支。

关于第二条：`findings` 这种范围的四个 scope 列**全为 NULL** ——
成员是一串 id，塞不进任何一列，它们存在 `report_findings` 关联表里。

⚠️ 与上一条迁移同样的两个坑，这里都踩过：

* `--autogenerate` **检测不到 CHECK 约束内容的**变化（它只比对
  "有没有这条约束"），所以这条是手写的。
* `drop_constraint` 要传**裸名**（`scope_kind_valid`），不是全名 ——
  项目的 `NAMING_CONVENTION` 会自动加 `ck_<表名>_` 前缀，
  传全名会得到 `ck_reports_ck_reports_scope_kind_valid`。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "c8a41d6e9307"
down_revision: str | None = "b1f7c0a25e38"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS_OLD = "scope_kind IN ('scan', 'target', 'range')"
_KINDS_NEW = "scope_kind IN ('scan', 'target', 'range', 'findings')"

_SHAPE_OLD = """
(scope_kind = 'scan'
    AND scope_scan_id IS NOT NULL AND scope_target IS NULL
    AND scope_from IS NULL AND scope_to IS NULL)
OR (scope_kind = 'target'
    AND scope_target IS NOT NULL AND scope_scan_id IS NULL
    AND scope_from IS NULL AND scope_to IS NULL)
OR (scope_kind = 'range'
    AND scope_from IS NOT NULL AND scope_to IS NOT NULL
    AND scope_scan_id IS NULL AND scope_target IS NULL)
"""

_SHAPE_NEW = (
    _SHAPE_OLD
    + """
OR (scope_kind = 'findings'
    AND scope_scan_id IS NULL AND scope_target IS NULL
    AND scope_from IS NULL AND scope_to IS NULL)
"""
)


def upgrade() -> None:
    op.drop_constraint("scope_kind_valid", "reports", type_="check")
    op.create_check_constraint("scope_kind_valid", "reports", _KINDS_NEW)

    op.drop_constraint("scope_exactly_one_shape", "reports", type_="check")
    op.create_check_constraint("scope_exactly_one_shape", "reports", _SHAPE_NEW)


def downgrade() -> None:
    op.drop_constraint("scope_exactly_one_shape", "reports", type_="check")
    op.create_check_constraint("scope_exactly_one_shape", "reports", _SHAPE_OLD)

    op.drop_constraint("scope_kind_valid", "reports", type_="check")
    # ⚠️ 回滚时若库里已有 findings 范围的报告，这里会失败 —— 那是对的：
    # 那些行在新约束下非法，静默把它们改成别的范围才是错的。
    op.create_check_constraint("scope_kind_valid", "reports", _KINDS_OLD)
