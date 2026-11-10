"""可用模型列表解析（v1.83）。"""

from __future__ import annotations

from app.providers.llm import parse_models


def test_parse_openai_style_models() -> None:
    payload = {"data": [{"id": "gpt-4o"}, {"id": "gpt-4o-mini"}]}

    assert parse_models(payload) == ["gpt-4o", "gpt-4o-mini"]


def test_parse_dedupes_and_ignores_bad_entries() -> None:
    payload = {"data": [{"id": "a"}, {"id": "a"}, {"id": ""}, {"x": 1}, "oops"]}

    assert parse_models(payload) == ["a"]


def test_parse_returns_empty_for_unknown_shape() -> None:
    assert parse_models({}) == []
    assert parse_models({"models": ["a"]}) == []
