"""模型调用重试策略（v1.83）。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.providers.retry import (
    RetryPolicy,
    backoff_ms,
    load_policy,
    save_policy,
    should_retry_status,
)


def test_retry_statuses_match_artex_layers() -> None:
    for status in (408, 429, 500, 502, 503, 504):
        assert should_retry_status(status) is True
    for status in (400, 401, 403, 404, 422):
        assert should_retry_status(status) is False


def test_backoff_is_exponential_and_capped() -> None:
    assert backoff_ms(0, 100) == 100
    assert backoff_ms(1, 100) == 200
    assert backoff_ms(2, 100) == 400
    assert backoff_ms(10, 100) <= 8_000


def test_policy_roundtrip(db_session: Session) -> None:
    save_policy(
        db_session,
        RetryPolicy(
            connect_retries=4,
            connect_interval_ms=150,
            empty_retries=1,
            stream_retries=2,
            breaker_failures=6,
            breaker_cooldown_s=90,
        ),
    )

    policy = load_policy(db_session)

    assert policy.connect_retries == 4
    assert policy.connect_interval_ms == 150
    assert policy.empty_retries == 1
    assert policy.breaker_failures == 6
    assert policy.breaker_cooldown_s == 90


def test_missing_policy_returns_defaults(db_session: Session) -> None:
    policy = load_policy(db_session)

    assert policy.connect_retries == 3
    assert policy.empty_retries == 2
    assert policy.stream_retries == 2
