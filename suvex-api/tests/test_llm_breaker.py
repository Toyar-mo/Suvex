"""模型调用熔断（v1.83）。"""

from __future__ import annotations

from app.providers.retry import Breaker


def test_opens_after_threshold_and_cools_down() -> None:
    now = [100.0]
    breaker = Breaker(clock=lambda: now[0])

    assert breaker.allow("p", failures=2, cooldown_s=60) is True
    breaker.record_failure("p", failures=2, cooldown_s=60)
    breaker.record_failure("p", failures=2, cooldown_s=60)
    assert breaker.allow("p", failures=2, cooldown_s=60) is False

    now[0] += 61
    assert breaker.allow("p", failures=2, cooldown_s=60) is True


def test_success_resets_failures() -> None:
    breaker = Breaker()
    breaker.record_failure("p", failures=2, cooldown_s=60)
    breaker.record_success("p")

    assert breaker.allow("p", failures=2, cooldown_s=60) is True


def test_retry_after_reports_remaining_cooldown() -> None:
    now = [100.0]
    breaker = Breaker(clock=lambda: now[0])
    breaker.record_failure("p", failures=1, cooldown_s=60)
    assert breaker.retry_after("p") == 60.0
    now[0] += 20
    assert 39.0 <= breaker.retry_after("p") <= 41.0
    now[0] += 41
    assert breaker.retry_after("p") == 0.0
