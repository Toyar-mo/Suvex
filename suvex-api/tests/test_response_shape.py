"""字段命名与错误信封：契约里最容易"看起来对"的两处。

契约 §1.1 的命名规则（camelCase，前端不做转换层）与 §1.3 的错误形状
是**所有**端点都要遵守的。它们不像业务逻辑那样有测试剧本，
所以漂移起来最安静 —— 前端某天发现 `createdAt` 变成了 `undefined`，
而后端日志一片正常。

这两组测试是那种漂移的机器化拦网。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.schemas import to_snake_case


@pytest.mark.parametrize(
    ("camel", "snake"),
    [
        ("id", "id"),
        ("scanId", "scan_id"),
        ("agentId", "agent_id"),
        ("agentPhase", "agent_phase"),
        ("createdAt", "created_at"),
        ("lastActiveAt", "last_active_at"),
        ("expiresAt", "expires_at"),
        ("hasMore", "has_more"),
        ("eventCount", "event_count"),
        ("includePoc", "include_poc"),
        ("memoryLimitMb", "memory_limit_mb"),
    ],
)
def test_camel_to_snake_mapping(camel: str, snake: str) -> None:
    """alias 生成器的映射本身。

    纯函数测试看起来琐碎，但它是**一处出错就会全站字段变 null** 的地方
    （alias 生成器只设 validation_alias 不设 serialization_alias 这件事
    很反直觉），所以值得钉住。
    """
    assert to_snake_case(camel) == snake


def test_orm_backed_response_uses_camel_case(ready_client: TestClient) -> None:
    """从 ORM 对象序列化出来的响应，字段名仍然是 camelCase。

    这条覆盖的是整条链路：ORM 属性 `created_at` → alias 生成器读它 →
    序列化时用字段名 `createdAt`。中间任何一环写反，这里都会红。
    """
    body = ready_client.get("/api/v1/auth/me").json()

    assert set(body) == {"id", "createdAt"}
    assert body["createdAt"].endswith("Z"), body["createdAt"]


def test_timestamp_format_matches_frontend_mocks(ready_client: TestClient) -> None:
    """时间格式是 `2026-09-23T15:30:42.123Z`（毫秒 + `Z`）。

    不靠 Pydantic 默认序列化：它会给出 `+00:00`，那也是合法 ISO 8601，
    但前端 mock 里全是 `Z`，混着来会让字符串比较排序在某些写法下错位。
    """
    from app.core.timeutil import now_utc, to_iso

    iso = to_iso(now_utc())

    assert iso.endswith("Z")
    assert len(iso) == 24, iso  # 2026-09-23T15:30:42.123Z
    assert iso[19] == "."


def test_error_envelope_shape(client: TestClient) -> None:
    """任何非 2xx 都是 `{"error": {"code", "message"}}`，`details` 只在有内容时出现。"""
    response = client.get("/api/v1/auth/me")

    assert response.status_code == 401
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}


def test_validation_error_is_400_not_422(client: TestClient) -> None:
    """契约要求 `VALIDATION_FAILED` 是 **400**，而 FastAPI 默认给 422。

    前端是按契约写的，返回 422 会让它的校验错误分支整个不触发 ——
    表现是"接口报错了，但表单上一个字段都没标红"。
    """
    response = client.post("/api/v1/auth/setup", json={})

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_FAILED"
    # details 的 key 用**前端字段名**，不是 pydantic 的 loc 路径
    # （`body.password` 对前端没有意义，用户看到的字段就是他自己填的那个框）。
    assert "password" in error["details"]
    assert error["details"]["password"] == "此项必填"


def test_validation_message_is_displayable_chinese(client: TestClient) -> None:
    """`message` 必须是可直接展示的中文完整句。

    前端 `SaveBar` 会把它拼进「保存失败：<error>」，所以它不能是
    `invalid_target` 这种码，也不能是 pydantic 的 `Field required`。
    """
    response = client.post("/api/v1/auth/setup", json={})

    message = response.json()["error"]["message"]
    assert "提交的内容有误" in message
    assert "Field required" not in message
    assert "必填" in message


def test_unknown_extra_field_in_request_body_is_rejected(ready_client: TestClient) -> None:
    """请求体里拼错的字段要报错，不能静默丢掉。

    `extra="ignore"`（Pydantic 默认）的表现是「我明明传了 includePoc=true
    但报告里没有 PoC」—— 没有任何地方提示字段名写错了。
    响应体**不**用这个严格模式：后端多返回一个字段不该让前端解析失败。
    """
    response = ready_client.post(
        "/api/v1/settings/tokens",
        json={"label": "CI", "scopes": ["read"], "expiresAt": None, "typoField": 1},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_method_not_allowed_uses_envelope(ready_client: TestClient) -> None:
    """405 也要用统一信封 + 中文。

    Starlette 默认回 `{"detail": "Method Not Allowed"}` ——
    形状和语言都不对，前端会拿不到 `error.code`。
    """
    response = ready_client.delete("/api/v1/auth/state")

    assert response.status_code == 405
    body = response.json()
    assert body["error"]["code"] == "METHOD_NOT_ALLOWED"
    assert "HTTP 方法" in body["error"]["message"]


def test_internal_error_does_not_leak_internals(monkeypatch) -> None:
    """500 的响应里不能有堆栈、SQL 或异常类名（契约 §1.3 第 3 条）。

    堆栈只进服务端日志。响应里连异常类名都不给 —— 类名本身就是内部结构信息。

    ⚠️ 这里要**单独建一个** `raise_server_exceptions=False` 的客户端：
    Starlette 的测试客户端默认把服务端异常**重新抛回测试进程**，
    那样就永远看不到响应体，而这正是要断言的东西。
    生产环境走的是 ASGI 服务器，没有这层重抛。
    """
    from fastapi.testclient import TestClient

    from app import health
    from app.main import app

    def explode() -> bool:
        raise RuntimeError("SELECT * FROM admin_accounts WHERE password_hash = 'oops'")

    monkeypatch.setattr(health, "ping", explode)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/health")

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert "Traceback" not in response.text
    assert "SELECT" not in response.text
    assert "RuntimeError" not in response.text
    assert "服务器内部错误" in body["error"]["message"]
