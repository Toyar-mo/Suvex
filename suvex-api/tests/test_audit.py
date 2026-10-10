"""审计日志（任务 8 要求 2）。

> 谁在什么时间对什么目标发起了扫描、谁变更了漏洞状态、谁下载了报告 ——
> 这些操作要有独立的审计日志表，不能只靠应用日志
> （应用日志会被日志轮转清理，审计日志需要更长的保留策略）。

对应验收："高风险操作的鉴权和**审计留痕**都能在真实请求中验证到" ——
所以下面大多数用例都是**打真实 HTTP 请求，然后回数据库读那一行**，
而不是直接调 `service.record()`。直接调 service 只能证明"写函数能用"，
证明不了"那个端点真的调了它"。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.models import AuditLog
from app.audit import service as audit_service
from app.core.timeutil import now_utc

AUTH_STATEMENT = "我有权对此目标进行安全测试"
VALID_AUTH = {"confirmed": True, "statement": AUTH_STATEMENT}
KEY = "sk-test-AUDIT-3f8a1c4e9b7d30625a8c1f4e"


@pytest.fixture()
def provider_id(ready_client: TestClient) -> str:
    created = ready_client.post(
        "/api/v1/providers",
        json={
            "label": "DeepSeek",
            "model": "deepseek-chat",
            "kind": "cloud",
            "apiKey": KEY,
            "baseUrl": "https://api.deepseek.com",
            "note": "审计测试用",
            "isDefault": True,
        },
    )
    assert created.status_code in (200, 201), created.text
    return created.json()["provider"]["id"]


def _rows(db: Session, action: str | None = None) -> list[AuditLog]:
    stmt = select(AuditLog).order_by(AuditLog.at)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    return list(db.execute(stmt).scalars())


def _create_scan(client: TestClient, provider_id: str, *, target: str = "audit.example.com") -> dict:
    response = client.post(
        "/api/v1/scans",
        json={
            "name": "",
            "target": target,
            "targetKind": "url",
            "skills": ["web"],
            "providerId": provider_id,
            "authorization": dict(VALID_AUTH),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


# ══════════════════════════════════════════════════════════
# ① 发起扫描留痕
# ══════════════════════════════════════════════════════════


def test_creating_a_scan_writes_an_audit_row(
    ready_client: TestClient, provider_id: str, db_session: Session
) -> None:
    """★ 这是任务 8 要求 2 点名的第一条：谁在什么时候对什么目标发起了扫描。"""
    scan = _create_scan(ready_client, provider_id, target="audit-one.example.com")

    rows = _rows(db_session, "scan.create")
    assert len(rows) == 1, f"应该正好一条，实际 {len(rows)} 条"
    row = rows[0]

    # 谁
    assert row.actor_kind == "session"
    assert row.actor_label == "管理员会话"
    assert row.actor_id, "会话通道必须记下账号 id"
    # 什么时候（服务端时刻）
    assert row.at is not None
    # 对什么目标
    assert scan["target"] in row.summary
    assert scan["target"] in (row.detail or {}).get("target", "")
    # 哪一次扫描（追责时要能一路查到那条扫描记录）
    assert row.target == scan["id"]
    # 从哪来（服务端才知道的，不可伪造）
    assert row.ip
    assert row.user_agent
    # 人读得懂
    assert "发起" in row.summary


def test_audit_records_the_machine_token_not_the_admin(
    ready_client: TestClient, provider_id: str, db_session: Session
) -> None:
    """★ 令牌通道下必须记**令牌**，不能记成"管理员会话"。

    "管理员本人点的"与"某个 CI 令牌干的"是两件不同的事 ——
    出了事第一个要回答的就是这个区别。这也是验收里
    "审计留痕能验证到"的实质：能验证到**是谁**。
    """
    created = ready_client.post(
        "/api/v1/settings/tokens",
        json={"label": "CI 流水线", "scopes": ["scan"], "expiresAt": None},
    )
    assert created.status_code == 201, created.text
    token_id = created.json()["token"]["id"]
    token = created.json()["plaintext"]
    ready_client.cookies.clear()

    response = ready_client.post(
        "/api/v1/scans",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": "",
            "target": "audit-token.example.com",
            "targetKind": "url",
            "skills": ["web"],
            "providerId": provider_id,
            "authorization": dict(VALID_AUTH),
        },
    )
    assert response.status_code == 201, response.text

    row = _rows(db_session, "scan.create")[0]
    assert row.actor_kind == "token"
    assert "CI 流水线" in row.actor_label
    assert row.actor_id == token_id


def test_rerun_and_abort_are_both_recorded(
    ready_client: TestClient, provider_id: str, db_session: Session
) -> None:
    """重跑与终止各有自己的 action —— 它们都是"对目标动了手"。"""
    scan = _create_scan(ready_client, provider_id)

    rerun = ready_client.post(
        f"/api/v1/scans/{scan['id']}/rerun", json={"authorization": dict(VALID_AUTH)}
    )
    assert rerun.status_code == 201, rerun.text
    new_id = rerun.json()["id"]

    aborted = ready_client.post(f"/api/v1/scans/{new_id}/abort")
    assert aborted.status_code == 200, aborted.text

    assert len(_rows(db_session, "scan.create")) == 1
    rerun_rows = _rows(db_session, "scan.rerun")
    assert len(rerun_rows) == 1
    assert rerun_rows[0].target == new_id
    assert scan["id"] in (rerun_rows[0].detail or {}).get("sourceScanId", "")
    assert len(_rows(db_session, "scan.abort")) == 1


# ══════════════════════════════════════════════════════════
# ② 变更漏洞状态留痕
# ══════════════════════════════════════════════════════════


def test_finding_status_change_is_recorded(
    ready_client: TestClient, db_session: Session
) -> None:
    """要求 2 点名的第二条：谁变更了漏洞状态。"""
    from tests.factories import make_scan

    scan = make_scan(db_session, status="completed")
    from app.core.timeutil import now_utc as _now
    from app.findings.models import Finding

    for index in range(2):
        db_session.add(
            Finding(
                id=f"find_audit_{index}",
                scan_id=scan.id,
                source_ref=f"vuln-audit-{index}",
                severity="high",
                category="misconfiguration",
                title=f"第 {index} 条",
                target=scan.target,
                verification="verified",
                status="new",
                discovered_at=_now(),
            )
        )
    db_session.commit()

    response = ready_client.post(
        "/api/v1/findings/status",
        json={"ids": ["find_audit_0", "find_audit_1"], "status": "confirmed"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["changed"] == 2

    rows = _rows(db_session, "finding.status_change")
    assert len(rows) == 1
    row = rows[0]
    assert "2 条" in row.summary
    assert "已确认" in row.summary
    assert (row.detail or {})["targetStatus"] == "confirmed"
    assert set((row.detail or {})["changedIds"]) == {"find_audit_0", "find_audit_1"}


def test_no_audit_row_when_nothing_actually_changed(
    ready_client: TestClient, db_session: Session
) -> None:
    """★ 幂等重放**不写**审计。

    把已经是 `confirmed` 的再标一次是合法的（返回 `changed: 0`），
    但它不是一次操作。记进去会让审计被"改了 0 条"的噪音淹掉 ——
    而审计的价值恰恰在于每一条都对应一个真实动作。
    """
    from tests.factories import make_scan
    from app.core.timeutil import now_utc as _now
    from app.findings.models import Finding

    scan = make_scan(db_session, status="completed")
    db_session.add(
        Finding(
            id="find_audit_idem",
            scan_id=scan.id,
            source_ref="vuln-idem",
            severity="low",
            category="crypto",
            title="已经是已确认",
            target=scan.target,
            verification="partial",
            status="confirmed",
            discovered_at=_now(),
        )
    )
    db_session.commit()

    response = ready_client.post(
        "/api/v1/findings/status", json={"ids": ["find_audit_idem"], "status": "confirmed"}
    )
    assert response.status_code == 200
    assert response.json()["changed"] == 0
    assert response.json()["skipped"] == 1

    assert _rows(db_session, "finding.status_change") == []


def test_illegal_transition_writes_no_audit_row(
    ready_client: TestClient, db_session: Session
) -> None:
    """被拒的流转（400）不留痕 —— 契约 §3.9 明确只记成功的操作。

    这不是遗漏，是有意的取舍：审计表要留一年，而把每一次非法尝试
    都永久存进去等于给攻击者一个写满磁盘的入口。被拒的请求
    由 access log 覆盖。
    """
    from tests.factories import make_scan
    from app.core.timeutil import now_utc as _now
    from app.findings.models import Finding

    scan = make_scan(db_session, status="completed")
    db_session.add(
        Finding(
            id="find_audit_illegal",
            scan_id=scan.id,
            source_ref="vuln-illegal",
            severity="high",
            category="xss",
            title="已修复",
            target=scan.target,
            verification="verified",
            status="fixed",
            discovered_at=_now(),
        )
    )
    db_session.commit()

    # fixed → false_positive 不在流转表里（必须先退回 confirmed）
    response = ready_client.post(
        "/api/v1/findings/status",
        json={"ids": ["find_audit_illegal"], "status": "false_positive"},
    )
    assert response.status_code == 400
    assert _rows(db_session, "finding.status_change") == []


# ══════════════════════════════════════════════════════════
# ③ 下载 / 删除报告留痕
# ══════════════════════════════════════════════════════════


def _make_report(client: TestClient, db_session: Session) -> dict:
    """造一份 markdown 报告（走真实 API）。"""
    from tests.factories import make_scan
    from app.core.timeutil import now_utc as _now
    from app.findings.models import Finding

    scan = make_scan(db_session, status="completed")
    db_session.add(
        Finding(
            id="find_report_audit",
            scan_id=scan.id,
            source_ref="vuln-report-audit",
            severity="high",
            category="misconfiguration",
            title="用于报告审计的一条",
            target=scan.target,
            verification="verified",
            status="confirmed",
            discovered_at=_now(),
        )
    )
    db_session.commit()

    created = client.post(
        "/api/v1/reports",
        json={
            "title": "审计用报告",
            "scope": {"kind": "scan", "scanId": scan.id},
            "options": {
                "includePoc": False,
                "includeReasoning": False,
                "onlyConfirmed": False,
                "includeRemediation": True,
            },
            "format": "markdown",
        },
    )
    assert created.status_code == 201, created.text
    return created.json()


def test_report_download_is_recorded(
    ready_client: TestClient, db_session: Session
) -> None:
    """★ 要求 2 点名的第三条：谁下载了报告。

    报告一旦被下载就不受控了 —— "谁把哪一份带出去了"必须能回答。
    """
    report = _make_report(ready_client, db_session)

    downloaded = ready_client.get(f"/api/v1/reports/{report['id']}/download")
    assert downloaded.status_code == 200, downloaded.text
    assert "attachment" in downloaded.headers.get("content-disposition", "")

    rows = _rows(db_session, "report.download")
    assert len(rows) == 1
    row = rows[0]
    # 记**编号**（人拿去对账的东西），不是内部 id
    assert row.target == report["code"]
    assert report["code"] in row.summary
    assert (row.detail or {})["bytes"] > 0
    assert (row.detail or {})["format"] == "markdown"


def test_pdf_download_note_is_not_recorded(
    ready_client: TestClient, db_session: Session
) -> None:
    """PDF 那条分支只回一句说明、**不产出文件**，所以不记下载。

    数据没有离开系统就没有后果，记进去只会稀释审计。
    """
    from tests.factories import make_scan
    from app.core.timeutil import now_utc as _now
    from app.findings.models import Finding

    scan = make_scan(db_session, status="completed")
    db_session.add(
        Finding(
            id="find_pdf_audit",
            scan_id=scan.id,
            source_ref="vuln-pdf",
            severity="low",
            category="crypto",
            title="pdf 用",
            target=scan.target,
            verification="partial",
            status="new",
            discovered_at=_now(),
        )
    )
    db_session.commit()
    created = ready_client.post(
        "/api/v1/reports",
        json={
            "title": "PDF 报告",
            "scope": {"kind": "scan", "scanId": scan.id},
            "options": {
                "includePoc": False,
                "includeReasoning": False,
                "onlyConfirmed": False,
                "includeRemediation": False,
            },
            "format": "pdf",
        },
    )
    assert created.status_code == 201, created.text
    report = created.json()

    response = ready_client.get(f"/api/v1/reports/{report['id']}/download")
    assert response.status_code == 200
    assert response.headers.get("x-suvex-note") == "pdf-via-browser-print"

    assert _rows(db_session, "report.download") == []


def test_report_delete_is_recorded(ready_client: TestClient, db_session: Session) -> None:
    """删除报告留痕 —— 它要 `admin`（契约 §3.5 的 v0.10 修正），
    而"谁删的"是审计存在的理由之一。"""
    report = _make_report(ready_client, db_session)

    deleted = ready_client.delete(f"/api/v1/reports/{report['id']}")
    assert deleted.status_code == 204, deleted.text

    rows = _rows(db_session, "report.delete")
    assert len(rows) == 1
    assert rows[0].target == report["code"]


# ══════════════════════════════════════════════════════════
# ④ 只追加 + 访问控制
# ══════════════════════════════════════════════════════════


def test_audit_endpoint_requires_admin(ready_client: TestClient) -> None:
    """审计日志含**来源 IP 与 UA**，不能给 read 级。

    一个只读令牌能看到"某个管理员在什么时候从哪个 IP 做了什么"
    本身就是一处信息泄漏。
    """
    created = ready_client.post(
        "/api/v1/settings/tokens",
        json={"label": "只读", "scopes": ["read"], "expiresAt": None},
    )
    token = created.json()["plaintext"]
    ready_client.cookies.clear()

    response = ready_client.get(
        "/api/v1/audit", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "INSUFFICIENT_SCOPE"


def test_audit_has_no_write_or_delete_http_surface(ready_client: TestClient) -> None:
    """★ 审计**不能**通过 HTTP 写入或删除。

    一个能被外部写入的审计日志，可信度等于零；
    而一个 `DELETE /audit/{id}` 就是一条抹掉某次操作痕迹的捷径。
    所以这个端点只有 GET —— 别的动词必须 405。
    """
    for method in ("post", "put", "patch"):
        response = getattr(ready_client, method)(
            "/api/v1/audit", json={"action": "scan.create"}
        )
        assert response.status_code == 405, (
            f"{method.upper()} /audit 返回了 {response.status_code}，只该允许 GET"
        )
    # DELETE 不带 body —— TestClient 的 delete() 不接受 json 参数。
    response = ready_client.delete("/api/v1/audit")
    assert response.status_code == 405, (
        f"DELETE /audit 返回了 {response.status_code}，只该允许 GET"
    )


def test_audit_module_exposes_no_update_or_delete_entry_point() -> None:
    """服务层也不导出任何"改/删一条"的函数（除了按保留期清理）。

    模型上不放 `updated_at`、`service` 里不放 `update()`/`delete(id)` ——
    有意图层面的一致性，也有这条测试钉住它。
    """
    exported = set(audit_service.__all__)
    assert "purge_expired" in exported
    forbidden = {"update", "delete", "remove", "edit", "drop"}
    assert not (exported & forbidden), f"审计模块不该导出 {exported & forbidden}"


def test_audit_survives_deleting_the_scan_it_references(
    ready_client: TestClient, provider_id: str, db_session: Session
) -> None:
    """★ 审计**不设外键**：被引用的对象删掉之后，记录仍要在。

    一条指向已删除扫描的记录，比一条被级联删掉的记录有用得多 ——
    "这次扫描是谁批的"恰恰在事后追查时才需要，而那时扫描可能早被清了。
    """
    scan = _create_scan(ready_client, provider_id, target="audit-survive.example.com")
    assert len(_rows(db_session, "scan.create")) == 1

    # 直接把扫描行删掉（没有 DELETE /scans 端点，所以走库）
    from app.scans.models import Scan

    row = db_session.get(Scan, scan["id"])
    db_session.delete(row)
    db_session.commit()

    survivors = _rows(db_session, "scan.create")
    assert len(survivors) == 1, "扫描被删之后审计记录必须还在"
    assert survivors[0].target == scan["id"]


# ══════════════════════════════════════════════════════════
# ⑤ 保留策略
# ══════════════════════════════════════════════════════════


def test_retention_is_a_year_and_longer_than_typical_log_rotation() -> None:
    """保留期是**一年**，并且明确写在常量上。

    要求 2 的原话是"不能只靠应用日志（应用日志会被日志轮转清理，
    审计日志需要更长的保留策略）"—— 所以这一条断言的是
    "它确实比日志长"，而不只是"有个数字"。
    """
    assert audit_service.AUDIT_RETENTION_DAYS == 365
    # 日志轮转的常见量级是天/周。365 天比它们高一个数量级。
    assert audit_service.AUDIT_RETENTION_DAYS > 90


def test_purge_removes_only_expired_rows(db_session: Session) -> None:
    """清理只删过期的那批，**留在窗口内的一个都不能少**。"""
    moment = now_utc()
    fresh = AuditLog(
        id="aud_fresh", at=moment - timedelta(days=1), action="scan.create",
        actor_kind="session", actor_label="管理员会话", summary="新的一条",
    )
    old = AuditLog(
        id="aud_old", at=moment - timedelta(days=400), action="scan.create",
        actor_kind="session", actor_label="管理员会话", summary="超过一年的一条",
    )
    edge = AuditLog(
        id="aud_edge", at=moment - timedelta(days=364), action="scan.create",
        actor_kind="session", actor_label="管理员会话", summary="刚好在窗口里",
    )
    db_session.add_all([fresh, old, edge])
    db_session.commit()

    removed = audit_service.purge_expired(db_session, now=moment)

    assert removed == 1
    remaining = {row.id for row in _rows(db_session)}
    assert remaining == {"aud_fresh", "aud_edge"}


# ══════════════════════════════════════════════════════════
# ⑥ 分页：同一毫秒的多条不能漏
# ══════════════════════════════════════════════════════════


def test_cursor_pagination_does_not_skip_same_millisecond_rows(
    db_session: Session,
) -> None:
    """★ 游标翻页在"同一时刻写了很多条"时**不能漏条**。

    这是本模块最容易写错的一处，也是我第一版写错的地方：
    ULID 的前 48 位是毫秒时间戳，所以**毫秒级**是单调的，
    但同一毫秒内多条记录的后 80 位是**随机的**。
    只用 `id < cursor` 会漏掉"同一毫秒里排在游标之后、但 id 更小"的记录。

    批量状态流转与一次请求内写多条审计都会触发这种情况，
    而对一份用于追责的记录来说"漏掉一条"是不可接受的。
    所以游标是 `(at, id)` 元组比较。
    """
    same_moment = now_utc()
    ids = [f"aud_same_{i}" for i in range(6)]
    for index, item_id in enumerate(ids):
        db_session.add(
            AuditLog(
                id=item_id,
                at=same_moment,  # ★ 全部同一时刻
                action="finding.status_change",
                actor_kind="session",
                actor_label="管理员会话",
                summary=f"第 {index} 条",
            )
        )
    db_session.commit()

    seen: list[str] = []
    cursor = None
    for _ in range(10):  # 防御性上限，正常两页就完
        page = audit_service.list_entries(db_session, limit=2, cursor=cursor)
        if not page:
            break
        seen.extend(row.id for row in page)
        if len(page) < 2:
            break
        cursor = audit_service.cursor_for(page[-1])

    assert sorted(seen) == sorted(ids), f"漏条了：拿到 {sorted(seen)}"
    assert len(seen) == len(set(seen)), "同一条被返回了两次"


def test_bad_cursor_does_not_break_the_query(db_session: Session) -> None:
    """形状不对的游标当成"从头开始"，而不是 500。

    一个坏游标不该让审计**查不出来** —— 那正是出事时最不能接受的失败。
    """
    db_session.add(
        AuditLog(
            id="aud_x", at=now_utc(), action="scan.create",
            actor_kind="session", actor_label="管理员会话", summary="随便一条",
        )
    )
    db_session.commit()

    assert len(audit_service.list_entries(db_session, cursor="这不是游标")) == 1
    assert len(audit_service.list_entries(db_session, cursor="|")) == 1


# ══════════════════════════════════════════════════════════
# ⑦ 审计里不许出现凭据
# ══════════════════════════════════════════════════════════


def test_audit_never_stores_credentials(
    ready_client: TestClient, provider_id: str, db_session: Session
) -> None:
    """★ 审计是**永久化**的，所以凭据绝不能进 summary / detail。

    这里断言的是"写进去的东西已经过脱敏"：调用方本来就不该放凭据，
    但 `record()` 里还是过了一遍 `redact()` —— 漏一次就永远漏着。
    """
    from app.core.crypto import redact

    # 直接构造一条"调用方手滑把连接串放进 detail"的记录
    from app.auth.deps import Identity
    from app.audit.service import AuditRequest

    identity = Identity(
        kind="session", label="s", scopes=frozenset({"read", "scan", "admin"}), user_id="user_1"
    )
    audit_service.record(
        db_session,
        request=AuditRequest(identity=identity, ip="127.0.0.1", user_agent="pytest"),
        action="scan.create",
        summary="手滑：postgresql+psycopg://suvex:suvex_dev_local@127.0.0.1:55432/suvex",
        target="scan_x",
        detail={"nested": {"url": "postgresql://u:hunter2@h/db"}, "list": ["sk-live-abcdefghijkl"]},
    )

    row = _rows(db_session)[0]
    blob = f"{row.summary} {row.detail}"
    assert "suvex_dev_local" not in blob, "连接串口令进了审计"
    assert "hunter2" not in blob, "嵌套结构里的口令没被脱敏"
    assert "sk-live-abcdefghijkl" not in blob, "密钥没被脱敏"
    assert "****" in blob


def test_record_failure_does_not_break_the_business_operation(
    db_session: Session, monkeypatch
) -> None:
    """★ 写审计失败**不能**让业务操作失败。

    一次扫描已经真的启动了，这时候因为审计表写不进去就把请求变成 500 ——
    用户会重试，而重试会**再发一次扫描**。那比丢一条审计记录糟得多。

    这条测试用一个"一定炸"的 `commit` 来验证吞异常那条路径。
    """
    from app.auth.deps import Identity
    from app.audit.service import AuditRequest

    class Boom(Exception):
        pass

    def explode() -> None:
        raise Boom("模拟审计写入失败")

    monkeypatch.setattr(db_session, "commit", explode)

    identity = Identity(
        kind="session", label="s", scopes=frozenset({"read"}), user_id="user_1"
    )
    # 不抛异常就是通过
    audit_service.record(
        db_session,
        request=AuditRequest(identity=identity, ip="127.0.0.1", user_agent="pytest"),
        action="scan.create",
        summary="这条写不进去，但不该抛",
        target="scan_y",
    )


def test_every_action_has_a_display_label() -> None:
    """★ 每个 `AuditAction` 都要有中文标签。

    ## 为什么这条要机器判，而不是靠人记得

    漏一个标签**不会报错**：`audit.service.label()` 找不到就回退成原始
    动作串（`ctf2.account_bind`），于是界面/导出里那一行显示一个英文
    标识符，而它看起来"像是正常的"—— 没人会去报一个 bug。

    所以真正会发生的不是崩溃，是**某个动作的说明一直是乱码**，
    直到有人碰巧看见。这正是 v0.20 加 7 个 CTF2 动作时最该有的那道闸。

    ⚠️ 这里**只**比键集合，不比文案。文案的措辞改了不该让测试红
    （与 `verify_ctf2_parity.py` 里"只比键不比文案"是同一条规矩）。
    """
    from app.core.vocab import AUDIT_ACTION_LABELS, AuditAction

    declared = {member.value for member in AuditAction}
    labeled = {str(key) for key in AUDIT_ACTION_LABELS}

    assert declared - labeled == set(), f"这些动作没有标签：{sorted(declared - labeled)}"
    # 反向也要查：表里挂着一个已经不存在的动作时，说明有人删了枚举却没清表，
    # 而那条"幽灵标签"会让下一个人以为那个动作还在用。
    assert labeled - declared == set(), f"标签表里有已不存在的动作：{sorted(labeled - declared)}"

    # 顺带把"回退会显示原始串"这件事钉住：action_label() 必须能查到每一个。
    for member in AuditAction:
        assert audit_service.action_label(member.value) != member.value, (
            f"{member.value} 的标签没生效（回退成了原始动作串）"
        )
