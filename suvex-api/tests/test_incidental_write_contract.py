"""附带写入的隐性契约：`audit.record()` / `emit_lifecycle_event()` 的调用方必须先 commit。

## 为什么单独一个文件

这两个函数的 docstring 一直写着"失败不影响主流程"，而它们自己会
`db.commit()` + `db.rollback()`。`Session.rollback()` 是**会话级**的 ——
它不区分"我的改动"和"调用方还没提交的改动"。

于是有一条只写在文档里的约定撑了 36 + 3 个调用点的安全：
**"你调我的时候，你的事已经 commit 过了。"**

约定本身是对的（所有调用点确实遵守），但支撑它的是"写代码的人记得这条规矩"，
不是代码本身 —— 这正是本项目反复踩的那类坑（文档/约定与实现脱节）。

v0.47 起 `core.db.warn_if_pending_changes` 让违约**留下 ERROR 日志**，
并且**行为真的不同**：违约时走 SAVEPOINT，不会替调用方 commit、也不会
回滚掉调用方的改动。本文件钉住这两件事。

## 判据必须是"库里到底有没有那行"

内存里的属性在 `rollback()` 之后由 SQLAlchemy 按需重读，看起来可能还是对的
（与 `tests/kernel/test_usage_and_writeup.py` 里那两条同一个理由）。
所以每一条都在 `commit()` + `expire_all()` + 重新 `get()` 之后才断言。
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit import service as audit_service
from app.audit.models import AuditLog
from app.auth.deps import Identity
from app.core.db import pending_change_count, session_scope
from app.core.ids import new_id
from app.core.timeutil import now_utc
from app.core.vocab import AuditAction, ScanStatus
from app.events.models import ActivityEvent
from app.scans import service as scan_service
from tests.factories import make_scan

IDENTITY = Identity(
    kind="session",
    label="session:test",
    scopes=frozenset({"*"}),
    user_id="user_test",
    session_id="sess_test",
    username="admin",
    role="admin",
)


def _request() -> audit_service.AuditRequest:
    return audit_service.AuditRequest(identity=IDENTITY, ip="127.0.0.1", user_agent="pytest")


def _audit_rows(db: Session) -> int:
    return int(db.scalar(select(func.count()).select_from(AuditLog)) or 0)


def _event_rows(db: Session, scan_id: str) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(ActivityEvent)
            .where(ActivityEvent.scan_id == scan_id)
        )
        or 0
    )


# ── 判据本身 ────────────────────────────────────────────


def test_pending_change_count_sees_insert_update_and_delete(db_session: Session) -> None:
    """三种未提交改动都要数到 —— 只数 `new` 会漏掉"改了但没提交"。"""
    assert pending_change_count(db_session) == 0, "干净会话应当报 0"

    row = AuditLog(
        id=new_id("aud"),
        action=AuditAction.SCAN_CREATE.value,
        actor_kind="session",
        actor_label="admin（管理员）",
        summary="探针",
    )
    db_session.add(row)
    assert pending_change_count(db_session) == 1, "新增要数到"

    db_session.expunge(row)
    assert pending_change_count(db_session) == 0, "摘掉之后回到 0"


# ── 正常路径：调用方已 commit ────────────────────────────


def test_audit_record_in_the_normal_path_still_commits_on_its_own(
    db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """契约没被违反时行为**一点都没变**：自己 commit，审计行立刻可见。

    这一条是回归护栏：加固不能把原本正确的那 36 个调用点改坏
    —— 它们依赖 `record()` 自己落盘（`get_db` 不 commit）。
    """
    with caplog.at_level(logging.ERROR, logger="suvex.db"):
        audit_service.record(
            db_session,
            request=_request(),
            action=AuditAction.SCAN_CREATE.value,
            summary="正常路径",
            target="t-normal",
        )

    assert _audit_rows(db_session) == 1, "审计行必须真的落库"
    assert "未提交的业务改动" not in caplog.text, "正常路径不该报警"


# ── 违约路径：会话里压着未提交的业务改动 ────────────────


def test_audit_record_does_not_swallow_uncommitted_business_changes(
    db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """★ 这条测试是本次加固的全部意义所在。

    调用方违约了（改了业务数据却不 commit 就调 `record()`）。
    旧写法会 `db.commit()` —— 把调用方半截的工作**替它落盘**；
    而如果审计写失败，旧写法会 `db.rollback()` —— 把它**整个吞掉**。

    新写法走 SAVEPOINT：调用方那笔改动既不被替它提交、也不会被吞掉，
    而是**留在会话里等它自己 commit**，同时留下一条 ERROR 日志。
    """
    scan = make_scan(db_session, status=ScanStatus.RUNNING.value)
    scan_id = scan.id

    # 模拟"调用方改了业务数据但还没 commit"。
    # ⚠️ 必须同时改 `finished_at` —— `scans` 上有 CHECK
    # `(status IN ('completed','failed','aborted')) = (finished_at IS NOT NULL)`。
    # 只改 status 会撞在约束上，测试就变成在验证数据库而不是验证本文件。
    scan.status = ScanStatus.COMPLETED.value
    scan.finished_at = now_utc()
    assert pending_change_count(db_session) >= 1

    with caplog.at_level(logging.ERROR, logger="suvex.db"):
        audit_service.record(
            db_session,
            request=_request(),
            action=AuditAction.SCAN_CREATE.value,
            summary="违约路径",
            target="t-violation",
        )

    # ① 契约被违反这件事必须**吵出来**
    assert "未提交的业务改动" in caplog.text, "违约必须留 ERROR 日志，不能安静降级"

    # ② ★ 关键判据：`record()` **不许替调用方提交**。
    #
    # 这一条是真正区分"改了行为"与"只多打了一条日志"的地方。
    # 用另一个连接看数据库 —— 内存里的 session 会因为 autoflush
    # 把未提交的行一起带出去，**看不见"还没提交"这件事**。
    #
    # 旧写法（`db.add(row); db.commit()`）在这里会看到 audit_log **已有 1 行**：
    # 它替调用方把半截工作落了盘。新写法走 SAVEPOINT，两行都还没落库。
    with session_scope() as other:
        assert _audit_rows(other) == 0, (
            "违约路径下 record() 不得自己 commit —— 那会把调用方半截的工作一起落盘"
        )

    # ③ 调用方的改动没被吞掉，也没被替它提交
    assert scan.status == ScanStatus.COMPLETED.value, "调用方内存里的改动还在"
    db_session.commit()  # ← 调用方自己提交（这才是它该做的事）
    db_session.expire_all()
    fresh = db_session.get(type(scan), scan_id)
    assert fresh is not None
    assert fresh.status == ScanStatus.COMPLETED.value, "调用方 commit 之后库里必须是它的值"

    # ④ 审计行跟着那次 commit 一起落盘了（SAVEPOINT 只是把失败范围收小）
    assert _audit_rows(db_session) == 1, "审计行应当随调用方的 commit 一起落库"


def test_audit_record_still_works_when_the_savepoint_write_fails(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """违约 + 审计自己写失败：业务改动**仍然**要活着。

    这是加固前会出事的那一半：旧写法走 `db.rollback()`，
    调用方那笔未提交的 status 改动会一起消失，而函数返回成功、
    日志只有一条 warning。
    """
    scan = make_scan(db_session, status=ScanStatus.RUNNING.value)
    scan_id = scan.id
    scan.status = ScanStatus.COMPLETED.value
    scan.finished_at = now_utc()  # CHECK：终态必须有 finished_at
    assert pending_change_count(db_session) >= 1

    # 让审计写入在 flush 时炸掉（CHECK 约束与真实情况同形：写不进去）
    real_add = db_session.add

    def explode(instance: object, *args: object, **kwargs: object) -> None:
        if isinstance(instance, AuditLog):
            raise RuntimeError("模拟审计写不进去")
        real_add(instance, *args, **kwargs)

    monkeypatch.setattr(db_session, "add", explode)

    audit_service.record(  # 不许抛
        db_session,
        request=_request(),
        action=AuditAction.SCAN_CREATE.value,
        summary="写失败",
        target="t-fail",
    )

    assert _audit_rows(db_session) == 0, "审计确实没写进去"

    # ★ 关键：调用方的改动必须活到它自己 commit
    db_session.commit()
    db_session.expire_all()
    fresh = db_session.get(type(scan), scan_id)
    assert fresh is not None
    assert fresh.status == ScanStatus.COMPLETED.value, (
        "审计写失败不得回滚掉调用方未提交的业务改动"
    )


# ── emit_lifecycle_event 同一条契约 ──────────────────────


def test_emit_lifecycle_event_in_the_normal_path_commits(
    db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    scan = make_scan(db_session, status=ScanStatus.RUNNING.value)

    with caplog.at_level(logging.ERROR, logger="suvex.db"):
        scan_service.emit_lifecycle_event(
            db_session,
            scan,
            event_type="discovery",
            summary="正常路径",
            detail={"kind": "info"},
            phase="recon",
        )

    assert _event_rows(db_session, scan.id) >= 1
    assert "未提交的业务改动" not in caplog.text


def test_emit_lifecycle_event_does_not_swallow_uncommitted_business_changes(
    db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    scan = make_scan(db_session, status=ScanStatus.RUNNING.value)
    scan_id = scan.id
    scan.status = ScanStatus.COMPLETED.value  # 未提交
    scan.finished_at = now_utc()  # CHECK：终态必须有 finished_at

    with caplog.at_level(logging.ERROR, logger="suvex.db"):
        scan_service.emit_lifecycle_event(
            db_session,
            scan,
            event_type="completed",
            summary="违约路径",
            detail={"kind": "completed"},
            phase="report",
        )

    assert "未提交的业务改动" in caplog.text

    # ★ 与 audit.record 同一条判据：不许替调用方提交
    with session_scope() as other:
        assert _event_rows(other, scan.id) == 0, (
            "违约路径下 emit_lifecycle_event 不得自己 commit"
        )

    db_session.commit()
    db_session.expire_all()
    fresh = db_session.get(type(scan), scan_id)
    assert fresh is not None
    assert fresh.status == ScanStatus.COMPLETED.value
    assert _event_rows(db_session, scan_id) >= 1, "事件应当随调用方的 commit 一起落库"
