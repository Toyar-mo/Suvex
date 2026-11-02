"""错误提交上限：v1.46 从 10 抬到 50（羊城杯赛制）。

## 为什么要一条**真跑**的用例

`tests/kernel/test_schemas.py::test_wrong_submission_ceiling_matches_...`
只证明"代码常量与表上 CHECK 的字面量一致"，那是**静态**的。它证明不了
"数据库真的接受 50、拒绝 51" —— 而后者才是比赛当天会踩的：应用层放行、
数据库 CHECK 拒绝，症状是"设置页显示保存了但没存进去"。

所以这里两条：
1. `_assert_wrong_ceiling(50)` 不抛（应用层放行）；
2. 真往库里写 `max_wrong_submissions=51` → 数据库 CHECK 拒绝（IntegrityError）。
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.kernel.service import (
    MAX_WRONG_SUBMISSIONS_CEILING,
    MIN_WRONG_SUBMISSIONS,
    _assert_wrong_ceiling,
)


def test_ceiling_is_50() -> None:
    """字面钉死：竞争规则是每题 50 次。改了这条要一起想清楚赛制。"""
    assert MAX_WRONG_SUBMISSIONS_CEILING == 50
    assert MIN_WRONG_SUBMISSIONS == 0


@pytest.mark.parametrize("value", [0, 1, 3, 10, 49, 50])
def test_assert_accepts_within_ceiling(value: int) -> None:
    _assert_wrong_ceiling(value)  # 不抛即通过


@pytest.mark.parametrize("value", [-1, 51, 100])
def test_assert_rejects_over_ceiling(value: int) -> None:
    from app.core.errors import ApiError

    with pytest.raises(ApiError):
        _assert_wrong_ceiling(value)


def test_database_accepts_50_and_rejects_51(db_session) -> None:
    """真写库：50 存得进去，51 被数据库 CHECK 挡下。

    这是"迁移真的应用了"的运行时证据 —— 没跑迁移的库，写 50 会直接
    IntegrityError（老约束 `<= 10`）。
    """
    from app.settings.models import SINGLETON_ID, KernelConfig

    config = db_session.get(KernelConfig, SINGLETON_ID)
    if config is None:
        config = KernelConfig(id=SINGLETON_ID)
        db_session.add(config)
        db_session.flush()

    # 50 必须通过
    config.max_wrong_submissions = 50
    db_session.flush()

    # 51 必须被 CHECK 拒绝
    config.max_wrong_submissions = 51
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()
