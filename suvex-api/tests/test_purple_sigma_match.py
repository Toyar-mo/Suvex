"""Sigma 草案的关键词对样本可命中（v1.83）。"""

from __future__ import annotations

import yaml

from app.core.ids import new_id
from app.core.timeutil import now_utc
from app.purple.export import build_sigma_draft
from app.purple.models import PurpleRule


def _keywords(cwe: str) -> list[str]:
    rule = PurpleRule(
        id=new_id("rule"),
        name="r",
        severity="high",
        cwe=cwe,
        title_contains=None,
        target_contains=None,
        enabled=True,
        created_at=now_utc(),
    )
    doc = yaml.safe_load(build_sigma_draft(rule))
    return [str(item) for item in doc["detection"]["keywords"]]


def test_sqli_keywords_fire_on_malicious_log() -> None:
    keywords = _keywords("CWE-89")
    event = "GET /login?id=1 UNION SELECT password FROM users"

    assert any(keyword.lower() in event.lower() for keyword in keywords)


def test_sqli_keywords_stay_quiet_on_normal_log() -> None:
    keywords = _keywords("CWE-89")
    event = "GET /products?page=2 HTTP/1.1 200"

    assert not any(keyword.lower() in event.lower() for keyword in keywords)
