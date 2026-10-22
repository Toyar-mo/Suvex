"""内核表名 ctf2_* → suvex_*（v0.54，改名第 3 步/6）

Revision ID: b8f3a1c7d2e4
Revises: e7b2c48d1a93
Create Date: 2026-10-22 10:30

## 这条迁移解决什么

上层已把包目录 `app/ctf2/` → `app/kernel/`（第 1 步）、Python 类 `Ctf2*` →
`Kernel*`（第 2 步）。本条把**数据库里的 12 张表**从 `ctf2_*` 改名为 `suvex_*`，
连同它们的主键 / 唯一约束 / CHECK / 外键 / 索引名一起改 —— 这些名字里都嵌着
`ctf2_`，只改 `__tablename__` 而不改约束名，会让 `naming_convention` 生成的新名字
和库里的旧名字对不上，下次 `alembic --autogenerate` 会看到一堆"凭空多出来的差异"。

## 为什么是纯 RENAME 而不是 drop+create

**数据一条不能丢。** 这 12 张表里住着账号、赛事、题目、环境、提交、方法卡、
题解、token 用量、遥测 —— 都是不可重建的历史（提交记录是证据链，方法卡是
跨题召回的唯一信号源）。`ALTER TABLE ... RENAME` 只改 catalog 里的名字，
不重写任何行，也不动数据。

## 顺序为什么是这个顺序

- **表先改**：FK 在 PostgreSQL 里按 OID 绑定到目标表，改被引用表的**名字**
  不会断 FK（OID 不变）。所以 12 张表可以一次改完，不必按父子顺序拆开。
- **约束其次**：`ALTER TABLE <newtable> RENAME CONSTRAINT old TO new`。
  PK / UNIQUE / CHECK / FK 都是表约束，都走这条。改表名后约束仍挂在原表（OID 同上）。
- **独立索引最后**：`ix_*` 是生成式索引、不是约束的底层索引，用
  `ALTER INDEX old RENAME TO new`。

## 为什么不 --autogenerate

`--autogenerate` **不比对约束名/索引名**，只看列的增删改。rename 它看不见，
生成出来是空的。这条必须手写。

## ⚠️ 表的物理名 vs 迁移历史

历史迁移（`e4a7b2c9d135` 等）里写的仍是 `ctf2_*` —— **那是对的，不能改**：
迁移是对"当时那个库"的描述，改写已应用的迁移会让 `alembic upgrade head`
在一个新库上重建出错误的名字。名字演化全部收敛到这一条里。
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "b8f3a1c7d2e4"
down_revision: Union[str, None] = "e7b2c48d1a93"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ① 表重命名（12 张）。顺序按依赖：被引用方先改，FK 目标名在库内按 OID 绑定，改表名不破 FK。
    op.execute("ALTER TABLE ctf2_accounts RENAME TO suvex_accounts")
    op.execute("ALTER TABLE ctf2_platforms RENAME TO suvex_platforms")
    op.execute("ALTER TABLE ctf2_competitions RENAME TO suvex_competitions")
    op.execute("ALTER TABLE ctf2_challenges RENAME TO suvex_challenges")
    op.execute("ALTER TABLE ctf2_environments RENAME TO suvex_environments")
    op.execute("ALTER TABLE ctf2_solve_attempts RENAME TO suvex_solve_attempts")
    op.execute("ALTER TABLE ctf2_submissions RENAME TO suvex_submissions")
    op.execute("ALTER TABLE ctf2_solve_notes RENAME TO suvex_solve_notes")
    op.execute("ALTER TABLE ctf2_solve_usages RENAME TO suvex_solve_usages")
    op.execute("ALTER TABLE ctf2_writeups RENAME TO suvex_writeups")
    op.execute("ALTER TABLE ctf2_solve_telemetry RENAME TO suvex_solve_telemetry")
    op.execute("ALTER TABLE ctf2_config RENAME TO suvex_config")
    # ② 约束重命名（PK / UNIQUE / CHECK / FK）—— ALTER TABLE ... RENAME CONSTRAINT 对非表约束会报错，故分类型处理
    op.execute("ALTER TABLE suvex_accounts RENAME CONSTRAINT ck_ctf2_accounts_status_valid TO ck_suvex_accounts_status_valid")
    op.execute("ALTER TABLE suvex_accounts RENAME CONSTRAINT pk_ctf2_accounts TO pk_suvex_accounts")
    op.execute("ALTER TABLE suvex_challenges RENAME CONSTRAINT ck_ctf2_challenges_candidate_never_with_confirmed TO ck_suvex_challenges_candidate_never_with_confirmed")
    op.execute("ALTER TABLE suvex_challenges RENAME CONSTRAINT ck_ctf2_challenges_confirmed_only_when_solved TO ck_suvex_challenges_confirmed_only_when_solved")
    op.execute("ALTER TABLE suvex_challenges RENAME CONSTRAINT ck_ctf2_challenges_max_wrong_non_negative TO ck_suvex_challenges_max_wrong_non_negative")
    op.execute("ALTER TABLE suvex_challenges RENAME CONSTRAINT ck_ctf2_challenges_sandbox_profile_valid TO ck_suvex_challenges_sandbox_profile_valid")
    op.execute("ALTER TABLE suvex_challenges RENAME CONSTRAINT ck_ctf2_challenges_solved_implies_flag TO ck_suvex_challenges_solved_implies_flag")
    op.execute("ALTER TABLE suvex_challenges RENAME CONSTRAINT ck_ctf2_challenges_status_valid TO ck_suvex_challenges_status_valid")
    op.execute("ALTER TABLE suvex_challenges RENAME CONSTRAINT ck_ctf2_challenges_wrong_count_non_negative TO ck_suvex_challenges_wrong_count_non_negative")
    op.execute("ALTER TABLE suvex_challenges RENAME CONSTRAINT fk_ctf2_challenges_competition_id_ctf2_competitions TO fk_suvex_challenges_competition_id_suvex_competitions")
    op.execute("ALTER TABLE suvex_challenges RENAME CONSTRAINT pk_ctf2_challenges TO pk_suvex_challenges")
    op.execute("ALTER TABLE suvex_challenges RENAME CONSTRAINT uq_ctf2_challenges_competition_remote TO uq_suvex_challenges_competition_remote")
    op.execute("ALTER TABLE suvex_competitions RENAME CONSTRAINT ck_ctf2_competitions_kind_valid TO ck_suvex_competitions_kind_valid")
    op.execute("ALTER TABLE suvex_competitions RENAME CONSTRAINT fk_ctf2_competitions_account_id_ctf2_accounts TO fk_suvex_competitions_account_id_suvex_accounts")
    op.execute("ALTER TABLE suvex_competitions RENAME CONSTRAINT pk_ctf2_competitions TO pk_suvex_competitions")
    op.execute("ALTER TABLE suvex_competitions RENAME CONSTRAINT uq_ctf2_competitions_account_remote TO uq_suvex_competitions_account_remote")
    op.execute("ALTER TABLE suvex_config RENAME CONSTRAINT ck_ctf2_config_auto_sync_seconds_within_range TO ck_suvex_config_auto_sync_seconds_within_range")
    op.execute("ALTER TABLE suvex_config RENAME CONSTRAINT ck_ctf2_config_default_sandbox_profile_valid TO ck_suvex_config_default_sandbox_profile_valid")
    op.execute("ALTER TABLE suvex_config RENAME CONSTRAINT ck_ctf2_config_max_turns_within_range TO ck_suvex_config_max_turns_within_range")
    op.execute("ALTER TABLE suvex_config RENAME CONSTRAINT ck_ctf2_config_max_wrong_submissions_within_ceiling TO ck_suvex_config_max_wrong_submissions_within_ceiling")
    op.execute("ALTER TABLE suvex_config RENAME CONSTRAINT ck_ctf2_config_solve_poll_seconds_within_range TO ck_suvex_config_solve_poll_seconds_within_range")
    op.execute("ALTER TABLE suvex_config RENAME CONSTRAINT ck_ctf2_config_submit_mode_valid TO ck_suvex_config_submit_mode_valid")
    op.execute("ALTER TABLE suvex_config RENAME CONSTRAINT pk_ctf2_config TO pk_suvex_config")
    op.execute("ALTER TABLE suvex_environments RENAME CONSTRAINT ck_ctf2_environments_access_url_only_when_ready TO ck_suvex_environments_access_url_only_when_ready")
    op.execute("ALTER TABLE suvex_environments RENAME CONSTRAINT ck_ctf2_environments_poll_count_non_negative TO ck_suvex_environments_poll_count_non_negative")
    op.execute("ALTER TABLE suvex_environments RENAME CONSTRAINT ck_ctf2_environments_sandbox_profile_valid TO ck_suvex_environments_sandbox_profile_valid")
    op.execute("ALTER TABLE suvex_environments RENAME CONSTRAINT ck_ctf2_environments_status_valid TO ck_suvex_environments_status_valid")
    op.execute("ALTER TABLE suvex_environments RENAME CONSTRAINT fk_ctf2_environments_challenge_id_ctf2_challenges TO fk_suvex_environments_challenge_id_suvex_challenges")
    op.execute("ALTER TABLE suvex_environments RENAME CONSTRAINT pk_ctf2_environments TO pk_suvex_environments")
    op.execute("ALTER TABLE suvex_platforms RENAME CONSTRAINT ck_ctf2_platforms_driver_valid TO ck_suvex_platforms_driver_valid")
    op.execute("ALTER TABLE suvex_platforms RENAME CONSTRAINT ck_ctf2_platforms_source_valid TO ck_suvex_platforms_source_valid")
    op.execute("ALTER TABLE suvex_platforms RENAME CONSTRAINT ck_ctf2_platforms_status_valid TO ck_suvex_platforms_status_valid")
    op.execute("ALTER TABLE suvex_platforms RENAME CONSTRAINT pk_ctf2_platforms TO pk_suvex_platforms")
    op.execute("ALTER TABLE suvex_platforms RENAME CONSTRAINT uq_ctf2_platforms_slug TO uq_suvex_platforms_slug")
    op.execute("ALTER TABLE suvex_solve_attempts RENAME CONSTRAINT ck_ctf2_solve_attempts_solve_attempt_mode_valid TO ck_suvex_solve_attempts_solve_attempt_mode_valid")
    op.execute("ALTER TABLE suvex_solve_attempts RENAME CONSTRAINT ck_ctf2_solve_attempts_solve_attempt_profile_valid TO ck_suvex_solve_attempts_solve_attempt_profile_valid")
    op.execute("ALTER TABLE suvex_solve_attempts RENAME CONSTRAINT ck_ctf2_solve_attempts_solve_attempt_turns_positive TO ck_suvex_solve_attempts_solve_attempt_turns_positive")
    op.execute("ALTER TABLE suvex_solve_attempts RENAME CONSTRAINT fk_ctf2_solve_attempts_challenge_id_ctf2_challenges TO fk_suvex_solve_attempts_challenge_id_suvex_challenges")
    op.execute("ALTER TABLE suvex_solve_attempts RENAME CONSTRAINT pk_ctf2_solve_attempts TO pk_suvex_solve_attempts")
    op.execute("ALTER TABLE suvex_solve_notes RENAME CONSTRAINT ck_ctf2_solve_notes_verdict_valid TO ck_suvex_solve_notes_verdict_valid")
    op.execute("ALTER TABLE suvex_solve_notes RENAME CONSTRAINT fk_ctf2_solve_notes_challenge_id_ctf2_challenges TO fk_suvex_solve_notes_challenge_id_suvex_challenges")
    op.execute("ALTER TABLE suvex_solve_notes RENAME CONSTRAINT pk_ctf2_solve_notes TO pk_suvex_solve_notes")
    op.execute("ALTER TABLE suvex_solve_notes RENAME CONSTRAINT uq_ctf2_solve_notes_challenge TO uq_suvex_solve_notes_challenge")
    op.execute("ALTER TABLE suvex_solve_telemetry RENAME CONSTRAINT ck_ctf2_solve_telemetry_solve_telemetry_non_negative TO ck_suvex_solve_telemetry_solve_telemetry_non_negative")
    op.execute("ALTER TABLE suvex_solve_telemetry RENAME CONSTRAINT fk_ctf2_solve_telemetry_attempt_id_ctf2_solve_attempts TO fk_suvex_solve_telemetry_attempt_id_suvex_solve_attempts")
    op.execute("ALTER TABLE suvex_solve_telemetry RENAME CONSTRAINT fk_ctf2_solve_telemetry_challenge_id_ctf2_challenges TO fk_suvex_solve_telemetry_challenge_id_suvex_challenges")
    op.execute("ALTER TABLE suvex_solve_telemetry RENAME CONSTRAINT pk_ctf2_solve_telemetry TO pk_suvex_solve_telemetry")
    op.execute("ALTER TABLE suvex_solve_usages RENAME CONSTRAINT ck_ctf2_solve_usages_solve_usage_tokens_non_negative TO ck_suvex_solve_usages_solve_usage_tokens_non_negative")
    op.execute("ALTER TABLE suvex_solve_usages RENAME CONSTRAINT ck_ctf2_solve_usages_solve_usage_turn_positive TO ck_suvex_solve_usages_solve_usage_turn_positive")
    op.execute("ALTER TABLE suvex_solve_usages RENAME CONSTRAINT fk_ctf2_solve_usages_attempt_id_ctf2_solve_attempts TO fk_suvex_solve_usages_attempt_id_suvex_solve_attempts")
    op.execute("ALTER TABLE suvex_solve_usages RENAME CONSTRAINT fk_ctf2_solve_usages_challenge_id_ctf2_challenges TO fk_suvex_solve_usages_challenge_id_suvex_challenges")
    op.execute("ALTER TABLE suvex_solve_usages RENAME CONSTRAINT pk_ctf2_solve_usages TO pk_suvex_solve_usages")
    op.execute("ALTER TABLE suvex_submissions RENAME CONSTRAINT ck_ctf2_submissions_correct_implies_receipt TO ck_suvex_submissions_correct_implies_receipt")
    op.execute("ALTER TABLE suvex_submissions RENAME CONSTRAINT ck_ctf2_submissions_mode_valid TO ck_suvex_submissions_mode_valid")
    op.execute("ALTER TABLE suvex_submissions RENAME CONSTRAINT ck_ctf2_submissions_status_valid TO ck_suvex_submissions_status_valid")
    op.execute("ALTER TABLE suvex_submissions RENAME CONSTRAINT fk_ctf2_submissions_challenge_id_ctf2_challenges TO fk_suvex_submissions_challenge_id_suvex_challenges")
    op.execute("ALTER TABLE suvex_submissions RENAME CONSTRAINT pk_ctf2_submissions TO pk_suvex_submissions")
    op.execute("ALTER TABLE suvex_writeups RENAME CONSTRAINT fk_ctf2_writeups_attempt_id_ctf2_solve_attempts TO fk_suvex_writeups_attempt_id_suvex_solve_attempts")
    op.execute("ALTER TABLE suvex_writeups RENAME CONSTRAINT fk_ctf2_writeups_challenge_id_ctf2_challenges TO fk_suvex_writeups_challenge_id_suvex_challenges")
    op.execute("ALTER TABLE suvex_writeups RENAME CONSTRAINT pk_ctf2_writeups TO pk_suvex_writeups")
    op.execute("ALTER TABLE suvex_writeups RENAME CONSTRAINT uq_ctf2_writeups_challenge TO uq_suvex_writeups_challenge")
    # ③ 独立索引重命名（ix_...，非约束支撑）
    op.execute("ALTER INDEX ix_ctf2_challenges_competition_status RENAME TO ix_suvex_challenges_competition_status")
    op.execute("ALTER INDEX ix_ctf2_challenges_stage RENAME TO ix_suvex_challenges_stage")
    op.execute("ALTER INDEX ix_ctf2_competitions_account_kind RENAME TO ix_suvex_competitions_account_kind")
    op.execute("ALTER INDEX ix_ctf2_environments_challenge_status RENAME TO ix_suvex_environments_challenge_status")
    op.execute("ALTER INDEX ix_ctf2_solve_attempts_challenge_at RENAME TO ix_suvex_solve_attempts_challenge_at")
    op.execute("ALTER INDEX ix_ctf2_solve_notes_category RENAME TO ix_suvex_solve_notes_category")
    op.execute("ALTER INDEX ix_ctf2_solve_notes_solved RENAME TO ix_suvex_solve_notes_solved")
    op.execute("ALTER INDEX ix_ctf2_solve_telemetry_category_at RENAME TO ix_suvex_solve_telemetry_category_at")
    op.execute("ALTER INDEX ix_ctf2_solve_telemetry_challenge_at RENAME TO ix_suvex_solve_telemetry_challenge_at")
    op.execute("ALTER INDEX ix_ctf2_solve_telemetry_subcategory_at RENAME TO ix_suvex_solve_telemetry_subcategory_at")
    op.execute("ALTER INDEX ix_ctf2_solve_usages_attempt_turn RENAME TO ix_suvex_solve_usages_attempt_turn")
    op.execute("ALTER INDEX ix_ctf2_solve_usages_challenge_at RENAME TO ix_suvex_solve_usages_challenge_at")
    op.execute("ALTER INDEX ix_ctf2_submissions_challenge_at RENAME TO ix_suvex_submissions_challenge_at")
    op.execute("ALTER INDEX ix_ctf2_submissions_status RENAME TO ix_suvex_submissions_status")
    op.execute("ALTER INDEX ix_ctf2_writeups_generated_at RENAME TO ix_suvex_writeups_generated_at")
    op.execute("ALTER INDEX uq_ctf2_accounts_active RENAME TO uq_suvex_accounts_active")


def downgrade() -> None:
    op.execute("ALTER INDEX uq_suvex_accounts_active RENAME TO uq_ctf2_accounts_active")
    op.execute("ALTER INDEX ix_suvex_writeups_generated_at RENAME TO ix_ctf2_writeups_generated_at")
    op.execute("ALTER INDEX ix_suvex_submissions_status RENAME TO ix_ctf2_submissions_status")
    op.execute("ALTER INDEX ix_suvex_submissions_challenge_at RENAME TO ix_ctf2_submissions_challenge_at")
    op.execute("ALTER INDEX ix_suvex_solve_usages_challenge_at RENAME TO ix_ctf2_solve_usages_challenge_at")
    op.execute("ALTER INDEX ix_suvex_solve_usages_attempt_turn RENAME TO ix_ctf2_solve_usages_attempt_turn")
    op.execute("ALTER INDEX ix_suvex_solve_telemetry_subcategory_at RENAME TO ix_ctf2_solve_telemetry_subcategory_at")
    op.execute("ALTER INDEX ix_suvex_solve_telemetry_challenge_at RENAME TO ix_ctf2_solve_telemetry_challenge_at")
    op.execute("ALTER INDEX ix_suvex_solve_telemetry_category_at RENAME TO ix_ctf2_solve_telemetry_category_at")
    op.execute("ALTER INDEX ix_suvex_solve_notes_solved RENAME TO ix_ctf2_solve_notes_solved")
    op.execute("ALTER INDEX ix_suvex_solve_notes_category RENAME TO ix_ctf2_solve_notes_category")
    op.execute("ALTER INDEX ix_suvex_solve_attempts_challenge_at RENAME TO ix_ctf2_solve_attempts_challenge_at")
    op.execute("ALTER INDEX ix_suvex_environments_challenge_status RENAME TO ix_ctf2_environments_challenge_status")
    op.execute("ALTER INDEX ix_suvex_competitions_account_kind RENAME TO ix_ctf2_competitions_account_kind")
    op.execute("ALTER INDEX ix_suvex_challenges_stage RENAME TO ix_ctf2_challenges_stage")
    op.execute("ALTER INDEX ix_suvex_challenges_competition_status RENAME TO ix_ctf2_challenges_competition_status")
    op.execute("ALTER TABLE suvex_writeups RENAME CONSTRAINT uq_suvex_writeups_challenge TO uq_ctf2_writeups_challenge")
    op.execute("ALTER TABLE suvex_writeups RENAME CONSTRAINT pk_suvex_writeups TO pk_ctf2_writeups")
    op.execute("ALTER TABLE suvex_writeups RENAME CONSTRAINT fk_suvex_writeups_challenge_id_suvex_challenges TO fk_ctf2_writeups_challenge_id_ctf2_challenges")
    op.execute("ALTER TABLE suvex_writeups RENAME CONSTRAINT fk_suvex_writeups_attempt_id_suvex_solve_attempts TO fk_ctf2_writeups_attempt_id_ctf2_solve_attempts")
    op.execute("ALTER TABLE suvex_submissions RENAME CONSTRAINT pk_suvex_submissions TO pk_ctf2_submissions")
    op.execute("ALTER TABLE suvex_submissions RENAME CONSTRAINT fk_suvex_submissions_challenge_id_suvex_challenges TO fk_ctf2_submissions_challenge_id_ctf2_challenges")
    op.execute("ALTER TABLE suvex_submissions RENAME CONSTRAINT ck_suvex_submissions_status_valid TO ck_ctf2_submissions_status_valid")
    op.execute("ALTER TABLE suvex_submissions RENAME CONSTRAINT ck_suvex_submissions_mode_valid TO ck_ctf2_submissions_mode_valid")
    op.execute("ALTER TABLE suvex_submissions RENAME CONSTRAINT ck_suvex_submissions_correct_implies_receipt TO ck_ctf2_submissions_correct_implies_receipt")
    op.execute("ALTER TABLE suvex_solve_usages RENAME CONSTRAINT pk_suvex_solve_usages TO pk_ctf2_solve_usages")
    op.execute("ALTER TABLE suvex_solve_usages RENAME CONSTRAINT fk_suvex_solve_usages_challenge_id_suvex_challenges TO fk_ctf2_solve_usages_challenge_id_ctf2_challenges")
    op.execute("ALTER TABLE suvex_solve_usages RENAME CONSTRAINT fk_suvex_solve_usages_attempt_id_suvex_solve_attempts TO fk_ctf2_solve_usages_attempt_id_ctf2_solve_attempts")
    op.execute("ALTER TABLE suvex_solve_usages RENAME CONSTRAINT ck_suvex_solve_usages_solve_usage_turn_positive TO ck_ctf2_solve_usages_solve_usage_turn_positive")
    op.execute("ALTER TABLE suvex_solve_usages RENAME CONSTRAINT ck_suvex_solve_usages_solve_usage_tokens_non_negative TO ck_ctf2_solve_usages_solve_usage_tokens_non_negative")
    op.execute("ALTER TABLE suvex_solve_telemetry RENAME CONSTRAINT pk_suvex_solve_telemetry TO pk_ctf2_solve_telemetry")
    op.execute("ALTER TABLE suvex_solve_telemetry RENAME CONSTRAINT fk_suvex_solve_telemetry_challenge_id_suvex_challenges TO fk_ctf2_solve_telemetry_challenge_id_ctf2_challenges")
    op.execute("ALTER TABLE suvex_solve_telemetry RENAME CONSTRAINT fk_suvex_solve_telemetry_attempt_id_suvex_solve_attempts TO fk_ctf2_solve_telemetry_attempt_id_ctf2_solve_attempts")
    op.execute("ALTER TABLE suvex_solve_telemetry RENAME CONSTRAINT ck_suvex_solve_telemetry_solve_telemetry_non_negative TO ck_ctf2_solve_telemetry_solve_telemetry_non_negative")
    op.execute("ALTER TABLE suvex_solve_notes RENAME CONSTRAINT uq_suvex_solve_notes_challenge TO uq_ctf2_solve_notes_challenge")
    op.execute("ALTER TABLE suvex_solve_notes RENAME CONSTRAINT pk_suvex_solve_notes TO pk_ctf2_solve_notes")
    op.execute("ALTER TABLE suvex_solve_notes RENAME CONSTRAINT fk_suvex_solve_notes_challenge_id_suvex_challenges TO fk_ctf2_solve_notes_challenge_id_ctf2_challenges")
    op.execute("ALTER TABLE suvex_solve_notes RENAME CONSTRAINT ck_suvex_solve_notes_verdict_valid TO ck_ctf2_solve_notes_verdict_valid")
    op.execute("ALTER TABLE suvex_solve_attempts RENAME CONSTRAINT pk_suvex_solve_attempts TO pk_ctf2_solve_attempts")
    op.execute("ALTER TABLE suvex_solve_attempts RENAME CONSTRAINT fk_suvex_solve_attempts_challenge_id_suvex_challenges TO fk_ctf2_solve_attempts_challenge_id_ctf2_challenges")
    op.execute("ALTER TABLE suvex_solve_attempts RENAME CONSTRAINT ck_suvex_solve_attempts_solve_attempt_turns_positive TO ck_ctf2_solve_attempts_solve_attempt_turns_positive")
    op.execute("ALTER TABLE suvex_solve_attempts RENAME CONSTRAINT ck_suvex_solve_attempts_solve_attempt_profile_valid TO ck_ctf2_solve_attempts_solve_attempt_profile_valid")
    op.execute("ALTER TABLE suvex_solve_attempts RENAME CONSTRAINT ck_suvex_solve_attempts_solve_attempt_mode_valid TO ck_ctf2_solve_attempts_solve_attempt_mode_valid")
    op.execute("ALTER TABLE suvex_platforms RENAME CONSTRAINT uq_suvex_platforms_slug TO uq_ctf2_platforms_slug")
    op.execute("ALTER TABLE suvex_platforms RENAME CONSTRAINT pk_suvex_platforms TO pk_ctf2_platforms")
    op.execute("ALTER TABLE suvex_platforms RENAME CONSTRAINT ck_suvex_platforms_status_valid TO ck_ctf2_platforms_status_valid")
    op.execute("ALTER TABLE suvex_platforms RENAME CONSTRAINT ck_suvex_platforms_source_valid TO ck_ctf2_platforms_source_valid")
    op.execute("ALTER TABLE suvex_platforms RENAME CONSTRAINT ck_suvex_platforms_driver_valid TO ck_ctf2_platforms_driver_valid")
    op.execute("ALTER TABLE suvex_environments RENAME CONSTRAINT pk_suvex_environments TO pk_ctf2_environments")
    op.execute("ALTER TABLE suvex_environments RENAME CONSTRAINT fk_suvex_environments_challenge_id_suvex_challenges TO fk_ctf2_environments_challenge_id_ctf2_challenges")
    op.execute("ALTER TABLE suvex_environments RENAME CONSTRAINT ck_suvex_environments_status_valid TO ck_ctf2_environments_status_valid")
    op.execute("ALTER TABLE suvex_environments RENAME CONSTRAINT ck_suvex_environments_sandbox_profile_valid TO ck_ctf2_environments_sandbox_profile_valid")
    op.execute("ALTER TABLE suvex_environments RENAME CONSTRAINT ck_suvex_environments_poll_count_non_negative TO ck_ctf2_environments_poll_count_non_negative")
    op.execute("ALTER TABLE suvex_environments RENAME CONSTRAINT ck_suvex_environments_access_url_only_when_ready TO ck_ctf2_environments_access_url_only_when_ready")
    op.execute("ALTER TABLE suvex_config RENAME CONSTRAINT pk_suvex_config TO pk_ctf2_config")
    op.execute("ALTER TABLE suvex_config RENAME CONSTRAINT ck_suvex_config_submit_mode_valid TO ck_ctf2_config_submit_mode_valid")
    op.execute("ALTER TABLE suvex_config RENAME CONSTRAINT ck_suvex_config_solve_poll_seconds_within_range TO ck_ctf2_config_solve_poll_seconds_within_range")
    op.execute("ALTER TABLE suvex_config RENAME CONSTRAINT ck_suvex_config_max_wrong_submissions_within_ceiling TO ck_ctf2_config_max_wrong_submissions_within_ceiling")
    op.execute("ALTER TABLE suvex_config RENAME CONSTRAINT ck_suvex_config_max_turns_within_range TO ck_ctf2_config_max_turns_within_range")
    op.execute("ALTER TABLE suvex_config RENAME CONSTRAINT ck_suvex_config_default_sandbox_profile_valid TO ck_ctf2_config_default_sandbox_profile_valid")
    op.execute("ALTER TABLE suvex_config RENAME CONSTRAINT ck_suvex_config_auto_sync_seconds_within_range TO ck_ctf2_config_auto_sync_seconds_within_range")
    op.execute("ALTER TABLE suvex_competitions RENAME CONSTRAINT uq_suvex_competitions_account_remote TO uq_ctf2_competitions_account_remote")
    op.execute("ALTER TABLE suvex_competitions RENAME CONSTRAINT pk_suvex_competitions TO pk_ctf2_competitions")
    op.execute("ALTER TABLE suvex_competitions RENAME CONSTRAINT fk_suvex_competitions_account_id_suvex_accounts TO fk_ctf2_competitions_account_id_ctf2_accounts")
    op.execute("ALTER TABLE suvex_competitions RENAME CONSTRAINT ck_suvex_competitions_kind_valid TO ck_ctf2_competitions_kind_valid")
    op.execute("ALTER TABLE suvex_challenges RENAME CONSTRAINT uq_suvex_challenges_competition_remote TO uq_ctf2_challenges_competition_remote")
    op.execute("ALTER TABLE suvex_challenges RENAME CONSTRAINT pk_suvex_challenges TO pk_ctf2_challenges")
    op.execute("ALTER TABLE suvex_challenges RENAME CONSTRAINT fk_suvex_challenges_competition_id_suvex_competitions TO fk_ctf2_challenges_competition_id_ctf2_competitions")
    op.execute("ALTER TABLE suvex_challenges RENAME CONSTRAINT ck_suvex_challenges_wrong_count_non_negative TO ck_ctf2_challenges_wrong_count_non_negative")
    op.execute("ALTER TABLE suvex_challenges RENAME CONSTRAINT ck_suvex_challenges_status_valid TO ck_ctf2_challenges_status_valid")
    op.execute("ALTER TABLE suvex_challenges RENAME CONSTRAINT ck_suvex_challenges_solved_implies_flag TO ck_ctf2_challenges_solved_implies_flag")
    op.execute("ALTER TABLE suvex_challenges RENAME CONSTRAINT ck_suvex_challenges_sandbox_profile_valid TO ck_ctf2_challenges_sandbox_profile_valid")
    op.execute("ALTER TABLE suvex_challenges RENAME CONSTRAINT ck_suvex_challenges_max_wrong_non_negative TO ck_ctf2_challenges_max_wrong_non_negative")
    op.execute("ALTER TABLE suvex_challenges RENAME CONSTRAINT ck_suvex_challenges_confirmed_only_when_solved TO ck_ctf2_challenges_confirmed_only_when_solved")
    op.execute("ALTER TABLE suvex_challenges RENAME CONSTRAINT ck_suvex_challenges_candidate_never_with_confirmed TO ck_ctf2_challenges_candidate_never_with_confirmed")
    op.execute("ALTER TABLE suvex_accounts RENAME CONSTRAINT pk_suvex_accounts TO pk_ctf2_accounts")
    op.execute("ALTER TABLE suvex_accounts RENAME CONSTRAINT ck_suvex_accounts_status_valid TO ck_ctf2_accounts_status_valid")
    op.execute("ALTER TABLE suvex_config RENAME TO ctf2_config")
    op.execute("ALTER TABLE suvex_solve_telemetry RENAME TO ctf2_solve_telemetry")
    op.execute("ALTER TABLE suvex_writeups RENAME TO ctf2_writeups")
    op.execute("ALTER TABLE suvex_solve_usages RENAME TO ctf2_solve_usages")
    op.execute("ALTER TABLE suvex_solve_notes RENAME TO ctf2_solve_notes")
    op.execute("ALTER TABLE suvex_submissions RENAME TO ctf2_submissions")
    op.execute("ALTER TABLE suvex_solve_attempts RENAME TO ctf2_solve_attempts")
    op.execute("ALTER TABLE suvex_environments RENAME TO ctf2_environments")
    op.execute("ALTER TABLE suvex_challenges RENAME TO ctf2_challenges")
    op.execute("ALTER TABLE suvex_competitions RENAME TO ctf2_competitions")
    op.execute("ALTER TABLE suvex_platforms RENAME TO ctf2_platforms")
    op.execute("ALTER TABLE suvex_accounts RENAME TO ctf2_accounts")
