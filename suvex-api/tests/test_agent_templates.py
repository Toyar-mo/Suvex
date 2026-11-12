"""角色模板（v1.83）。"""

from __future__ import annotations

from app.agents import service


def test_role_templates_are_complete() -> None:
    rows = service.list_templates()

    assert len(rows) == 10
    keys = {row["key"] for row in rows}
    assert {"goals", "planner", "mainagent", "worker", "auto", "pentest", "reporter", "retester", "verifier"} <= keys
    assert all(row["name"] and row["description"] for row in rows)
