"""落 finding 前的反证清单（v1.82）。"""

from __future__ import annotations

from types import SimpleNamespace

from app.findings.remediation import plan
from app.findings.verifier import checks_for, classify


def test_classifies_by_cwe_and_category() -> None:
    assert classify("CWE-89", "") == "sqli"
    assert classify("", "xss") == "xss"
    assert classify("CWE-9999", "injection") == "sqli"
    assert classify("CWE-9999", "unknown") == "generic"


def test_checks_include_general_and_specific() -> None:
    checks = checks_for("CWE-89", "injection")

    assert checks["class"] == "sqli"
    assert len(checks["general"]) == 3
    assert any("布尔" in item or "盲注" in item for item in checks["specific"])


def test_unknown_class_has_no_specific_checks() -> None:
    checks = checks_for("CWE-9999", "unknown")

    assert checks["class"] == "generic"
    assert checks["specific"] == []


def test_remediation_plan_carries_verifier_checks() -> None:
    out = plan(SimpleNamespace(id="f1", cwe="CWE-79", category="xss", verification="verified"))

    assert out["verifierChecks"]["class"] == "xss"
    assert out["verifierChecks"]["specific"]
