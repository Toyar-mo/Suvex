"""平台「识别」阶段不得把 AccessKey 写进库里（2026-11-09 复核）。

## 为什么值得一条测试

设置页那句「识别阶段不落库」是一条**可验证的承诺**，而它靠的是
"调用链上没有人写它"这种消极事实 —— 没有测试的话，将来谁在 `_finish`
里顺手多存一个字段、或者把 key 塞进 `last_raw`，都不会有人发现。

这里从 HTTP 入口打进去，然后把平台行**所有文本列**扫一遍找那个 key。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.kernel.models import KernelPlatform

API = "/api/v1"
SECRET = "ak_live_THIS_SHOULD_NEVER_BE_STORED_123456"


@pytest.fixture(autouse=True)
def _no_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """不让后台线程真的去打平台：这里验的是**落库**，不是生成。"""
    monkeypatch.setattr(
        "app.kernel.platforms.start_build", lambda *a, **kw: True, raising=True
    )


def test_access_key_is_not_persisted_during_recognition(ready_client: TestClient) -> None:
    resp = ready_client.post(
        f"{API}/kernel/platforms/draft",
        json={"baseUrl": "https://example.test/api/v1/agent", "accessKey": SECRET},
    )
    assert resp.status_code == 202, resp.text

    platform_id = resp.json()["id"]

    from app.core.db import get_session_factory

    with get_session_factory()() as db:
        row = db.scalars(select(KernelPlatform).where(KernelPlatform.id == platform_id)).one()
        blob = " ".join(
            str(value)
            for value in (
                row.slug,
                row.display_name,
                row.base_url,
                row.driver,
                row.source,
                row.status,
                row.last_error,
                row.last_raw,
                repr(row.spec),
            )
            if value is not None
        )

    assert SECRET not in blob, "识别阶段的 AccessKey 不应出现在平台行的任何字段里"
