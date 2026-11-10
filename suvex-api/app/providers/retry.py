"""模型调用的分层重试策略（v1.83）。

借鉴 ARTEX 的五层重试设计（建连/空响应/安全窗口/熔断/意图重跑），
**独立实现**（AGPL，不复制 Go 代码）。本模块只提供策略与配置：
- `should_retry_status`：哪些 HTTP 状态可重试；
- `backoff_ms`：指数退避（有上限）；
- `load_policy` / `save_policy`：接入 `llm_retry_config` 单例。

真正的重试循环在调用层（`_call_model`）；这里保持纯函数，便于单测。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.providers.models import LlmRetryConfig

RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
MAX_BACKOFF_MS = 8_000


@dataclass(frozen=True)
class RetryPolicy:
    connect_retries: int = 3
    connect_interval_ms: int = 0
    empty_retries: int = 2
    empty_interval_ms: int = 0
    stream_retries: int = 2
    stream_interval_ms: int = 0
    breaker_failures: int = 5
    breaker_interval_ms: int = 0
    breaker_cooldown_s: int = 60


class Breaker:
    """进程级熔断器：按 key（provider 标签）累计连续失败。

    失败到阈值后进入冷却；冷却期内的调用直接拒绝，冷却结束自动放行。
    成功一次即清零（与 ARTEX 的轮询熔断同一语义）。
    """

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._state: dict[str, tuple[int, float]] = {}

    def allow(self, key: str, *, failures: int, cooldown_s: int) -> bool:
        fails, until = self._state.get(key, (0, 0.0))
        return not (fails >= failures and self._clock() < until)

    def record_failure(self, key: str, *, failures: int, cooldown_s: int) -> None:
        fails, until = self._state.get(key, (0, 0.0))
        fails += 1
        if fails >= failures:
            until = self._clock() + cooldown_s
        self._state[key] = (fails, until)

    def retry_after(self, key: str) -> float:
        """冷却还剩多久（秒）。不在冷却期返回 0。"""
        fails, until = self._state.get(key, (0, 0.0))
        return max(0.0, until - self._clock()) if fails > 0 else 0.0

    def record_success(self, key: str) -> None:
        self._state.pop(key, None)


def should_retry_status(status: int) -> bool:
    return status in RETRYABLE_STATUSES


def backoff_ms(attempt: int, base_ms: int) -> int:
    """第 `attempt` 次重试前的等待（0 基）。`base_ms=0` 表示立即重试。"""
    if base_ms <= 0:
        return 0
    return min(base_ms * (2**attempt), MAX_BACKOFF_MS)


def _clamp(value: int, *, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def load_policy(db: Session) -> RetryPolicy:
    row = db.get(LlmRetryConfig, "default")
    if row is None:
        return RetryPolicy()
    return RetryPolicy(
        connect_retries=_clamp(row.connect_retries, low=0, high=10),
        connect_interval_ms=_clamp(row.connect_interval_ms, low=0, high=30_000),
        empty_retries=_clamp(row.empty_retries, low=0, high=10),
        empty_interval_ms=_clamp(row.empty_interval_ms, low=0, high=30_000),
        stream_retries=_clamp(row.stream_retries, low=0, high=10),
        stream_interval_ms=_clamp(row.stream_interval_ms, low=0, high=30_000),
        breaker_failures=_clamp(row.breaker_failures, low=1, high=100),
        breaker_interval_ms=_clamp(row.breaker_interval_ms, low=0, high=30_000),
        breaker_cooldown_s=_clamp(row.breaker_cooldown_s, low=1, high=3_600),
    )


def save_policy(db: Session, policy: RetryPolicy) -> RetryPolicy:
    row = db.get(LlmRetryConfig, "default")
    if row is None:
        row = LlmRetryConfig(id="default")
        db.add(row)
    for field in RetryPolicy.__dataclass_fields__:
        setattr(row, field, getattr(policy, field))
    db.commit()
    return load_policy(db)


def validate_policy(policy: RetryPolicy) -> None:
    """越界直接 400：范围外的手填参数不会静默截断。"""
    limits = {
        "connect_retries": (0, 10),
        "empty_retries": (0, 10),
        "stream_retries": (0, 10),
        "breaker_failures": (1, 100),
    }
    for name, (low, high) in limits.items():
        value = getattr(policy, name)
        if not (low <= value <= high):
            raise ApiError.validation(f"{name} 必须在 {low}..{high} 之间")
    for name in ("connect_interval_ms", "empty_interval_ms", "stream_interval_ms"):
        if not (0 <= getattr(policy, name) <= 30_000):
            raise ApiError.validation(f"{name} 必须在 0..30000 毫秒之间")
    if not (1 <= policy.breaker_cooldown_s <= 3_600):
        raise ApiError.validation("breaker_cooldown_s 必须在 1..3600 秒之间")


__all__ = [
    "Breaker",
    "MAX_BACKOFF_MS",
    "RETRYABLE_STATUSES",
    "RetryPolicy",
    "backoff_ms",
    "load_policy",
    "save_policy",
    "should_retry_status",
    "validate_policy",
]
