"""ctf2 module：账号 / 赛事 / 题目 / 环境 / 提交

Revision ID: e4a7b2c9d135
Revises: d8e3a1f60c47
Create Date: 2026-09-28 10:10:00.000000+00:00

## ★ 为什么这条迁移的 CHECK 全是手写的

任务清单里点名要求过（"CHECK 约束必须手写，不依赖 --autogenerate"），
而这条规矩的**代价本项目已经真实付过一次**：任务 10 P0 把
`FindingStatus` 从 4 个值扩到 6 个时，改了 vocab、改了前端、改了报告标签，
**唯独漏了数据库上那条 CHECK** —— 于是把漏洞标成「已关闭」直接撞约束：

    CheckViolation: new row for relation "findings"
    violates check constraint "ck_findings_status_valid"

（事故复盘见迁移 `b1f7c0a25e38`。）

根因是 autogenerate 对 CHECK 约束**只比对"有没有这条约束"，
不比对里面的表达式**。所以本项目的规矩是：枚举类约束一律手写、
且显式 drop + create 而不是"试着改一下"。

## 约束名传**裸名**

⚠️ 项目的 `NAMING_CONVENTION`（`app/core/db.py`）是
`ck_%(table_name)s_%(constraint_name)s` —— 它会给传进来的名字
**再包一层前缀**。所以 `create_check_constraint` 一律传 `status_valid`
而不是全名。

但 `sa.CheckConstraint(...)` 写在 `create_table` 里时要传
`op.f("ck_<表>_<名>")`（**完整名**）—— 因为那一层不走 alembic 的
命名模板，而是直接用 SQLAlchemy 的 metadata 约定。这两处的写法
**不一样**，是任务 8 在审计表上踩过的坑。

## 建表顺序

有外键依赖，必须父表在前：
`ctf2_accounts` → `ctf2_competitions` → `ctf2_challenges`
→ `ctf2_environments` / `ctf2_submissions`。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# 迁移版本标识，由 alembic 分配，不要手改
revision: str = "e4a7b2c9d135"
down_revision: str | None = "d8e3a1f60c47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 可移植 JSONB（与 `app/core/columns.py` 的 `JsonColumn` 同形）。
# ⚠️ 手写而不是 import `JsonColumn`：迁移是**历史快照**，它必须永远
# 表达"那一刻的事实"。从 app 里取的话，将来 `JsonColumn` 的实现一变，
# 这条已经跑过的迁移在别人的库上就再也不会按原样执行了。
_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    # ── ctf2_accounts ─────────────────────────────────────
    op.create_table(
        "ctf2_accounts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("token_encrypted", sa.Text(), nullable=True),
        sa.Column("token_hint", sa.String(length=40), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("remote_user_id", sa.String(length=64), nullable=True),
        sa.Column("remote_username", sa.String(length=120), nullable=True),
        sa.Column("scopes", _JSON, nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'expired', 'revoked')",
            name=op.f("ck_ctf2_accounts_status_valid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ctf2_accounts")),
    )
    op.create_index(
        "uq_ctf2_accounts_active",
        "ctf2_accounts",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )

    # ── ctf2_competitions ─────────────────────────────────
    op.create_table(
        "ctf2_competitions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("account_id", sa.String(length=64), nullable=False),
        sa.Column("remote_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("remote_status", sa.String(length=64), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("challenge_count", sa.Integer(), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw", _JSON, nullable=True),
        sa.CheckConstraint(
            "kind IN ('competition', 'practice')",
            name=op.f("ck_ctf2_competitions_kind_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["ctf2_accounts.id"],
            name=op.f("fk_ctf2_competitions_account_id_ctf2_accounts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ctf2_competitions")),
        sa.UniqueConstraint(
            "account_id", "remote_id", name="uq_ctf2_competitions_account_remote"
        ),
    )
    op.create_index(
        "ix_ctf2_competitions_account_kind", "ctf2_competitions", ["account_id", "kind"]
    )

    # ── ctf2_challenges ───────────────────────────────────
    op.create_table(
        "ctf2_challenges",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("competition_id", sa.String(length=64), nullable=False),
        sa.Column("remote_id", sa.String(length=64), nullable=False),
        sa.Column("stage_id", sa.String(length=64), nullable=True),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("attachments", _JSON, nullable=True),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("sub_flag_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("flag", sa.Text(), nullable=True),
        sa.Column("wrong_submission_count", sa.Integer(), nullable=False),
        sa.Column("max_wrong_submissions", sa.Integer(), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("sandbox_profile", sa.String(length=32), nullable=False),
        sa.Column("scan_id", sa.String(length=64), nullable=True),
        sa.Column("solved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        # ★ 7 个取值手写。**不 import `values(KernelChallengeStatus)`** ——
        # 迁移是历史快照，从 vocab 取的话，将来加一个状态时，
        # 这条已经跑过的迁移会跟着改变含义（而且是在别人的库上
        # 不会再跑一次的那种改变）。
        sa.CheckConstraint(
            "status IN ('unsolved', 'environment_ready', 'solving', "
            "'candidate_ready', 'submitting', 'solved', 'failed')",
            name=op.f("ck_ctf2_challenges_status_valid"),
        ),
        sa.CheckConstraint(
            "sandbox_profile IN ('ctf2-sandbox', 'ctf2-sandbox-re')",
            name=op.f("ck_ctf2_challenges_sandbox_profile_valid"),
        ),
        sa.CheckConstraint(
            "wrong_submission_count >= 0",
            name=op.f("ck_ctf2_challenges_wrong_count_non_negative"),
        ),
        sa.CheckConstraint(
            "max_wrong_submissions >= 0",
            name=op.f("ck_ctf2_challenges_max_wrong_non_negative"),
        ),
        # 单向蕴含：解出 ⇒ 有 flag。反向不成立（见 models 的说明：
        # 反向会让"手工清空 flag"这个清理动作被拒绝）。
        sa.CheckConstraint(
            "status <> 'solved' OR flag IS NOT NULL",
            name=op.f("ck_ctf2_challenges_solved_implies_flag"),
        ),
        sa.ForeignKeyConstraint(
            ["competition_id"],
            ["ctf2_competitions.id"],
            name=op.f("fk_ctf2_challenges_competition_id_ctf2_competitions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ctf2_challenges")),
        sa.UniqueConstraint(
            "competition_id", "remote_id", name="uq_ctf2_challenges_competition_remote"
        ),
    )
    op.create_index(
        "ix_ctf2_challenges_competition_status",
        "ctf2_challenges",
        ["competition_id", "status"],
    )
    op.create_index("ix_ctf2_challenges_stage", "ctf2_challenges", ["stage_id"])

    # ── ctf2_environments ─────────────────────────────────
    op.create_table(
        "ctf2_environments",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("challenge_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("sandbox_profile", sa.String(length=32), nullable=False),
        sa.Column("access_url", sa.Text(), nullable=True),
        sa.Column("access_urls", _JSON, nullable=True),
        sa.Column("container_id", sa.String(length=128), nullable=True),
        sa.Column("poll_count", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('stopped', 'starting', 'ready', 'error', 'expired')",
            name=op.f("ck_ctf2_environments_status_valid"),
        ),
        sa.CheckConstraint(
            "sandbox_profile IN ('ctf2-sandbox', 'ctf2-sandbox-re')",
            name=op.f("ck_ctf2_environments_sandbox_profile_valid"),
        ),
        # ★ 只有 ready 才允许有 access_url。挡的是"轮询超时后留下一个
        # 可点但连不上的链接"—— 用户会当成"你们的链接坏了"来报障。
        sa.CheckConstraint(
            "status = 'ready' OR access_url IS NULL",
            name=op.f("ck_ctf2_environments_access_url_only_when_ready"),
        ),
        sa.CheckConstraint(
            "poll_count >= 0", name=op.f("ck_ctf2_environments_poll_count_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["challenge_id"],
            ["ctf2_challenges.id"],
            name=op.f("fk_ctf2_environments_challenge_id_ctf2_challenges"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ctf2_environments")),
    )
    op.create_index(
        "ix_ctf2_environments_challenge_status",
        "ctf2_environments",
        ["challenge_id", "status"],
    )

    # ── ctf2_submissions ──────────────────────────────────
    op.create_table(
        "ctf2_submissions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("challenge_id", sa.String(length=64), nullable=False),
        sa.Column("flag", sa.Text(), nullable=False),
        sa.Column("sub_flag_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("remote_submission_id", sa.String(length=128), nullable=True),
        sa.Column("remote_message", sa.Text(), nullable=True),
        sa.Column("local_error", sa.Text(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'correct', 'wrong', 'rejected', 'failed')",
            name=op.f("ck_ctf2_submissions_status_valid"),
        ),
        sa.CheckConstraint(
            "mode IN ('safe', 'auto')",
            name=op.f("ck_ctf2_submissions_mode_valid"),
        ),
        # 判对 ⇒ 有平台回执。挡住的是"把请求成功当成判题成功"——
        # 那会让系统凭空宣称解出一道题，而下一次同步又变回未解出。
        sa.CheckConstraint(
            "status <> 'correct' OR remote_submission_id IS NOT NULL",
            name=op.f("ck_ctf2_submissions_correct_implies_receipt"),
        ),
        sa.ForeignKeyConstraint(
            ["challenge_id"],
            ["ctf2_challenges.id"],
            name=op.f("fk_ctf2_submissions_challenge_id_ctf2_challenges"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ctf2_submissions")),
    )
    op.create_index(
        "ix_ctf2_submissions_challenge_at",
        "ctf2_submissions",
        ["challenge_id", "submitted_at"],
    )
    op.create_index("ix_ctf2_submissions_status", "ctf2_submissions", ["status"])


def downgrade() -> None:
    # 顺序与 upgrade **严格相反**：先删有外键的子表，再删父表。
    # 反了的话 PostgreSQL 会报 "cannot drop table ... because other
    # objects depend on it"，而那句话不会告诉你正确的顺序。
    op.drop_index("ix_ctf2_submissions_status", table_name="ctf2_submissions")
    op.drop_index("ix_ctf2_submissions_challenge_at", table_name="ctf2_submissions")
    op.drop_table("ctf2_submissions")

    op.drop_index("ix_ctf2_environments_challenge_status", table_name="ctf2_environments")
    op.drop_table("ctf2_environments")

    op.drop_index("ix_ctf2_challenges_stage", table_name="ctf2_challenges")
    op.drop_index("ix_ctf2_challenges_competition_status", table_name="ctf2_challenges")
    op.drop_table("ctf2_challenges")

    op.drop_index("ix_ctf2_competitions_account_kind", table_name="ctf2_competitions")
    op.drop_table("ctf2_competitions")

    op.drop_index("uq_ctf2_accounts_active", table_name="ctf2_accounts")
    op.drop_table("ctf2_accounts")
