"""「默认沙箱镜像」现在真的有落点（2026-11-09）。

## 事故

设置页那一项原来是一张**空头支票**：解题读的是题目行上的
`sandbox_profile`（同步时按分类写死），设置只在 `start_solve` 里当了个
永远走不到的兜底。改成逆向镜像，一道题都不会变。

现在它有了确切含义 —— **只有分类认不出来（归到「其它」）的新题**才用它。
认得出的题仍按分类走：否则改一次设置就等于推翻所有历史题的镜像，
那比"设置没生效"更糟。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.vocab import KernelSandboxProfile
from app.kernel import service as kernel_service


def _set_default_profile(db: Session, value: str) -> None:
    """把「默认沙箱镜像」写进配置。

    ⚠️ `effective_ctf2_settings` 返回的是**冻结** dataclass（它是给读方用的），
       不能就地赋值 —— 必须走真正的保存入口。
    """
    cur = kernel_service.effective_ctf2_settings(db)
    kernel_service.update_ctf2_settings(
        db,
        submit_mode=cur.submit_mode,
        max_wrong_submissions=cur.max_wrong_submissions,
        default_sandbox_profile=value,
        max_concurrent_solves=cur.max_concurrent_solves,
        solve_poll_seconds=cur.solve_poll_seconds,
        auto_sync_seconds=cur.auto_sync_seconds,
        auto_priority_new=cur.auto_priority_new,
        auto_start_new=cur.auto_start_new,
        max_turns=cur.max_turns,
        fallback_provider_id=cur.fallback_provider_id,
        enable_agentsec_scenarios=cur.enable_agentsec_scenarios,
        enable_ir_scenarios=cur.enable_ir_scenarios,
        enable_multi_agent=cur.enable_multi_agent,
        wall_clock_seconds=cur.wall_clock_seconds,
    )
    db.commit()


def test_unknown_category_uses_the_setting(db_session: Session) -> None:
    _set_default_profile(db_session, KernelSandboxProfile.REVERSE.value)

    profile = kernel_service._profile_for("day1-task-A", db=db_session)

    assert profile == KernelSandboxProfile.REVERSE.value


def test_recognised_category_keeps_its_own_image(db_session: Session) -> None:
    """★ 认得出的题**不能**被设置接管 —— 否则一次改动推翻所有历史题。"""
    _set_default_profile(db_session, KernelSandboxProfile.REVERSE.value)

    assert kernel_service._profile_for("web", db=db_session) == KernelSandboxProfile.BASE.value


def test_default_setting_is_base_so_nothing_changes_by_default(db_session: Session) -> None:
    _set_default_profile(db_session, KernelSandboxProfile.BASE.value)

    assert kernel_service._profile_for("day1-task-A", db=db_session) == KernelSandboxProfile.BASE.value
