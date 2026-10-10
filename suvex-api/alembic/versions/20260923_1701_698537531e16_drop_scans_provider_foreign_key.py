"""drop scans provider foreign key

移除 `scans.provider_id` 上的外键约束
（`fk_scans_provider_id_provider_accounts`，`ondelete=RESTRICT`）。

## 为什么

它是一条**历史引用**，不是引用完整性约束 —— 记录的是"这次扫描当时用的是
哪个 provider"。而 provider 是会被删的。

`RESTRICT` 的后果是**任何一条历史扫描都会永久锁住那个 provider 的删除**，
而且用户看到的是一个数据库层的 500（英文的 foreign key violation），
不是我们能解释的中文。契约 §6 只要求「有 running 扫描引用时返回 409」，
也就是"只拦未结束的" —— `RESTRICT` 比它严得多，且无法表达这个区分。

改 `SET NULL` 也不行：`Scan.providerId` 在前端是 `string`（非空）。
软删除能保住外键，但代价是每条 provider 查询都要加 `deleted_at IS NULL`。

完整性改为**在写入时保证**：`POST /scans`（任务 3）校验 provider 存在且可用。
历史引用的完整性本来就该在写入那一刻保证，而不是靠"永不允许删除被引用过的东西"。

完整论证见 `app/scans/models.py` 里 `Scan` 的注释。

## 这个改动是写测试时逼出来的

`test_deleting_a_provider_used_only_by_finished_scans_works` 直接把
`IntegrityError` 摆了出来：一条**已完成**的扫描也拦住了删除。
契约 §6 的意图明显不是这样。

Revision ID: 698537531e16
Revises: 54eaafabd74c
Create Date: 2026-09-23 17:01:29.921545+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# 迁移版本标识，由 alembic 分配，不要手改
revision: str = '698537531e16'
down_revision: str | None = '54eaafabd74c'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        'fk_scans_provider_id_provider_accounts', 'scans', type_='foreignkey'
    )


def downgrade() -> None:
    # 降级会把约束加回来。
    #
    # ⚠️ 若此时库里已有"引用了不存在 provider"的扫描（正是这次改动允许
    # 出现的情况），这条语句会**失败**。这不是 bug，是降级的固有代价：
    # 约束回来了，数据却已经不符合它。
    #
    # 刻意**不**做成"自动清理那些悬空引用"：那意味着一次降级会悄悄删掉
    # 扫描记录，而扫描记录是审计留痕。宁可让降级失败并要求人工处理 ——
    # 一个会静默删审计数据的降级脚本比一个会失败的降级脚本危险得多。
    op.create_foreign_key(
        'fk_scans_provider_id_provider_accounts',
        'scans',
        'provider_accounts',
        ['provider_id'],
        ['id'],
        ondelete='RESTRICT',
    )
