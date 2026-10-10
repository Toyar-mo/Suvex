"""处理人名单与漏洞指派（任务 10 P2）。

覆盖四类：

1. **名单 CRUD 的边界** —— 重名、空名、改名、停用。
2. **指派本身** —— 批量、幂等、取消指派、不存在的处理人。
3. **留痕** —— 审计一条、时间线**逐条**（这是需求明确要求的粒度）。
4. **筛选** —— `assigneeId` 与 `unassigned` 是两种不同的查询。
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.models import AuditLog
from app.findings.models import Finding, FindingTimelineEntry

API = "/api/v1"


def _make_member(client: TestClient, name: str, email: str | None = None) -> str:
    response = client.post(f"{API}/members", json={"name": name, "email": email})
    assert response.status_code == 201, response.text
    return response.json()["id"]


# ══════════════════════════════════════════════════════════
# 名单本身
# ══════════════════════════════════════════════════════════


def test_member_crud_round_trip(ready_client: TestClient) -> None:
    member_id = _make_member(ready_client, "张三", "zhangsan@example.com")

    listed = ready_client.get(f"{API}/members").json()
    assert [m["name"] for m in listed] == ["张三"]
    assert listed[0]["active"] is True
    assert listed[0]["email"] == "zhangsan@example.com"

    # 改名
    patched = ready_client.patch(f"{API}/members/{member_id}", json={"name": "张三丰"})
    assert patched.status_code == 200
    assert patched.json()["name"] == "张三丰"

    # 停用（而不是删除）
    off = ready_client.patch(f"{API}/members/{member_id}", json={"active": False})
    assert off.json()["active"] is False

    # 默认仍列出来 —— 历史指派要能显示名字
    assert len(ready_client.get(f"{API}/members").json()) == 1
    # 只要候选时才过滤掉
    assert ready_client.get(f"{API}/members?includeInactive=false").json() == []


def test_duplicate_member_name_is_rejected(ready_client: TestClient) -> None:
    """重名会让「按负责人筛选」被拆成两组，所以必须挡。"""
    _make_member(ready_client, "张三")

    response = ready_client.post(f"{API}/members", json={"name": "张三"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"
    # 错误里要说清是重名，而不是抛一句英文的约束冲突
    assert "张三" in response.json()["error"]["message"]


def test_blank_member_name_is_rejected(ready_client: TestClient) -> None:
    """只有空格的名字不是名字。"""
    response = ready_client.post(f"{API}/members", json={"name": "   "})
    assert response.status_code == 400


def test_renaming_onto_an_existing_name_is_rejected(ready_client: TestClient) -> None:
    _make_member(ready_client, "张三")
    other = _make_member(ready_client, "李四")

    response = ready_client.patch(f"{API}/members/{other}", json={"name": "张三"})
    assert response.status_code == 400


def test_renaming_to_the_same_name_is_allowed(ready_client: TestClient) -> None:
    """改成自己现在的名字不算冲突 —— 否则"只改邮箱"就做不到了。"""
    member_id = _make_member(ready_client, "张三")

    response = ready_client.patch(
        f"{API}/members/{member_id}", json={"name": "张三", "email": "new@example.com"}
    )
    assert response.status_code == 200
    assert response.json()["email"] == "new@example.com"


def test_member_operations_are_audited(
    ready_client: TestClient, db_session: Session
) -> None:
    """名单决定"漏洞归谁"，所以"谁把人加进来的"必须查得到。"""
    member_id = _make_member(ready_client, "张三")
    ready_client.patch(f"{API}/members/{member_id}", json={"active": False})
    ready_client.delete(f"{API}/members/{member_id}")

    actions = list(
        db_session.execute(
            select(AuditLog.action).where(AuditLog.action.like("member.%"))
        ).scalars()
    )
    assert "member.create" in actions
    assert "member.update" in actions
    assert "member.delete" in actions


# ══════════════════════════════════════════════════════════
# 指派
# ══════════════════════════════════════════════════════════


def _first_finding(client: TestClient) -> dict:
    listed = client.get(f"{API}/findings").json()
    assert listed, "前置数据没准备好"
    return listed[0]


def test_assign_and_unassign(
    ready_client: TestClient, imported: list[Finding], db_session: Session
) -> None:
    member_id = _make_member(ready_client, "张三")
    ids = [f.id for f in imported[:2]]

    response = ready_client.post(f"{API}/findings/assign", json={"ids": ids, "assigneeId": member_id})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["changed"] == 2
    assert body["skipped"] == 0
    # ★ 响应里直接带名字 —— 前端不必再查一次名单
    assert body["findings"][0]["assigneeName"] == "张三"
    assert body["findings"][0]["assigneeId"] == member_id

    # 列表里也要带上（这是列表页那一列的数据来源）
    listed = {row["id"]: row for row in ready_client.get(f"{API}/findings").json()}
    assert listed[ids[0]]["assigneeName"] == "张三"

    # 取消指派：`assigneeId: null` 是**明确的操作**，不是"没传"
    cleared = ready_client.post(
        f"{API}/findings/assign", json={"ids": ids, "assigneeId": None}
    ).json()
    assert cleared["changed"] == 2
    assert cleared["findings"][0]["assigneeName"] is None
    assert cleared["findings"][0]["assigneeId"] is None


def test_assign_is_idempotent_and_writes_no_timeline(
    ready_client: TestClient, imported: list[Finding], db_session: Session
) -> None:
    """★ 重复指派同一个人：算跳过，且**不写时间线**。

    不写是因为否则时间线里会出现「指派给张三 → 指派给张三」的噪音，
    而时间线是给人读的历史。
    """
    member_id = _make_member(ready_client, "张三")
    ids = [imported[0].id]

    assert (
        ready_client.post(
            f"{API}/findings/assign", json={"ids": ids, "assigneeId": member_id}
        ).json()["changed"]
        == 1
    )

    db_session.expire_all()
    before = len(
        list(
            db_session.execute(
                select(FindingTimelineEntry).where(FindingTimelineEntry.finding_id == ids[0])
            ).scalars()
        )
    )

    again = ready_client.post(
        f"{API}/findings/assign", json={"ids": ids, "assigneeId": member_id}
    ).json()
    assert again["changed"] == 0
    assert again["skipped"] == 1

    db_session.expire_all()
    after = len(
        list(
            db_session.execute(
                select(FindingTimelineEntry).where(FindingTimelineEntry.finding_id == ids[0])
            ).scalars()
        )
    )
    assert after == before, "幂等跳过不该写时间线"


def test_assign_writes_one_timeline_entry_per_finding(
    ready_client: TestClient, imported: list[Finding], db_session: Session
) -> None:
    """★ 逐条留痕 —— 需求明确要求「不能只存批量操作这一条粗粒度日志」。

    批量改 3 条，时间线就该有 3 行，各带**那一条自己的**历史。
    """
    member_id = _make_member(ready_client, "张三")
    ids = [f.id for f in imported[:3]]

    ready_client.post(f"{API}/findings/assign", json={"ids": ids, "assigneeId": member_id})

    db_session.expire_all()
    for finding_id in ids:
        rows = list(
            db_session.execute(
                select(FindingTimelineEntry).where(
                    FindingTimelineEntry.finding_id == finding_id,
                    FindingTimelineEntry.kind == "assigned",
                )
            ).scalars()
        )
        assert len(rows) == 1, f"{finding_id} 没有指派记录"
        assert "张三" in rows[0].summary


def test_assign_records_a_single_audit_row(
    ready_client: TestClient, imported: list[Finding], db_session: Session
) -> None:
    """审计**每次操作一行**（不是逐条）—— 与 timeline 的分工。

    逐条写审计会让一次 500 条的批量产生 500 行，把真正的操作序列淹掉。
    逐条留痕由 timeline 负责，两者不是替代关系。
    """
    member_id = _make_member(ready_client, "张三")
    ids = [f.id for f in imported[:3]]

    ready_client.post(f"{API}/findings/assign", json={"ids": ids, "assigneeId": member_id})

    db_session.expire_all()
    rows = list(
        db_session.execute(
            select(AuditLog).where(AuditLog.action == "finding.assign")
        ).scalars()
    )
    assert len(rows) == 1
    assert len(rows[0].detail["changedIds"]) == 3


def test_assign_to_unknown_member_is_rejected(
    ready_client: TestClient, imported: list[Finding]
) -> None:
    """指向不存在的处理人会 404，而不是静默写入一个悬空 id ——
    那样界面上那一列永远是空的，而没人知道为什么。"""
    response = ready_client.post(
        f"{API}/findings/assign",
        json={"ids": [imported[0].id], "assigneeId": "member_does_not_exist"},
    )
    assert response.status_code == 404


def test_assign_reports_missing_finding_ids(
    ready_client: TestClient, imported: list[Finding]
) -> None:
    member_id = _make_member(ready_client, "张三")

    response = ready_client.post(
        f"{API}/findings/assign",
        json={"ids": [imported[0].id, "finding_nope"], "assigneeId": member_id},
    )
    assert response.status_code == 404
    # 一次性说清缺几条，而不是改到第一个不存在的就报错 ——
    # 批量提交几十条时逐个排查是不可用的
    assert "1 条" in response.json()["error"]["message"]


def test_deleting_a_member_unassigns_but_keeps_the_findings(
    ready_client: TestClient, imported: list[Finding], db_session: Session
) -> None:
    """删人**不删漏洞** —— 那些漏洞只是变成"未指派"。

    ★ 而且接口要**把这个条数返回**（任务 10 P2 实测后从 204 改的）：
    删完之后那几条的负责人是无声消失的，页面上没有任何提示 ——
    用户看到的只是列表里少了一个人。返回数字，界面才能说清代价。
    """
    member_id = _make_member(ready_client, "张三")
    ids = [f.id for f in imported[:2]]
    ready_client.post(f"{API}/findings/assign", json={"ids": ids, "assigneeId": member_id})

    response = ready_client.delete(f"{API}/members/{member_id}")
    assert response.status_code == 200, response.text
    assert response.json()["unassigned"] == 2, "要说清有几条漏洞被解除指派"

    db_session.expire_all()
    for finding_id in ids:
        finding = db_session.get(Finding, finding_id)
        assert finding is not None, "漏洞不该跟着人一起消失"
        assert finding.assignee_id is None


# ══════════════════════════════════════════════════════════
# 筛选
# ══════════════════════════════════════════════════════════


def test_filter_by_assignee_and_unassigned_are_different_queries(
    ready_client: TestClient, imported: list[Finding]
) -> None:
    """★ `assigneeId=<id>` 和 `unassigned=true` 是两种查询，不能互相代替。

    用 `assigneeId=""` 表达"未指派"会让空串成为一个特殊值，
    而数据库里存的是 NULL。
    """
    member_id = _make_member(ready_client, "张三")
    ready_client.post(
        f"{API}/findings/assign",
        json={"ids": [imported[0].id], "assigneeId": member_id},
    )

    mine = ready_client.get(f"{API}/findings?assigneeId={member_id}").json()
    assert [row["id"] for row in mine] == [imported[0].id]

    unassigned = ready_client.get(f"{API}/findings?unassigned=true").json()
    assert len(unassigned) == len(imported) - 1
    assert imported[0].id not in {row["id"] for row in unassigned}
