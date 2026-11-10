"""Sigma 草案的可执行性（v1.83）。"""

from __future__ import annotations

import yaml

from app.core.ids import new_id
from app.core.timeutil import now_utc
from app.purple.export import build_sigma_draft
from app.purple.models import PurpleRule


def _rule(cwe: str = "CWE-89") -> PurpleRule:
    return PurpleRule(
        id=new_id("rule"),
        name="SQLi",
        severity="high",
        cwe=cwe,
        title_contains=None,
        target_contains=None,
        enabled=True,
        created_at=now_utc(),
    )


def test_sigma_draft_parses_and_has_required_fields() -> None:
    doc = yaml.safe_load(build_sigma_draft(_rule()))

    assert doc["status"] == "experimental"
    assert doc["logsource"]["category"] == "webserver"
    assert doc["detection"]["condition"] == "keywords"
    assert doc["detection"]["keywords"]
    assert doc["level"] == "high"
    assert doc["falsepositives"]


def test_sigma_draft_for_unknown_cwe_still_has_keyword() -> None:
    doc = yaml.safe_load(build_sigma_draft(_rule("CWE-9999")))

    assert doc["detection"]["keywords"]
