"""手选漏洞出报告（任务 10 P5）。

打通的是"漏洞发现"与"报告"两个模块：用户在漏洞页勾了 5 条，点
「生成报告」直接带着它们跳到报告页，而不是去报告页重新勾一遍。

四类要覆盖的：

1. 生成 + 预览能按 id 挑出那几条。
2. **复现** —— 生成后的报告再取文档时，成员要从 `report_findings`
   读回来，而不是重新按 scope 查一次。
3. union 校验 —— 同时给 `findingIds` 和 `scanId` 要报错。
4. 报告自己的排序规则优先于"勾选顺序"。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.vocab import SEVERITY_RANK
from app.findings.models import Finding
from app.reports import service

API = "/api/v1"


def _preview(client: TestClient, finding_ids: list[str]) -> dict:
    response = client.post(
        f"{API}/reports/preview",
        json={
            "title": "手选报告",
            "scope": {"kind": "findings", "findingIds": finding_ids},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_preview_with_selected_findings(
    ready_client: TestClient, imported: list[Finding]
) -> None:
    """按 id 挑出那几条 —— 只要两条，第三条不该出现。"""
    picked = [imported[0].id, imported[1].id]
    body = _preview(ready_client, picked)

    assert body["stats"]["total"] == 2
    titles = {entry.get("title") for entry in body["entries"]}
    assert imported[2].title not in titles, "没选中的那条不该进报告"


def test_generate_persists_and_replays_from_the_join_table(
    ready_client: TestClient, imported: list[Finding]
) -> None:
    """★ 生成后再取文档，成员从**关联表**读回来。

    `reports` 表上没有存 id 列表的列（塞不进任何一列），成员在
    `report_findings` 里 —— 而那本来就是"这份报告冻结了哪些漏洞"。
    所以复现靠的是它，不是重新按 scope 查一次。
    """
    picked = [imported[0].id, imported[1].id]
    created = ready_client.post(
        f"{API}/reports",
        json={
            "title": "冻结测试",
            "scope": {"kind": "findings", "findingIds": picked},
            "format": "markdown",
        },
    )
    assert created.status_code == 201, created.text
    report = created.json()
    assert report["findingCount"] == 2

    # scope 回读时带上了 id 列表（前端要能显示"这份报告冻结了哪几条"）
    assert report["scope"]["kind"] == "findings"
    assert sorted(report["scope"]["findingIds"]) == sorted(picked)

    # 复现：再取一次文档，内容一致
    again = ready_client.get(f"{API}/reports/{report['id']}/document")
    assert again.status_code == 200, again.text
    assert again.json()["stats"]["total"] == 2


def test_scope_rejects_mixing_findings_with_scan(
    ready_client: TestClient, imported: list[Finding]
) -> None:
    """union 语义：不能同时给两种范围。

    这条守的是"到底按哪个过滤"必须有唯一答案 —— 同时给两种的话，
    只能靠读实现猜，而猜错的表现是报告里出现了没选中的漏洞。
    """
    response = ready_client.post(
        f"{API}/reports/preview",
        json={
            "title": "非法组合",
            "scope": {
                "kind": "findings",
                "findingIds": [imported[0].id],
                "scanId": imported[0].scan_id,
            },
        },
    )
    assert response.status_code == 400, response.text


def test_findings_scope_requires_ids(
    ready_client: TestClient, imported: list[Finding]
) -> None:
    """`kind="findings"` 但没给 id → 400，而不是静默出一份空报告。"""
    response = ready_client.post(
        f"{API}/reports/preview",
        json={"title": "空", "scope": {"kind": "findings", "findingIds": []}},
    )
    assert response.status_code == 400, response.text


def test_domain_scope_round_trip(imported: list[Finding]) -> None:
    """`Scope` 的构造与 `to_api` 对称（不经过 HTTP）。"""
    picked = (imported[0].id, imported[1].id)
    scope = service.Scope(kind="findings", finding_ids=picked)

    api = scope.to_api()
    assert api["kind"] == "findings"
    assert sorted(api["findingIds"]) == sorted(picked)

    # 与另外三种范围互斥
    import pytest

    with pytest.raises(ValueError, match="只能给出一种范围"):
        service.Scope(kind="scan", scan_id="s", finding_ids=picked)


def test_report_orders_by_severity_not_by_pick_order(
    ready_client: TestClient, imported: list[Finding]
) -> None:
    """报告有自己的排序（严重度降序），**不保留勾选顺序**。

    给管理层看的文档里，顺序应该反映严重程度而不是勾选动作 ——
    用户勾选时的心血来潮不该决定读者先看到什么。
    """
    # 故意反着传：低危在前、严重在后
    low_first = [imported[2].id, imported[0].id]
    body = _preview(ready_client, low_first)

    severities = [entry.get("severity") for entry in body["entries"]]
    assert severities == sorted(
        severities, key=lambda s: -SEVERITY_RANK.get(s, 0)
    ), f"应按严重度降序，实际 {severities}"
