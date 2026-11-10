"""finding 入库前的验证器桥接（v1.81）。

只测纯函数：`_verify_item` 的调度与降级、`_level_with_verification` 的升降级。
真正的 HTTP 判据在 `tests/scans/test_validators.py`。
"""

from __future__ import annotations

from app.scans import agent_runner, validators


def _outcome(passed: bool) -> validators.ValidationOutcome:
    return validators.ValidationOutcome(
        kind="replay",
        passed=passed,
        confidence="high" if passed else "low",
        summary="ok" if passed else "unstable",
        evidence="status=200",
        attempts=2,
    )


def test_level_promotes_to_l2_when_verifier_passes() -> None:
    assert agent_runner._level_with_verification("L0", _outcome(True)) == "L2"
    assert agent_runner._level_with_verification("L1", _outcome(True)) == "L2"


def test_level_caps_at_l1_when_verifier_fails() -> None:
    assert agent_runner._level_with_verification("L2", _outcome(False)) == "L1"
    assert agent_runner._level_with_verification("L1", _outcome(False)) == "L1"


def test_level_is_unchanged_without_outcome() -> None:
    assert agent_runner._level_with_verification("L1", None) == "L1"


def test_verify_item_is_none_without_spec() -> None:
    assert agent_runner._verify_item({"title": "x"}) is None
    assert agent_runner._verify_item({"verify": "oops"}) is None


def test_verify_item_runs_replay_with_structured_spec(monkeypatch) -> None:
    calls: dict[str, object] = {}

    def fake_replay(spec, *, attempts, markers, client_factory):  # noqa: ANN001
        calls.update(spec=spec, attempts=attempts, markers=markers)
        return _outcome(True)

    monkeypatch.setattr(validators, "replay", fake_replay)

    outcome = agent_runner._verify_item(
        {
            "verify": {
                "kind": "replay",
                "url": "https://example.test/x",
                "method": "post",
                "headers": {"X-Test": "1"},
                "body": "id=1",
                "markers": ["root:x"],
                "attempts": 3,
            }
        }
    )

    assert outcome is not None and outcome.passed is True
    spec = calls["spec"]
    assert spec.method == "POST"
    assert spec.body == b"id=1"
    assert calls["attempts"] == 3
    assert calls["markers"] == ("root:x",)


def test_verify_item_runs_differential(monkeypatch) -> None:
    calls: dict[str, object] = {}

    def fake_differential(control, experiment, *, markers, client_factory):  # noqa: ANN001
        calls.update(control=control, experiment=experiment)
        return _outcome(True)

    monkeypatch.setattr(validators, "differential", fake_differential)

    outcome = agent_runner._verify_item(
        {
            "verify": {
                "kind": "differential",
                "control": {"url": "https://example.test/x", "body": "user"},
                "experiment": {"url": "https://example.test/x", "body": "admin"},
            }
        }
    )

    assert outcome is not None and outcome.passed is True
    assert calls["control"].body == b"user"
    assert calls["experiment"].body == b"admin"


def test_verify_item_swallows_verifier_errors(monkeypatch) -> None:
    def boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("network down")

    monkeypatch.setattr(validators, "replay", boom)

    assert agent_runner._verify_item(
        {"verify": {"kind": "replay", "url": "https://example.test/x"}}
    ) is None
