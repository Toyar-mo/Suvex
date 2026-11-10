"""`verify_http` 对话工具的测试（v1.81）。

验证器本身在 `tests/scans/test_validators.py` 里单独测；这里只钉**工具层**：
参数形状、返回结构、注册表。
"""

from __future__ import annotations

import json

import pytest

from app.chat import engine
from app.chat.prompts import CHAT_TOOL_NAMES
from app.scans import validators


@pytest.mark.asyncio
async def test_replay_returns_structured_evidence(monkeypatch) -> None:
    calls: dict[str, object] = {}

    def fake_replay(spec, *, attempts, markers, client_factory):  # noqa: ANN001
        calls.update(
            spec=spec, attempts=attempts, markers=markers, factory=client_factory
        )
        return validators.ValidationOutcome(
            kind="replay",
            passed=True,
            confidence="high",
            summary="2/2 次重放响应一致",
            evidence="status=200 length=3",
            attempts=2,
        )

    monkeypatch.setattr(validators, "replay", fake_replay)

    text = await engine._tool_verify_http(
        None,  # type: ignore[arg-type]
        {
            "kind": "replay",
            "url": "https://example.test/item",
            "method": "post",
            "headers": {"X-Test": "1"},
            "body": "id=1",
            "markers": ["ok"],
            "attempts": 3,
        },
    )

    payload = json.loads(text)
    assert payload["passed"] is True
    assert payload["kind"] == "replay"
    assert payload["confidence"] == "high"

    spec = calls["spec"]
    assert spec.method == "POST"
    assert spec.url == "https://example.test/item"
    assert spec.headers == {"X-Test": "1"}
    assert spec.body == b"id=1"
    assert calls["attempts"] == 3
    assert calls["markers"] == ("ok",)


@pytest.mark.asyncio
async def test_differential_passes_control_and_experiment(monkeypatch) -> None:
    calls: dict[str, object] = {}

    def fake_differential(control, experiment, *, markers, client_factory):  # noqa: ANN001
        calls.update(
            control=control, experiment=experiment, markers=markers, factory=client_factory
        )
        return validators.ValidationOutcome(
            kind="differential",
            passed=True,
            confidence="high",
            summary="状态码 200 → 403",
            evidence="control: ...\nexperiment: ...",
            attempts=2,
        )

    monkeypatch.setattr(validators, "differential", fake_differential)

    text = await engine._tool_verify_http(
        None,  # type: ignore[arg-type]
        {
            "kind": "differential",
            "control": {"url": "https://example.test/a", "body": "user"},
            "experiment": {"url": "https://example.test/a", "body": "admin"},
            "markers": ["root:x"],
        },
    )

    assert json.loads(text)["passed"] is True
    assert calls["control"].body == b"user"
    assert calls["experiment"].body == b"admin"
    assert calls["markers"] == ("root:x",)


@pytest.mark.asyncio
async def test_missing_url_is_a_clear_tool_error() -> None:
    text = await engine._tool_verify_http(
        None,  # type: ignore[arg-type]
        {"kind": "replay"},
    )
    assert text.startswith("失败：")


@pytest.mark.asyncio
async def test_unknown_kind_is_rejected() -> None:
    text = await engine._tool_verify_http(
        None,  # type: ignore[arg-type]
        {"kind": "exploit", "url": "https://example.test/"},
    )
    assert text.startswith("失败：")


def test_tool_is_registered_and_advertised() -> None:
    assert "verify_http" in engine._TOOL_IMPLS
    assert "verify_http" in CHAT_TOOL_NAMES
