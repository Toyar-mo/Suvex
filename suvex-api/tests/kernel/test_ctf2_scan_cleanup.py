"""CTF2 解题壳的收尾（2026-11-08）。

## 这个文件在防什么

解题会给每道题**借一条 scan 壳**（`origin='ctf2'`），状态条与扫描列表
按 `scans.status` 数「N 个扫描运行中」。实测那天：

  · 取消了 5 道 PWN、杀掉沙箱、重启服务之后，界面上仍有 **24 个**
    「运行中」；
  · 其中 20 条对应的 worker 早就不存在 —— 数字是假的。

两个漏洞同时存在：

1. `cancel_challenge` 只改题目行，不碰那条壳；
2. 进程重启后，没有任何人把遗留的 `running` 壳收尾。

用户会拿这个数字判断"系统在不在忙、还剩多少活"，所以它不是装饰性
字段。两条路径各钉一个用例。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.timeutil import now_utc
from app.core.vocab import KernelChallengeStatus, ScanStatus
from app.kernel import events, service
from tests.factories import make_provider, make_scan


def _ensure_provider(db: Session) -> str:
    """拿一个可用的 provider（没有就建一个）。

    ★ 不用现成的 `ctf2_provider` 夹具：它每次都新建一个 default provider，
      而本文件的用例会 `commit`（取消路径本身就在提交），于是第二个用例
      会撞上 `uq_provider_accounts_default` 唯一约束。
    """
    from sqlalchemy import select

    from app.providers.models import ProviderAccount

    existing = db.scalars(
        select(ProviderAccount).order_by(ProviderAccount.created_at)
    ).first()
    if existing is not None:
        return existing.id
    return make_provider(db, provider_id="prov_ctf2_cleanup_test")


def test_cancel_aborts_the_borrowed_scan(
    db_session: Session, ctf2_challenge
) -> None:
    """取消解题 → 那道题借的 scan 壳必须一起收尾。"""
    _ensure_provider(db_session)
    shell = events.ensure_solve_scan(db_session, ctf2_challenge)
    ctf2_challenge.status = KernelChallengeStatus.SOLVING.value
    db_session.commit()

    service.cancel_challenge(db_session, ctf2_challenge)

    db_session.refresh(shell)
    assert shell.status == ScanStatus.ABORTED.value, "题目取消了，壳还挂着 running"
    assert shell.finished_at is not None, "终态必须带结束时刻（数据库约束也要求）"
    assert "取消" in (shell.failure_reason or "")


def test_cancel_leaves_a_finished_scan_alone(
    db_session: Session, ctf2_challenge
) -> None:
    """已经收尾的壳不该被改写成 aborted —— 那会把"解出"改成"中止"。"""
    _ensure_provider(db_session)
    shell = events.ensure_solve_scan(db_session, ctf2_challenge)
    shell.status = ScanStatus.COMPLETED.value
    shell.finished_at = now_utc()  # 终态必须带结束时刻（数据库约束）
    db_session.commit()

    service.cancel_challenge(db_session, ctf2_challenge)

    db_session.refresh(shell)
    assert shell.status == ScanStatus.COMPLETED.value


def test_startup_sweep_aborts_only_orphan_ctf2_shells(
    db_session: Session, ctf2_challenge
) -> None:
    """启动收尾只碰 ctf2 壳，真扫描（其他 runner 在跑）不动。"""
    provider_id = _ensure_provider(db_session)
    orphan = events.ensure_solve_scan(db_session, ctf2_challenge)
    # 显式传 provider：不传的话工厂会自己造一个默认 provider，撞唯一约束
    real = make_scan(db_session, status=ScanStatus.RUNNING.value, provider_id=provider_id)
    db_session.commit()

    swept = service.abort_orphan_ctf2_scans(db_session)

    assert swept == 1
    db_session.refresh(orphan)
    db_session.refresh(real)
    assert orphan.status == ScanStatus.ABORTED.value
    assert orphan.finished_at is not None
    assert real.status == ScanStatus.RUNNING.value, "真扫描被误杀了"


def test_startup_sweep_is_a_noop_when_nothing_is_stale(db_session: Session) -> None:
    assert service.abort_orphan_ctf2_scans(db_session) == 0
