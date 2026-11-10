"""重试配置响应映射（v1.83）。"""

from __future__ import annotations

from app.providers.retry import RetryPolicy
from app.settings.schemas import LlmRetryOut


def test_retry_out_maps_snake_dataclass_to_camel_fields() -> None:
    out = LlmRetryOut.model_validate(
        RetryPolicy(connect_retries=4, breaker_cooldown_s=90), from_attributes=True
    )

    assert out.connectRetries == 4
    assert out.breakerCooldownS == 90
