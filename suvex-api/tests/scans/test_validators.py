"""可重放验证器的单元测试（v1.81）。

全部走 `httpx.MockTransport`，不碰网络。验证的是**判据**而不是 HTTP 细节：
重放稳定性、差分硬信号、请求原样透传。
"""

from __future__ import annotations

import httpx
import pytest

from app.scans import validators


def _factory(handler) -> validators.ClientFactory:
    transport = httpx.MockTransport(handler)
    return lambda: httpx.Client(transport=transport, trust_env=False)


def _spec(**overrides) -> validators.HttpRequestSpec:
    values = {"method": "GET", "url": "http://target.test/item"}
    values.update(overrides)
    return validators.HttpRequestSpec(**values)


def test_replay_passes_when_responses_are_stable() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=b"hello marker")

    outcome = validators.replay(
        _spec(), attempts=3, markers=("marker",), client_factory=_factory(handler)
    )

    assert outcome.passed is True
    assert outcome.confidence == "high"
    assert outcome.attempts == 3
    assert calls["n"] == 3
    assert "3/3" in outcome.summary
    assert "marker" in outcome.evidence


def test_replay_fails_when_every_response_differs() -> None:
    bodies = iter([b"a", b"bb", b"ccc"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=next(bodies))

    outcome = validators.replay(
        _spec(), attempts=3, client_factory=_factory(handler)
    )

    assert outcome.passed is False
    assert outcome.confidence == "low"
    assert "1/3" in outcome.summary


def test_replay_requires_at_least_two_attempts() -> None:
    with pytest.raises(ValueError):
        validators.replay(_spec(), attempts=1)


def test_differential_detects_a_marker_only_in_experiment() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if b"payload" in (request.content or b""):
            return httpx.Response(200, content=b"root:x:0:0")
        return httpx.Response(200, content=b"no results")

    outcome = validators.differential(
        _spec(body=b"id=1"),
        _spec(body=b"id=1 payload"),
        markers=("root:x",),
        client_factory=_factory(handler),
    )

    assert outcome.passed is True
    assert outcome.confidence == "high"
    assert "root:x" in outcome.summary


def test_differential_detects_status_and_length_changes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if b"admin" in (request.content or b""):
            return httpx.Response(403, content=b"forbidden")
        return httpx.Response(200, content=b"x" * 200)

    outcome = validators.differential(
        _spec(body=b"user"),
        _spec(body=b"admin"),
        client_factory=_factory(handler),
    )

    assert outcome.passed is True
    assert "状态码" in outcome.summary
    assert "长度变化" in outcome.summary


def test_differential_ignores_small_dynamic_noise() -> None:
    bodies = iter([b"x" * 100, b"x" * 105])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=next(bodies))

    outcome = validators.differential(
        _spec(), _spec(), client_factory=_factory(handler)
    )

    assert outcome.passed is False
    assert outcome.confidence == "low"


def test_sends_method_headers_and_body_verbatim() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"ok")

    spec = _spec(
        method="POST",
        url="http://target.test/login",
        headers={"X-Test": "yes", "Content-Type": "application/x-www-form-urlencoded"},
        body=b"user=admin&pass=admin",
    )
    validators.replay(spec, attempts=2, client_factory=_factory(handler))

    assert seen[0].method == "POST"
    assert str(seen[0].url) == "http://target.test/login"
    assert seen[0].headers["X-Test"] == "yes"
    assert seen[0].content == b"user=admin&pass=admin"


def test_default_client_does_not_read_environment_proxy() -> None:
    client = validators._default_client_factory()
    try:
        assert client.trust_env is False
        assert client.follow_redirects is False
    finally:
        client.close()
