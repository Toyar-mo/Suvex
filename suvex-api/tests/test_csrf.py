"""CSRF 防护（契约 §1.2 的必配项）。

契约 §0.3 论证过为什么必须加鉴权，而**决定性的是 CSRF**：
一个无鉴权的 JSON API 如果监听在 localhost/内网，用户浏览器访问的
**任意网页**都能向它发请求 —— 用户打开那个页面，他的机器就替攻击者
发起了一次针对任意目标的攻击扫描。受害者（目标方）看到的来源是**用户的 IP**。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

ALLOWED_ORIGIN = "http://localhost:5173"
FOREIGN_ORIGIN = "https://evil.example"

PASSWORD_BODY = {"current": "SuvexDevPass2026", "next": "Whatever2026Pass"}


def test_cookie_write_from_foreign_origin_is_blocked(ready_client: TestClient) -> None:
    """带 Cookie 的写请求来自未授权源 → 403 `CROSS_ORIGIN_BLOCKED`。

    这是 SameSite=Strict 之外的第二道。为什么还要第二道：
    `SameSite` 是浏览器行为，而浏览器行为有历史包袱 ——
    新注册域名有默认值差异、`localhost` 有特殊放宽。
    服务端自己判一次 `Origin` 的成本是几微秒，
    而这把安全边界从「依赖浏览器实现正确」变成了「依赖我们自己的一行判断」。
    """
    response = ready_client.post(
        "/api/v1/settings/password",
        json=PASSWORD_BODY,
        headers={"Origin": FOREIGN_ORIGIN},
    )

    assert response.status_code == 403, response.text
    error = response.json()["error"]
    assert error["code"] == "CROSS_ORIGIN_BLOCKED"
    assert "来源" in error["message"]


def test_cookie_write_from_allowed_origin_passes(ready_client: TestClient) -> None:
    """来自配置里允许的源 → 放行（这里最终 200，说明 CSRF 那道过了）。"""
    response = ready_client.post(
        "/api/v1/settings/password",
        json=PASSWORD_BODY,
        headers={"Origin": ALLOWED_ORIGIN},
    )

    assert response.status_code == 200, response.text


def test_cookie_write_from_same_origin_passes(ready_client: TestClient) -> None:
    """同源请求必须是放行的。

    生产环境常把前端与 API 放在同一个域名下，此时 `Origin` 等于 API 自己的
    地址，而它不会出现在 `SUPEX_CORS_ORIGINS`（那是给开发态跨源用的）。
    同源请求按定义不是 CSRF —— 不放行的话生产环境会全站写操作 403。
    """
    response = ready_client.post(
        "/api/v1/settings/password",
        json=PASSWORD_BODY,
        headers={"Origin": "http://testserver"},
    )

    assert response.status_code == 200, response.text


def test_form_encoded_write_is_rejected(ready_client: TestClient) -> None:
    """跨站 HTML 表单**只能**发出三种 Content-Type，本 API 一个都不收。

    契约 §1.2 的「所有写操作要求 Content-Type: application/json」——
    这条就是它的可执行版本。少了它，一个 `<form method=post>` 就能
    在没有 CORS 预检的情况下打到写端点（表单提交是"简单请求"，
    不触发预检，所以 Origin 检查是唯一能拦它的依据，而有些老浏览器的
    表单提交不带 Origin）。
    """
    response = ready_client.post(
        "/api/v1/settings/password",
        content="current=x&next=y",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_bearer_token_is_not_subject_to_csrf_checks(ready_client: TestClient) -> None:
    """机器令牌不受 CSRF 约束 —— 它本来就不该受。

    CSRF 的定义是"借用户的浏览器"：浏览器会自动带上 Cookie，
    但**不会**自动带上 `Authorization` 头。所以拿 Bearer 的请求
    无论来自哪个源都不构成 CSRF，而给它加 Origin 检查只会让
    "在别的域名下跑 CI"这种正常用法莫名失败。
    """
    created = ready_client.post(
        "/api/v1/settings/tokens",
        json={"label": "CI", "scopes": ["admin"], "expiresAt": None},
    ).json()
    ready_client.cookies.clear()

    response = ready_client.post(
        "/api/v1/settings/password",
        json={"current": "SuvexDevPass2026", "next": "Whatever2026Pass"},
        headers={
            "Authorization": f"Bearer {created['plaintext']}",
            "Origin": FOREIGN_ORIGIN,
        },
    )

    assert response.status_code == 200, response.text


def test_read_requests_are_not_csrf_checked(ready_client: TestClient) -> None:
    """GET 不做 Origin 检查。

    读操作不改变状态，而跨源读取本来就被 CORS 挡住（浏览器不让 JS
    读到响应）。给 GET 加 Origin 检查只会挡住「从别的页面链过来看文档」
    这种无害场景。
    """
    response = ready_client.get("/api/v1/auth/me", headers={"Origin": FOREIGN_ORIGIN})

    assert response.status_code == 200
