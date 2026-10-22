"""SARIF 2.1.0 导出（`app/findings/sarif.py`）。

测试分两层：
- **纯函数层**：`build_sarif` / `sarif_summary` 对 `Finding` 对象的行为，
  不碰 DB、不发 HTTP。
- **契约层**：SARIF 结构的关键不变量（version/schema/runs/rules/results、
  每条 result 有 ruleId/level/message/locations、CWE 有就带没有不编、
  security-severity 是档位中点不是 CVSS）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.vocab import Severity, Verification
from app.findings.models import Finding
from app.findings.sarif import (
    SARIF_SCHEMA,
    SARIF_VERSION,
    build_sarif,
    sarif_summary,
)


def _finding(**overrides) -> Finding:
    """构造一个最小可用 Finding（不落库，只在内存里测序列化）。"""
    base = dict(
        id="fnd_test_1",
        scan_id="scan_test",
        severity=Severity.HIGH.value,
        category="info_disclosure",
        title="未授权访问 Prometheus",
        target="http://192.168.1.121:9090",
        verification=Verification.VERIFIED.value,
        detail_description="Prometheus 的 /api/v1/status/config 未鉴权即可读取。",
        detail_reasoning="直接 GET 返回 200 且带完整抓取配置。",
        detail_evidence="GET /api/v1/status/config -> 200 ...",
        detail_affected=["/api/v1/status/config"],
        remediation="为 Prometheus 增加反向代理鉴权。",
        discovered_at=datetime(2026, 10, 22, tzinfo=UTC),
    )
    base.update(overrides)
    return Finding(**base)


def _parse(rows):
    return json.loads(build_sarif(rows))


# ── 结构不变量 ────────────────────────────────────────────


def test_version_and_schema_are_sarif_2_1_0():
    doc = _parse([_finding()])
    assert doc["version"] == SARIF_VERSION == "2.1.0"
    assert doc["$schema"] == SARIF_SCHEMA
    assert doc["$schema"].endswith("sarif-2.1.0.json")


def test_single_run_with_tool_driver():
    doc = _parse([_finding()])
    assert len(doc["runs"]) == 1
    driver = doc["runs"][0]["tool"]["driver"]
    assert driver["name"] == "Suvex"
    assert "version" in driver
    assert isinstance(driver["rules"], list)


def test_one_result_per_finding():
    rows = [_finding(id="a"), _finding(id="b")]
    doc = _parse(rows)
    assert len(doc["runs"][0]["results"]) == 2


def test_empty_input_yields_valid_empty_run():
    doc = _parse([])
    run = doc["runs"][0]
    assert run["results"] == []
    assert run["tool"]["driver"]["rules"] == []


# ── 每条 result 的必备字段 ─────────────────────────────────


def test_result_has_rule_id_level_message_location():
    doc = _parse([_finding()])
    result = doc["runs"][0]["results"][0]
    assert result["ruleId"] == "suvex.info_disclosure"
    assert result["level"] in {"error", "warning", "note", "none"}
    assert result["message"]["text"]
    loc = result["locations"][0]["physicalLocation"]
    assert loc["artifactLocation"]["uri"] == "http://192.168.1.121:9090"
    # 行/列必须是正数（SARIF 校验要求）
    assert loc["region"]["startLine"] >= 1
    assert loc["region"]["startColumn"] >= 1


def test_message_includes_description_and_reasoning():
    doc = _parse([_finding()])
    text = doc["runs"][0]["results"][0]["message"]["text"]
    assert "未授权访问 Prometheus" in text
    assert "判定依据" in text
    assert "GET 返回 200" in text


def test_logical_locations_carry_affected_paths():
    doc = _parse([_finding(detail_affected=["/a", "/b"])])
    loc = doc["runs"][0]["results"][0]["locations"][0]["physicalLocation"]
    names = {x["name"] for x in loc["logicalLocations"]}
    assert names == {"/a", "/b"}


def test_no_logical_locations_when_affected_empty():
    doc = _parse([_finding(detail_affected=[])])
    loc = doc["runs"][0]["results"][0]["locations"][0]["physicalLocation"]
    assert "logicalLocations" not in loc


# ── 严重度映射 ────────────────────────────────────────────


def test_severity_to_level_mapping():
    cases = {
        Severity.CRITICAL.value: "error",
        Severity.HIGH.value: "error",
        Severity.MEDIUM.value: "warning",
        Severity.LOW.value: "note",
        Severity.INFO.value: "note",
    }
    for sev, level in cases.items():
        doc = _parse([_finding(severity=sev)])
        assert doc["runs"][0]["results"][0]["level"] == level


def test_security_severity_is_band_midpoint_not_cvss():
    # GitHub Code Scanning 用 security-severity 排序；它是档位中点，
    # 不是 CVSS。断言它在 0–10 内且随严重度单调下降。
    scores = {}
    for sev in (Severity.CRITICAL.value, Severity.HIGH.value, Severity.MEDIUM.value, Severity.LOW.value):
        doc = _parse([_finding(severity=sev)])
        scores[sev] = float(doc["runs"][0]["results"][0]["properties"]["security-severity"])
    assert 0.0 <= scores[Severity.CRITICAL.value] <= 10.0
    assert scores[Severity.CRITICAL.value] >= scores[Severity.HIGH.value] >= scores[Severity.MEDIUM.value] >= scores[Severity.LOW.value]


def test_unknown_severity_falls_back_to_note():
    doc = _parse([_finding(severity="banana")])
    assert doc["runs"][0]["results"][0]["level"] == "note"


# ── CWE / CVSS 不编造 ─────────────────────────────────────


def test_cwe_present_becomes_tag():
    doc = _parse([_finding(cwe="CWE-200")])
    props = doc["runs"][0]["results"][0]["properties"]
    assert "CWE-200" in props["tags"]


def test_no_cwe_no_tags():
    doc = _parse([_finding(cwe=None)])
    props = doc["runs"][0]["results"][0]["properties"]
    assert "tags" not in props


def test_no_cvss_no_cvss_score():
    doc = _parse([_finding(cvss_score=None)])
    props = doc["runs"][0]["results"][0]["properties"]
    assert "cvssScore" not in props


def test_real_cvss_is_carried_through():
    doc = _parse([_finding(cvss_score=7.5)])
    props = doc["runs"][0]["results"][0]["properties"]
    assert props["cvssScore"] == 7.5


# ── 规则聚合 ──────────────────────────────────────────────


def test_rules_deduplicated_by_category():
    rows = [
        _finding(id="a", category="injection"),
        _finding(id="b", category="injection"),
        _finding(id="c", category="auth"),
    ]
    doc = _parse(rows)
    rule_ids = [r["id"] for r in doc["runs"][0]["tool"]["driver"]["rules"]]
    assert sorted(rule_ids) == ["suvex.auth", "suvex.injection"]


def test_unknown_category_uses_generic_rule():
    doc = _parse([_finding(category="weird-thing")])
    result, = doc["runs"][0]["results"]
    assert result["ruleId"] == "suvex.finding"
    assert doc["runs"][0]["tool"]["driver"]["rules"][0]["id"] == "suvex.finding"


def test_every_result_rule_is_declared_in_rules():
    rows = [_finding(id="a", category="injection"), _finding(id="b", category="auth"), _finding(id="c", category="zzz")]
    doc = _parse(rows)
    declared = {r["id"] for r in doc["runs"][0]["tool"]["driver"]["rules"]}
    used = {r["ruleId"] for r in doc["runs"][0]["results"]}
    assert used <= declared


# ── sarif_summary ─────────────────────────────────────────


def test_summary_counts_by_severity():
    rows = [
        _finding(severity=Severity.HIGH.value),
        _finding(severity=Severity.HIGH.value),
        _finding(severity=Severity.LOW.value),
    ]
    counts = sarif_summary(rows)
    assert counts["high"] == 2
    assert counts["low"] == 1
    assert counts["critical"] == 0


# ══════════════════════════════════════════════════════════
# HTTP 契约层：端点真的挂在 /findings/export/sarif 上，
# 且没被 /findings/{findingId} 抢先匹配掉。
# ══════════════════════════════════════════════════════════

API = "/api/v1"


def _seed_finding(db: Session, scan) -> str:
    import uuid

    row = Finding(
        id=f"finding_{uuid.uuid4().hex[:12]}",
        scan_id=scan.id,
        severity="high",
        category="info_disclosure",
        title="未授权访问 Prometheus",
        target="http://192.168.1.121:9090",
        verification="verified",
        detail_description="未鉴权即可读取抓取配置。",
        detail_affected=["/api/v1/status/config"],
        discovered_at=datetime(2026, 10, 22, tzinfo=UTC),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row.id


def test_http_export_sarif_returns_valid_sarif(ready_client, db_session, scan):
    _seed_finding(db_session, scan)
    resp = ready_client.get(f"{API}/findings/export/sarif")
    assert resp.status_code == 200, resp.text
    assert "application/sarif+json" in resp.headers["content-type"]
    doc = resp.json()
    assert doc["version"] == "2.1.0"
    results = doc["runs"][0]["results"]
    assert len(results) == 1
    assert results[0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == "http://192.168.1.121:9090"


def test_http_export_sarif_not_shadowed_by_finding_id_route(ready_client, db_session, scan):
    """★ 关键：`export/sarif` 不能被 `/findings/{findingId}` 当成 id 吃掉。

    若被吃掉，`findingId="export"` 会走详情路由并返回 404「找不到漏洞
    export」—— 症状是导出接口 404 而不是 200。这里断言 200 + SARIF 结构。
    """
    _seed_finding(db_session, scan)
    resp = ready_client.get(f"{API}/findings/export/sarif")
    assert resp.status_code == 200
    assert "runs" in resp.json()


def test_http_export_sarif_honors_severity_filter(ready_client, db_session, scan):
    """筛选参数与 /findings 对齐：筛 high 时 low 的发现不该出现。"""
    import uuid

    db_session.add_all([
        Finding(
            id=f"finding_{uuid.uuid4().hex[:12]}",
            scan_id=scan.id, severity="high", category="info_disclosure",
            title="high 问题", target="t1", verification="verified",
            discovered_at=datetime(2026, 10, 22, tzinfo=UTC),
        ),
        Finding(
            id=f"finding_{uuid.uuid4().hex[:12]}",
            scan_id=scan.id, severity="low", category="info_disclosure",
            title="low 问题", target="t2", verification="verified",
            discovered_at=datetime(2026, 10, 22, tzinfo=UTC),
        ),
    ])
    db_session.commit()

    resp = ready_client.get(f"{API}/findings/export/sarif", params={"severity": "high"})
    assert resp.status_code == 200
    results = resp.json()["runs"][0]["results"]
    assert len(results) == 1
    assert results[0]["properties"]["suvexSeverity"] == "high"
