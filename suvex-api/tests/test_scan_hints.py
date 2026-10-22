"""运行中的人机协同：给扫描补线索（P2，v0.57）。

## 这一组验的是什么

人机协同的核心不是「有个输入框」，而是「人补的那句话，真的会在下一轮
进到模型的上下文里」。所以除了 HTTP 层的 201/409/400，还必须验：

  1. 线索按**先后顺序**排队，不会被后来的覆盖；
  2. 引擎取一条就标一条为「已送达」——**同一条不会注入两次**（否则历史被塞满）；
  3. 只允许 `running` 状态的扫描补（queued 该写任务说明，终态没有作用点）。

第 2 条是最容易写错、也最容易被"输入框看起来生效了"蒙混过去的一条 ——
所以它有一个专门的「连取两次只出一次」用例。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.vocab import AuditAction, ScanStatus
from app.scans import service as scan_service
from tests.factories import make_scan


@pytest.fixture()
def running_scan(db_session) -> str:
    scan = make_scan(db_session, target="hint.example", status=ScanStatus.RUNNING.value)
    return scan.id


# ══════════════════════════════════════════════════════════
# HTTP 层
# ══════════════════════════════════════════════════════════


def test_add_hint_to_running_scan_returns_201(ready_client: TestClient, running_scan: str):
    resp = ready_client.post(
        f"/api/v1/scans/{running_scan}/hints", json={"text": "去试试 /admin 后台"}
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["text"] == "去试试 /admin 后台"
    # 刚补的线索还没被引擎取，所以是「待送达」。
    assert body["consumed"] is False
    assert body["consumedTurn"] is None
    assert body["id"].startswith("hint_")


def test_add_hint_records_author(ready_client: TestClient, running_scan: str):
    resp = ready_client.post(
        f"/api/v1/scans/{running_scan}/hints", json={"text": "看 .env"}
    )
    assert resp.status_code == 201, resp.text
    # 作者由服务端从当前身份取，不是客户端传的。
    assert resp.json()["author"] == "admin"


def test_add_hint_to_queued_scan_is_rejected(ready_client: TestClient, db_session):
    scan = make_scan(db_session, status=ScanStatus.QUEUED.value)
    resp = ready_client.post(
        f"/api/v1/scans/{scan.id}/hints", json={"text": "看 .env"}
    )
    assert resp.status_code == 409, resp.text
    # 错误消息必须给出替代动作（写任务说明 / 发新扫描），只说"不行"没有用。
    msg = resp.json()["error"]["message"]
    assert "任务说明" in msg or "新的扫描" in msg


def test_add_hint_to_finished_scan_is_rejected(ready_client: TestClient, db_session):
    scan = make_scan(db_session, status=ScanStatus.COMPLETED.value)
    resp = ready_client.post(
        f"/api/v1/scans/{scan.id}/hints", json={"text": "太晚了"}
    )
    assert resp.status_code == 409, resp.text


def test_empty_hint_is_rejected(ready_client: TestClient, running_scan: str):
    resp = ready_client.post(
        f"/api/v1/scans/{running_scan}/hints", json={"text": "   "}
    )
    assert resp.status_code == 400, resp.text


def test_too_long_hint_is_rejected(ready_client: TestClient, running_scan: str):
    resp = ready_client.post(
        f"/api/v1/scans/{running_scan}/hints", json={"text": "x" * 501}
    )
    # 400 由 schema 的 `max_length=500` 拦下（早于 service 层）。
    assert resp.status_code == 400, resp.text
    msg = resp.json()["error"]["message"]
    assert "长度" in msg or "500" in msg


def test_hint_on_missing_scan_is_404(ready_client: TestClient):
    resp = ready_client.post(
        "/api/v1/scans/scan_does_not_exist/hints", json={"text": "hi"}
    )
    assert resp.status_code == 404, resp.text


def test_unknown_field_is_rejected(ready_client: TestClient, running_scan: str):
    # StrictModel 拒收多余字段 —— 防止客户端以为能传 author 伪造留痕。
    resp = ready_client.post(
        f"/api/v1/scans/{running_scan}/hints",
        json={"text": "hi", "author": "someone_else"},
    )
    assert resp.status_code == 400, resp.text


def test_list_hints_returns_in_order(ready_client: TestClient, running_scan: str):
    for text in ["第一条", "第二条", "第三条"]:
        ready_client.post(f"/api/v1/scans/{running_scan}/hints", json={"text": text})
    resp = ready_client.get(f"/api/v1/scans/{running_scan}/hints")
    assert resp.status_code == 200, resp.text
    texts = [h["text"] for h in resp.json()]
    assert texts == ["第一条", "第二条", "第三条"]


def test_add_hint_writes_audit_record(ready_client: TestClient, running_scan: str, db_session):
    from app.audit.models import AuditLog

    ready_client.post(f"/api/v1/scans/{running_scan}/hints", json={"text": "看 /admin"})
    rows = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == AuditAction.SCAN_HINT.value)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].target == running_scan


def test_viewer_cannot_add_hint(viewer_client: TestClient, db_session):
    scan = make_scan(db_session, status=ScanStatus.RUNNING.value)
    resp = viewer_client.post(
        f"/api/v1/scans/{scan.id}/hints", json={"text": "我是只读用户"}
    )
    # hint 是 scan 级（会改变智能体接下来做什么），只读令牌不能写。
    assert resp.status_code == 403, resp.text


def test_viewer_can_read_hints(viewer_client: TestClient, db_session):
    scan = make_scan(db_session, status=ScanStatus.RUNNING.value)
    resp = viewer_client.get(f"/api/v1/scans/{scan.id}/hints")
    assert resp.status_code == 200, resp.text


# ══════════════════════════════════════════════════════════
# service 层（引擎侧消费语义 —— 最关键的一组）
# ══════════════════════════════════════════════════════════


def test_pending_hint_consumes_once(db_session, running_scan: str):
    """★ 同一个线索只会被取到一次 —— 否则每轮重复注入，历史被塞满。"""
    scan_service.add_hint(db_session, running_scan, "去试 /admin")
    db_session.commit()

    first = scan_service.pending_hint(db_session, running_scan)
    assert first is not None
    assert first.text == "去试 /admin"
    db_session.commit()

    # 第二次取：已经标了 consumed_at，不该再出来。
    second = scan_service.pending_hint(db_session, running_scan)
    assert second is None


def test_pending_hint_returns_in_fifo_order(db_session, running_scan: str):
    scan_service.add_hint(db_session, running_scan, "A")
    scan_service.add_hint(db_session, running_scan, "B")
    db_session.commit()

    got = []
    while True:
        hint = scan_service.pending_hint(db_session, running_scan)
        if hint is None:
            break
        got.append(hint.text)
        db_session.commit()
    assert got == ["A", "B"]


def test_pending_hint_records_turn(db_session, running_scan: str):
    scan_service.add_hint(db_session, running_scan, "第5轮才被读到")
    db_session.commit()
    hint = scan_service.pending_hint(db_session, running_scan, turn=5)
    assert hint is not None
    assert hint.consumed_turn == 5


def test_pending_hint_none_when_queue_empty(db_session, running_scan: str):
    assert scan_service.pending_hint(db_session, running_scan) is None


def test_hints_are_scoped_per_scan(db_session):
    """一条扫描的线索不会被另一条取走（否则 A 的提示会跑到 B 的模型里）。"""
    a = make_scan(db_session, target="a.example", status=ScanStatus.RUNNING.value)
    b = make_scan(db_session, target="b.example", status=ScanStatus.RUNNING.value)
    scan_service.add_hint(db_session, a.id, "给 A 的")
    scan_service.add_hint(db_session, b.id, "给 B 的")
    db_session.commit()

    got_a = scan_service.pending_hint(db_session, a.id)
    assert got_a is not None and got_a.text == "给 A 的"
    db_session.commit()
    got_b = scan_service.pending_hint(db_session, b.id)
    assert got_b is not None and got_b.text == "给 B 的"


def test_add_hint_strips_whitespace(db_session, running_scan: str):
    hint = scan_service.add_hint(db_session, running_scan, "  前后都有空格  ")
    assert hint.text == "前后都有空格"
