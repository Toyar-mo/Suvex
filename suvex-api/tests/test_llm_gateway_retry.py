"""模型调用状态重试（v1.83）。"""

from __future__ import annotations

import asyncio

import pytest

from app.kernel import runner
from app.providers.retry import RetryPolicy


class _Resp:
    def __init__(self, status: int, payload: dict) -> None:
        self.status_code = status
        self._payload = payload
        self.text = ""

    def json(self) -> dict:
        return self._payload


class _Client:
    calls = 0

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        pass

    async def __aenter__(self) -> "_Client":
        return self

    async def __aexit__(self, *args) -> None:  # noqa: ANN002
        return None

    async def post(self, *args, **kwargs):  # noqa: ANN002, ANN003
        type(self).calls += 1
        if type(self).calls < 3:
            return _Resp(429, {"error": "rate limited"})
        return _Resp(200, {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})


def test_gateway_retries_429_until_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _Client.calls = 0
    monkeypatch.setattr(runner.httpx, "AsyncClient", _Client)

    gateway = runner.LlmGateway(
        model="gpt-4o",
        api_key="k",
        base_url="https://api.openai.com/v1",
        retry_policy=RetryPolicy(connect_retries=2, connect_interval_ms=0),
    )
    reply = asyncio.run(gateway.chat([{"role": "user", "content": "hi"}], system="s"))

    assert _Client.calls == 3
    assert reply.text == "ok"
