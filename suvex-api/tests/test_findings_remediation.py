"""修复方案生成器的测试（v1.81）。"""

from __future__ import annotations

from types import SimpleNamespace

from app.findings.remediation import plan


def _finding(**overrides) -> SimpleNamespace:
    values = {
        "id": "finding_1",
        "cwe": "CWE-89",
        "category": "injection",
        "verification": "verified",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_known_cwe_with_l2_is_ready() -> None:
    out = plan(_finding())

    assert out["readiness"] == "ready"
    assert out["findingId"] == "finding_1"
    assert out["cwe"] == "CWE-89"
    assert any("参数化" in step for step in out["steps"])
    assert out["verification"]


def test_unverified_finding_asks_for_verification_first() -> None:
    out = plan(_finding(verification="partial"))

    assert out["readiness"] == "needs_verification"
    assert out["steps"][0].startswith("这条目前不是 L2")
    assert "先补验证" in out["note"]


def test_unknown_cwe_falls_back_to_generic_plan() -> None:
    out = plan(_finding(cwe="CWE-9999"))

    assert out["title"].startswith("通用修复")
    assert len(out["steps"]) >= 4


def test_missing_fields_do_not_raise() -> None:
    out = plan(SimpleNamespace())

    assert out["findingId"] == ""
    assert out["readiness"] == "needs_verification"
    assert out["title"].startswith("通用修复")


def test_cwe_is_normalized() -> None:
    out = plan(_finding(cwe=" cwe-79 "))

    assert out["cwe"] == "CWE-79"
    assert "XSS" in out["title"]
