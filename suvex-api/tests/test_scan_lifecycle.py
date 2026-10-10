"""扫描列表的归档 / 软删除 / 回收站 / 保留期（任务 9，契约 v0.13）。

## 这个文件盯的是"数据不会意外消失"

三个机制合起来才成立，任何一环断了都会让用户丢数据：

| 机制 | 保证 |
| --- | --- |
| `archivedAt` | 归档只是**从默认视图移走**，数据全在 |
| `deletedAt` | 列表上的"删除"是**软删除**，进回收站而不是消失 |
| `purge` / `sweep` | 真正的物理删除只有两条路，**都被报告引用挡住** |

最后一条是本文件最要紧的：`report_findings.finding_id → findings
ON DELETE CASCADE`，而 `findings.scan_id → scans ON DELETE CASCADE`。
所以物理删一次扫描会**连带删掉它的漏洞**，进而掏空引用它们的报告 ——
而报告是交付物。这条路径必须有测试钉住。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timeutil import now_utc
from app.scans.models import Scan
from app.scans import service as scans_service
from tests.factories import make_scan

AUTH_STATEMENT = "我有权对此目标进行安全测试"
VALID_AUTH = {"confirmed": True, "statement": AUTH_STATEMENT}


# ══════════════════════════════════════════════════════════
# 辅助
# ══════════════════════════════════════════════════════════


def _scan(db: Session, *, status: str = "completed", target: str = "a.example.com") -> Scan:
    return make_scan(db, status=status, target=target)


def _listed(client: TestClient, **params) -> list[dict]:
    response = client.get("/api/v1/scans", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _add_finding(db: Session, scan: Scan, *, title: str = "一条漏洞", fid: str = "f_x") -> str:
    from app.findings.models import Finding

    db.add(
        Finding(
            id=fid,
            scan_id=scan.id,
            source_ref=f"vuln-{fid}",
            severity="high",
            category="misconfiguration",
            title=title,
            target=scan.target,
            verification="verified",
            status="confirmed",
            discovered_at=now_utc(),
        )
    )
    db.commit()
    return fid


# ══════════════════════════════════════════════════════════
# 一、默认视图的可见性
# ══════════════════════════════════════════════════════════


def test_default_list_hides_archived_and_deleted(
    ready_client: TestClient, db_session: Session
) -> None:
    """★ 默认视图既不含归档、也不含回收站。

    这条是**服务端过滤**而不是前端筛的结果：列表页的 tab 计数直接数
    返回的数组（"全部"用 `length`）。如果把归档的混在同一个数组里返回，
    "全部 N"会当场把归档的也算进去 —— 那个数字会撒谎。
    """
    keep = _scan(db_session, target="keep.example.com")
    archived = _scan(db_session, target="archived.example.com")
    deleted = _scan(db_session, target="deleted.example.com")

    ready_client.post("/api/v1/scans/batch/archive", json={"ids": [archived.id], "archived": True})
    ready_client.post("/api/v1/scans/batch/delete", json={"ids": [deleted.id]})

    default_targets = {s["target"] for s in _listed(ready_client)}
    assert default_targets == {keep.target}, f"默认视图漏了：{default_targets}"

    archived_targets = {s["target"] for s in _listed(ready_client, archived=True)}
    assert archived_targets == {archived.target}

    trash_targets = {s["target"] for s in _listed(ready_client, deleted=True)}
    assert trash_targets == {deleted.target}


def test_archived_scan_keeps_all_its_data(
    ready_client: TestClient, db_session: Session
) -> None:
    """归档**只是从视图里移走**，数据一样都不能少。

    这条挡的是"把归档实现成删除" —— 那是最容易偷懒的写法，
    而用户在界面上看不出区别，直到他想取消归档。
    """
    scan = _scan(db_session)
    _add_finding(db_session, scan, fid="f_keep")
    before = db_session.get(Scan, scan.id)
    assert before is not None

    ready_client.post("/api/v1/scans/batch/archive", json={"ids": [scan.id], "archived": True})

    db_session.expire_all()  # HTTP 用的是另一个会话，必须让它重读
    still = db_session.get(Scan, scan.id)
    assert still is not None, "归档把记录弄丢了"
    assert still.archived_at is not None
    assert still.deleted_at is None
    from app.findings.models import Finding

    assert db_session.get(Finding, "f_keep") is not None, "归档把漏洞弄丢了"


# ══════════════════════════════════════════════════════════
# 二、归档 / 取消归档
# ══════════════════════════════════════════════════════════


def test_archive_is_idempotent_and_keeps_the_original_timestamp(
    ready_client: TestClient, db_session: Session
) -> None:
    """★ 重复归档算 `skipped`，且**不刷新** `archivedAt`。

    为什么要紧：`archivedAt` 是保留期与事后解释的依据
    （"这条为什么被归档了"）。每次点击都重写它，
    就等于把"当初归档的时刻"改成"我最后一次点它的时刻"。
    """
    scan = _scan(db_session)

    first = ready_client.post(
        "/api/v1/scans/batch/archive", json={"ids": [scan.id], "archived": True}
    ).json()
    assert first["changed"] == 1 and first["skipped"] == 0

    # ★ 两次都在**库里**读，不经过 API 的字符串。
    #   走 API 会引入一个纯格式化的差异：API 出的是毫秒（`…Z`，
    #   微秒被截掉），而库里是微秒。拿两者比会因为 526 微秒的差失败，
    #   而那条失败信息看起来像"时间戳变了"，完全指错方向。
    db_session.expire_all()
    stamp = db_session.get(Scan, scan.id).archived_at
    assert stamp is not None

    second = ready_client.post(
        "/api/v1/scans/batch/archive", json={"ids": [scan.id], "archived": True}
    ).json()
    assert second["changed"] == 0 and second["skipped"] == 1
    assert second["scans"] == [], "幂等重放不该返回 changed"

    db_session.expire_all()
    assert db_session.get(Scan, scan.id).archived_at == stamp, (
        "幂等重放刷新了归档时刻 —— 那就丢掉了「当初什么时候归档的」"
    )


def test_archive_requires_explicit_direction(
    ready_client: TestClient, db_session: Session
) -> None:
    """★ `archived` 必填 —— 破坏性动作不能有默认方向。

    漏传时如果默认成 `true`，一次本想取消归档的请求会**默默归档**。
    """
    scan = _scan(db_session)
    response = ready_client.post("/api/v1/scans/batch/archive", json={"ids": [scan.id]})
    assert response.status_code == 400
    assert "archived" in response.text


def test_unarchive_puts_it_back(ready_client: TestClient, db_session: Session) -> None:
    scan = _scan(db_session)
    ready_client.post("/api/v1/scans/batch/archive", json={"ids": [scan.id], "archived": True})
    assert _listed(ready_client) == []

    ready_client.post("/api/v1/scans/batch/archive", json={"ids": [scan.id], "archived": False})
    assert {s["target"] for s in _listed(ready_client)} == {scan.target}


def test_archiving_works_on_any_status(ready_client: TestClient, db_session: Session) -> None:
    """归档与 status **正交** —— 任何终态都能归档。"""
    for status in ("completed", "failed", "aborted"):
        scan = _scan(db_session, status=status, target=f"{status}.example.com")
        response = ready_client.post(
            "/api/v1/scans/batch/archive", json={"ids": [scan.id], "archived": True}
        )
        assert response.status_code == 200, response.text
        assert response.json()["changed"] == 1, f"{status} 不能归档"


def test_batch_reports_missing_ids_once(ready_client: TestClient, db_session: Session) -> None:
    """缺 id 时**一次性说清**，不是改到第一个不存在就报错。"""
    scan = _scan(db_session)
    response = ready_client.post(
        "/api/v1/scans/batch/archive",
        json={"ids": [scan.id, "scan_nope_a", "scan_nope_b"], "archived": True},
    )
    assert response.status_code == 404
    assert "2 次扫描记录" in response.json()["error"]["message"]


# ══════════════════════════════════════════════════════════
# 三、软删除 / 恢复
# ══════════════════════════════════════════════════════════


def test_delete_is_soft_and_restorable(ready_client: TestClient, db_session: Session) -> None:
    """★ 列表上的"删除"**不物理删数据**，而且在回收站里能恢复。"""
    scan = _scan(db_session)
    _add_finding(db_session, scan, fid="f_trash")

    deleted = ready_client.post("/api/v1/scans/batch/delete", json={"ids": [scan.id]}).json()
    assert deleted["changed"] == 1

    # ⚠️ HTTP 用的是**另一个会话**。不 expire 的话这里读到的还是
    # 本会话身份映射里的旧对象（deleted_at=None），
    # 于是测试以"改动没生效"的形式失败 —— 指向实现，其实是缓存。
    db_session.expire_all()
    still = db_session.get(Scan, scan.id)
    assert still is not None, "软删除把数据删掉了 —— 那不是软删除"
    assert still.deleted_at is not None

    restored = ready_client.post("/api/v1/scans/batch/restore", json={"ids": [scan.id]}).json()
    assert restored["changed"] == 1
    db_session.expire_all()
    assert db_session.get(Scan, scan.id).deleted_at is None
    assert {s["target"] for s in _listed(ready_client)} == {scan.target}


def test_restore_does_not_silently_unarchive(ready_client: TestClient, db_session: Session) -> None:
    """★ 恢复**不碰**归档状态。

    用户只要求"恢复"，顺手取消归档等于做了一件他没要求的事 ——
    而归档是他之前明确做过的决定。
    """
    scan = _scan(db_session)
    ready_client.post("/api/v1/scans/batch/archive", json={"ids": [scan.id], "archived": True})
    ready_client.post("/api/v1/scans/batch/delete", json={"ids": [scan.id]})
    ready_client.post("/api/v1/scans/batch/restore", json={"ids": [scan.id]})

    db_session.expire_all()
    row = db_session.get(Scan, scan.id)
    assert row.deleted_at is None
    assert row.archived_at is not None, "恢复顺手取消了归档"
    # 恢复后应当回到「已归档」tab，而不是默认视图
    assert _listed(ready_client) == []
    assert {s["target"] for s in _listed(ready_client, archived=True)} == {scan.target}


# ══════════════════════════════════════════════════════════
# 四、永久删除（两道闸）
# ══════════════════════════════════════════════════════════


def test_purge_requires_the_scan_to_be_in_trash(
    ready_client: TestClient, db_session: Session
) -> None:
    """★ 第一道闸：不在回收站里不能永久删除。

    "先删进回收站，再永久删除"是两道闸。合成一步等于把 purge
    变成一个不经过回收站的 `rm -rf`。
    """
    scan = _scan(db_session)
    response = ready_client.delete(f"/api/v1/scans/{scan.id}/purge")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SCAN_NOT_IN_TRASH"
    db_session.expire_all()
    assert db_session.get(Scan, scan.id) is not None


def test_purge_is_blocked_by_a_report_that_references_it(
    ready_client: TestClient, db_session: Session
) -> None:
    """★★ 第二道闸，也是最要紧的一条：报告引用时拒绝永久删除。

    为什么必须有：`report_findings.finding_id → findings ON DELETE CASCADE`，
    而 `findings.scan_id → scans ON DELETE CASCADE`。所以物理删一次扫描会
    **连带删掉它的漏洞**，进而删掉报告与那些漏洞的关联行 ——
    报告的 `findingIds` 还在，但它们指向的东西没了，那份报告被掏空。

    报告是**交付物**（有编号、客户拿去对账）。让它在清回收站时
    悄悄变空，是不可接受的。
    """
    scan = _scan(db_session)
    _add_finding(db_session, scan, fid="f_reported")

    report = ready_client.post(
        "/api/v1/reports",
        json={
            "title": "会挡住删除的报告",
            "scope": {"kind": "scan", "scanId": scan.id},
            "options": {
                "includePoc": False,
                "includeReasoning": False,
                "onlyConfirmed": False,
                "includeRemediation": False,
            },
            "format": "markdown",
        },
    )
    assert report.status_code == 201, report.text
    code = report.json()["code"]

    ready_client.post("/api/v1/scans/batch/delete", json={"ids": [scan.id]})
    response = ready_client.delete(f"/api/v1/scans/{scan.id}/purge")

    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "SCAN_REFERENCED_BY_REPORT"
    # ★ 必须说出**是哪份报告** —— 否则用户只能一份份翻
    assert code in error["message"], f"没告诉用户是哪份报告：{error['message']}"
    # 数据必须还在
    db_session.expire_all()
    assert db_session.get(Scan, scan.id) is not None


def test_purge_succeeds_when_nothing_references_it(
    ready_client: TestClient, db_session: Session
) -> None:
    """反面：没有报告引用时，永久删除要真的删掉（含级联的漏洞）。"""
    from app.findings.models import Finding

    scan = _scan(db_session)
    _add_finding(db_session, scan, fid="f_purge_me")
    ready_client.post("/api/v1/scans/batch/delete", json={"ids": [scan.id]})

    response = ready_client.delete(f"/api/v1/scans/{scan.id}/purge")
    assert response.status_code == 204, response.text

    # ★ 用 expunge_all 而不是 expire_all。
    #   expire 之后身份映射里**还留着**那个已删除的实例，
    #   再次 `get` 会去刷新它、然后抛 `ObjectDeletedError`
    #   （而不是干净地返回 None）。expunge 把身份映射清空，
    #   于是 get 变成一次真正的 SELECT，如实返回 None。
    db_session.expunge_all()
    assert db_session.get(Scan, scan.id) is None
    assert db_session.get(Finding, "f_purge_me") is None, "级联没生效（漏洞还在）"


def test_purge_is_recorded_in_the_audit_log(
    ready_client: TestClient, db_session: Session
) -> None:
    """永久删除是**不可逆**的，必须有痕。"""
    from app.audit.models import AuditLog

    scan = _scan(db_session)
    ready_client.post("/api/v1/scans/batch/delete", json={"ids": [scan.id]})
    ready_client.delete(f"/api/v1/scans/{scan.id}/purge")

    rows = list(db_session.execute(select(AuditLog).where(AuditLog.action == "scan.purge")).scalars())
    assert len(rows) == 1
    assert rows[0].target == scan.id


def test_archive_is_recorded_in_the_audit_log(
    ready_client: TestClient, db_session: Session
) -> None:
    """归档要留痕 —— 它让记录**从默认视图里消失**，事后要能解释。"""
    from app.audit.models import AuditLog

    scan = _scan(db_session)
    ready_client.post("/api/v1/scans/batch/archive", json={"ids": [scan.id], "archived": True})

    rows = list(
        db_session.execute(select(AuditLog).where(AuditLog.action == "scan.archive")).scalars()
    )
    assert len(rows) == 1
    assert "归档" in rows[0].summary
    assert (rows[0].detail or {})["archived"] is True


# ══════════════════════════════════════════════════════════
# 五、保留期清理
# ══════════════════════════════════════════════════════════


def _age(scan: Scan, db: Session, *, days: int) -> None:
    """把一条扫描的结束/删除时刻往前推 N 天。"""
    moment = now_utc() - timedelta(days=days)
    scan.finished_at = moment
    db.commit()


def test_sweep_auto_archives_old_failed_and_aborted(db_session: Session) -> None:
    """★ 规则：失败 / 已终止 N 天后自动归档。

    **只归档这两种状态** —— 已完成的扫描是用户要回看的结果，
    替他归档等于把结果藏起来。
    """
    old_failed = _scan(db_session, status="failed", target="old-failed.example.com")
    _age(old_failed, db_session, days=40)
    old_aborted = _scan(db_session, status="aborted", target="old-aborted.example.com")
    _age(old_aborted, db_session, days=40)
    old_completed = _scan(db_session, status="completed", target="old-completed.example.com")
    _age(old_completed, db_session, days=40)
    fresh_failed = _scan(db_session, status="failed", target="fresh.example.com")
    _age(fresh_failed, db_session, days=1)

    result = scans_service.sweep_retention(db_session)
    assert result["archived"] == 2, f"归档了 {result['archived']} 条，应为 2"

    db_session.expire_all()
    assert db_session.get(Scan, old_failed.id).archived_at is not None
    assert db_session.get(Scan, old_aborted.id).archived_at is not None
    assert db_session.get(Scan, old_completed.id).archived_at is None, "已完成的被误归档了"
    assert db_session.get(Scan, fresh_failed.id).archived_at is None, "还没到期的被归档了"


def test_sweep_can_be_turned_off(db_session: Session) -> None:
    """`archiveAfterDays = 0` = **显式关闭**规则。"""
    scan = _scan(db_session, status="failed")
    _age(scan, db_session, days=999)

    config = scans_service.get_retention_config(db_session)
    config.archive_after_days = 0
    db_session.commit()

    result = scans_service.sweep_retention(db_session)
    assert result["archived"] == 0
    db_session.expire_all()
    assert db_session.get(Scan, scan.id).archived_at is None


def test_sweep_purges_expired_trash(db_session: Session) -> None:
    """回收站到期 → 物理删除。"""
    scan = _scan(db_session)
    ready = now_utc() - timedelta(days=40)
    scan.deleted_at = ready
    db_session.commit()

    result = scans_service.sweep_retention(db_session)
    assert result["purged"] == 1
    db_session.expunge_all()
    assert db_session.get(Scan, scan.id) is None


def test_sweep_refuses_to_destroy_a_scan_a_report_needs(
    ready_client: TestClient, db_session: Session
) -> None:
    """★★ 后台清理**跳过**被报告引用的扫描，并如实计数。

    这里**不能**沿用 `purge_scan` 的"抛 409" —— 那是个后台流程，
    没有请求方可以接收错误。所以静默跳过 + 计数，
    由调用方把"有几条因为被报告引用而保留了"说出来。

    悄悄删掉交付物的来源，比留一条过期的回收站记录严重得多。
    """
    from app.findings.models import Finding

    scan = _scan(db_session)
    finding_id = _add_finding(db_session, scan, fid="f_keep_alive")

    # 走真实 API 建报告 —— 与"用户真的做过这件事"一致，
    # 也避免手搓 ORM 对象时漏字段（那是测试自己的 bug，不是实现的）。
    created = ready_client.post(
        "/api/v1/reports",
        json={
            "title": "引用了这次扫描的报告",
            "scope": {"kind": "scan", "scanId": scan.id},
            "options": {
                "includePoc": False,
                "includeReasoning": False,
                "onlyConfirmed": False,
                "includeRemediation": False,
            },
            "format": "markdown",
        },
    )
    assert created.status_code == 201, created.text

    scan.deleted_at = now_utc() - timedelta(days=400)
    db_session.commit()

    result = scans_service.sweep_retention(db_session)

    assert result["purged"] == 0, "把有报告引用的扫描删了"
    assert result["keptBecauseReferenced"] == 1
    db_session.expire_all()
    assert db_session.get(Scan, scan.id) is not None
    assert db_session.get(Finding, finding_id) is not None


def test_sweep_records_when_it_ran(db_session: Session) -> None:
    """`lastSweepAt` 要写下来 —— 否则"我设了 30 天为什么没生效"无法回答。"""
    config = scans_service.get_retention_config(db_session)
    assert config.last_sweep_at is None

    scans_service.sweep_retention(db_session)

    db_session.refresh(config)
    assert config.last_sweep_at is not None


def test_retention_config_round_trips_through_the_api(
    ready_client: TestClient, db_session: Session
) -> None:
    """设置页能读到、能改、改完立刻生效（保存时顺手跑一次清理）。"""
    got = ready_client.get("/api/v1/settings/scan-retention").json()
    assert got["archiveAfterDays"] == 30
    assert got["trashRetentionDays"] == 30
    # ⚠️ **不断言 `lastSweepAt is None`** —— 服务启动时会跑一次清理
    #（`main.lifespan` → `sweep_retention`），所以这时候它通常已经有值了。
    # 那条断言是我写测试时的想当然，而它恰好证明了启动清理真的在跑。

    scan = _scan(db_session, status="failed")
    _age(scan, db_session, days=10)

    # 把阈值改成 5 天 —— 那条 10 天前的失败扫描应当立刻被归档
    saved = ready_client.put(
        "/api/v1/settings/scan-retention",
        json={"archiveAfterDays": 5, "trashRetentionDays": 30},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["archiveAfterDays"] == 5
    assert saved.json()["lastSweepAt"] is not None, "保存后没有立刻跑清理"

    db_session.expire_all()
    assert db_session.get(Scan, scan.id).archived_at is not None


# ══════════════════════════════════════════════════════════
# 六、对比
# ══════════════════════════════════════════════════════════


def test_compare_reports_added_and_removed(
    ready_client: TestClient, db_session: Session
) -> None:
    """★ 按**标题**比，不按 id —— 否则两次同一目标的扫描会得出"全都不一样"。"""
    old = _scan(db_session, target="pinotes.xyz")
    new = _scan(db_session, target="pinotes.xyz")

    _add_finding(db_session, old, title="一直存在的问题", fid="f_both_a")
    _add_finding(db_session, old, title="这次修好了", fid="f_only_a")
    _add_finding(db_session, new, title="一直存在的问题", fid="f_both_b")
    _add_finding(db_session, new, title="新引入的问题", fid="f_only_b")

    body = ready_client.get(
        "/api/v1/scans/compare", params={"a": old.id, "b": new.id}
    ).json()

    assert body["onlyInA"] == ["这次修好了"]
    assert body["onlyInB"] == ["新引入的问题"]
    assert body["common"] == ["一直存在的问题"]
    assert "新增 1 项" in body["verdict"]
    assert "消失 1 项" in body["verdict"]


def test_compare_of_identical_scans_says_so(
    ready_client: TestClient, db_session: Session
) -> None:
    a = _scan(db_session, target="same.example.com")
    b = _scan(db_session, target="same.example.com")
    _add_finding(db_session, a, title="同一问题", fid="f_same_a")
    _add_finding(db_session, b, title="同一问题", fid="f_same_b")

    body = ready_client.get("/api/v1/scans/compare", params={"a": a.id, "b": b.id}).json()
    assert body["onlyInA"] == [] and body["onlyInB"] == []
    assert "完全一致" in body["verdict"]


# ══════════════════════════════════════════════════════════
# 七、批量重跑：不能绕过授权确认
# ══════════════════════════════════════════════════════════


# ⚠️ 这里**故意没有** provider 夹具。
#
# `make_scan` 的工厂已经会建一个 provider（`prov_deepseek`，默认）。
# 而 `POST /providers` 在请求体写 `isDefault: false` 时**仍会补空缺** ——
# `create_provider` 里走的是 `_ensure_default()`：
# "没有默认就把它设成默认"。所以夹具再 POST 一个，就会与工厂那个
# 撞在唯一索引 `uq_provider_accounts_default` 上。
#
# 这个夹具原来就在犯这个错。它的失败信息（唯一键冲突）指向的是
# 测试夹具，而不是被测实现 —— 那是测试自己的 bug。


def test_batch_rerun_requires_authorization(
    ready_client: TestClient, db_session: Session
) -> None:
    """★ 批量重跑**必须**带授权确认，不能因为"批量"就绕过 §5.3。

    少了这条，`POST /scans/batch/rerun` 会变成一条**绕过授权确认的捷径**：
    点一次，N 个目标被真的发包，而没有任何一条留痕。
    """
    scan = _scan(db_session)

    # 完全不带 authorization → 403（授权缺失，不是内容不合法）
    missing = ready_client.post("/api/v1/scans/batch/rerun", json={"ids": [scan.id]})
    assert missing.status_code in (400, 403), missing.text

    # 带了但措辞不对 → 400，且**逐条**校验
    wrong = ready_client.post(
        "/api/v1/scans/batch/rerun",
        json={"ids": [scan.id], "authorization": {"confirmed": True, "statement": "我确认过了"}},
    )
    assert wrong.status_code == 200, wrong.text
    body = wrong.json()
    assert body["created"] == [], "措辞不对却建出了扫描"
    assert len(body["failed"]) == 1
    assert "措辞" in body["failed"][0]["error"]


def test_batch_rerun_creates_one_record_per_input(
    ready_client: TestClient, db_session: Session
) -> None:
    """逐条新建，且**逐条失败** —— 一条坏不该让其余全灭。"""
    good = _scan(db_session, target="ok.example.com")
    # 让它的 provider 指向不存在的一个 —— 单条会失败
    bad = _scan(db_session, target="bad.example.com")
    bad.provider_id = "prov_does_not_exist"
    db_session.commit()

    response = ready_client.post(
        "/api/v1/scans/batch/rerun",
        json={"ids": [good.id, bad.id], "authorization": dict(VALID_AUTH)},
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert len(body["created"]) == 1, f"好的那条没建出来：{body}"
    assert len(body["failed"]) == 1, f"坏的那条没被单独报告：{body}"
    assert body["failed"][0]["scanId"] == bad.id
    # 重跑是**新建**，不是复用原记录
    assert body["created"][0]["id"] != good.id
    assert body["created"][0]["target"] == good.target
