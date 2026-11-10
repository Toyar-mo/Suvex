"""`purple_rules` -- 紫队检测规则（v1.82）。

默认规则只覆盖最常见的六类 CWE。它们不是"全部检测能力"，
而是"能被这批 finding 直接评估"的最小起点；规则本身可以在界面里增删。

`down_revision` = 7c2f9b1e5a46（benchmark_cases）。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "5d1a8c3f2e70"
down_revision = "7c2f9b1e5a46"
branch_labels = None
depends_on = None

_DEFAULTS = [
    ("CWE-89 SQL 注入", "CWE-89", "high"),
    ("CWE-79 跨站脚本", "CWE-79", "medium"),
    ("CWE-918 SSRF", "CWE-918", "high"),
    ("CWE-78 命令注入", "CWE-78", "critical"),
    ("CWE-22 路径遍历", "CWE-22", "high"),
    ("CWE-502 反序列化", "CWE-502", "critical"),
]


def upgrade() -> None:
    table = op.create_table(
        "purple_rules",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False, server_default="medium"),
        sa.Column("cwe", sa.String(length=32), nullable=True),
        sa.Column("title_contains", sa.String(length=200), nullable=True),
        sa.Column("target_contains", sa.String(length=512), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_purple_rules_enabled", "purple_rules", ["enabled"])

    op.bulk_insert(
        table,
        [
            {
                "id": f"rule_default_{index}",
                "name": name,
                "severity": severity,
                "cwe": cwe,
                "title_contains": None,
                "target_contains": None,
                "enabled": True,
            }
            for index, (name, cwe, severity) in enumerate(_DEFAULTS, start=1)
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_purple_rules_enabled", table_name="purple_rules")
    op.drop_table("purple_rules")
