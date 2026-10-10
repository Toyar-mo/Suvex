"""认证流程：初始化 → 登录 → 会话 → 登出（契约 §3.1）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest import GOOD_PASSWORD, OTHER_PASSWORD


def test_state_before_setup_says_not_initialized(client: TestClient) -> None:
    """`/auth/state` 是前端启动时的**第一个**请求，登录前就要能调用。"""
    response = client.get("/api/v1/auth/state")

    assert response.status_code == 200
    assert response.json() == {"initialized": False, "authenticated": False}


def test_state_leaks_nothing_beyond_two_booleans(client: TestClient) -> None:
    """它虽然 public，但只能回两个布尔值。

    多回一个 `accountCount` 或 `createdAt` 就等于把"这个系统有没有被用过"
    告诉任何能访问到端口的人 —— 而本平台监听在本机/内网。
    """
    body = client.get("/api/v1/auth/state").json()
    assert set(body) == {"initialized", "authenticated"}


@pytest.mark.parametrize(
    ("password", "expected_fragments"),
    [
        # 前端 `checkPassword` 的三条硬要求，逐条对应。
        ("short1A", ["至少 12 个字符"]),
        ("alllowercase1234", ["同时包含大写与小写字母"]),
        ("NoDigitsHereAtAll", ["包含数字"]),
        # ★ 一次返回**全部**未满足项（契约 §1.3 第 2 条）。
        # 只报第一条会让用户"补了长度又说不含数字"，来回三次。
        # 注意这里用 7 位而不是 12 位 —— 12 位恰好满足第一条，
        # 那样就只剩两条未满足了（写测试时踩过一次）。
        ("aaaaaaa", ["至少 12 个字符", "同时包含大写与小写字母", "包含数字"]),
    ],
)
def test_setup_reports_every_unmet_password_requirement(
    client: TestClient, password: str, expected_fragments: list[str]
) -> None:
    response = client.post("/api/v1/auth/setup", json={"password": password})

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_FAILED"
    details = error["details"]
    assert "password" in details
    for fragment in expected_fragments:
        assert fragment in details["password"], details["password"]
    # `message` 必须是可直接展示的中文完整句（前端 SaveBar 会拼进"保存失败：…"）。
    assert "口令强度不足" in error["message"]


def test_setup_creates_account_and_logs_in(client: TestClient) -> None:
    response = client.post("/api/v1/auth/setup", json={"password": GOOD_PASSWORD})

    assert response.status_code == 201
    body = response.json()
    assert body["user"]["id"]
    # 字段名是 camelCase（契约 §1.1「前端不做字段名转换层」）。
    assert "createdAt" in body["user"]
    assert "created_at" not in body["user"]

    state = client.get("/api/v1/auth/state").json()
    assert state == {"initialized": True, "authenticated": True}


def test_session_cookie_has_all_four_required_attributes(client: TestClient) -> None:
    """契约 §1.2 的四个属性**缺一不可**，每个漏掉都有具体后果。

    这条测试是照着那张后果表写的：
    HttpOnly（XSS 偷不走）、SameSite=Strict（挡 CSRF）、
    Max-Age（不是永久有效）、Path=/（整个站点都带上）。
    """
    response = client.post("/api/v1/auth/setup", json={"password": GOOD_PASSWORD})
    header = response.headers["set-cookie"]

    assert header.startswith("suvex_session=")
    assert "HttpOnly" in header
    assert "samesite=strict" in header.lower()
    assert "Path=/" in header
    # 12 小时 = 43200 秒
    assert "Max-Age=43200" in header
    # 开发态是 http，Secure 会让浏览器根本不回传这个 Cookie ——
    # 症状是"登录成功但下一个请求仍是未登录"，很容易被误判成 CORS 问题。
    assert "Secure" not in header


def test_session_token_is_opaque_not_a_jwt(client: TestClient) -> None:
    """会话值是**不透明随机串**，不是 JWT（契约 §1.2 明确否掉了 JWT）。

    用形状断言而不是读实现：JWT 是 `header.payload.signature` 三段点分，
    不透明串里不会有这个结构。这条测试同时挡住"以后有人顺手换成 JWT"——
    换的话这里会红，而换 JWT 会让「改密码后旧会话立即失效」失效。
    """
    response = client.post("/api/v1/auth/setup", json={"password": GOOD_PASSWORD})
    cookie_value = response.cookies["suvex_session"]

    assert cookie_value.count(".") != 2, "会话值看起来像 JWT，而契约要求不透明随机串"
    assert len(cookie_value) >= 32


def test_setup_is_closed_after_initialization(client: TestClient) -> None:
    """★ 初始化后 `/auth/setup` 必须**彻底关闭**。

    否则它就是一个「任意人可重设管理员口令」的后门 ——
    而本平台的危害不是读到数据，是替攻击者发包（契约 §0.3）。
    """
    client.post("/api/v1/auth/setup", json={"password": GOOD_PASSWORD})

    response = client.post("/api/v1/auth/setup", json={"password": OTHER_PASSWORD})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SETUP_ALREADY_DONE"
    # 而且真的没被改掉：原口令仍能登录。
    client.post("/api/v1/auth/logout")
    assert client.post("/api/v1/auth/login", json={"password": GOOD_PASSWORD}).status_code == 200
    assert client.post("/api/v1/auth/login", json={"password": OTHER_PASSWORD}).status_code == 401


def test_login_failure_does_not_distinguish_causes(ready_client: TestClient) -> None:
    """只有「口令不正确」一种失败，不区分「账号不存在」（账号枚举漏洞）。"""
    ready_client.post("/api/v1/auth/logout")

    response = ready_client.post("/api/v1/auth/login", json={"password": "WrongPass123456"})

    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "UNAUTHENTICATED"
    assert error["message"] == "口令不正确。"
    # 不回声用户输入（契约 §1.3 第 3 条）。
    assert "WrongPass123456" not in response.text


def test_logout_clears_cookie_and_session(ready_client: TestClient) -> None:
    assert ready_client.get("/api/v1/auth/state").json()["authenticated"] is True

    response = ready_client.post("/api/v1/auth/logout")

    assert response.status_code == 204
    set_cookie = response.headers.get("set-cookie", "")
    # 删 Cookie 必须带**与设置时相同的 path**，否则浏览器删的是另一个
    # 不存在的 Cookie —— 表现是"点了登出，刷新一下又登录着"。
    assert "Path=/" in set_cookie
    assert "suvex_session=" in set_cookie
    assert ready_client.get("/api/v1/auth/state").json()["authenticated"] is False
    assert ready_client.get("/api/v1/auth/me").status_code == 401


def test_logout_is_idempotent_when_not_logged_in(client: TestClient) -> None:
    """没有会话时登出也是 204（契约那一格改成了「幂等」）。

    凭据已经过期时返回 401 会让前端只能跳登录页、却清不掉那个失效 Cookie，
    而 Cookie 留在浏览器里没有任何好处。
    """
    assert client.post("/api/v1/auth/logout").status_code == 204
    assert client.post("/api/v1/auth/logout").status_code == 204


def test_me_requires_session_and_returns_camel_case(client: TestClient) -> None:
    assert client.get("/api/v1/auth/me").status_code == 401

    client.post("/api/v1/auth/setup", json={"password": GOOD_PASSWORD})
    response = client.get("/api/v1/auth/me")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"id", "createdAt"}
    # 不返回口令哈希、会话数量、最后登录时间 —— 单管理员系统里
    # 这些字段没有任何消费方，而每一个都是多余的信息面。
    assert "password" not in response.text.lower()


def test_login_and_setup_share_one_rate_limit_bucket(client: TestClient) -> None:
    """契约 §3.1：`/auth/login` 与 `/auth/setup` **都必须限流**。

    口令是本系统唯一的凭据，不限流等于允许在线爆破。

    两者**共用同一个计数桶**（默认 5 次/分钟）。这是刻意的：
    分桶的话攻击者交替打两个端点就能把尝试次数翻倍，
    而限流的目的正是限制"试探这个口令"的总次数。
    代价是 setup 也占一次 —— 下面这条测试把它显式记下来。
    """
    assert client.post("/api/v1/auth/setup", json={"password": GOOD_PASSWORD}).status_code == 201
    client.cookies.clear()

    statuses = [
        client.post("/api/v1/auth/login", json={"password": "WrongPass123456"}).status_code
        for _ in range(6)
    ]

    # setup 已经用掉配额里的 1 次，所以只剩 4 次 401，第 5 次起就是 429。
    assert statuses == [401, 401, 401, 401, 429, 429], statuses

    limited = client.post("/api/v1/auth/login", json={"password": "WrongPass123456"})
    assert limited.json()["error"]["code"] == "RATE_LIMITED"
    # Retry-After 要给前端读到（CORS 里已加进 expose_headers）——
    # 它是界面能告诉用户"多久后再试"的唯一依据。
    assert "retry-after" in {key.lower() for key in limited.headers}


def test_login_alone_allows_five_attempts_per_minute(client: TestClient) -> None:
    """把 setup 的占用排除掉之后，`/auth/login` 单独就是 5 次/分钟。"""
    from app.auth import router as auth_router

    client.post("/api/v1/auth/setup", json={"password": GOOD_PASSWORD})
    client.cookies.clear()
    auth_router.reset_limiters()

    statuses = [
        client.post("/api/v1/auth/login", json={"password": "WrongPass123456"}).status_code
        for _ in range(6)
    ]

    assert statuses == [401, 401, 401, 401, 401, 429], statuses


def test_change_password_revokes_existing_sessions(ready_client: TestClient) -> None:
    """★ 改口令必须让**旧会话立即失效**。

    这是契约 §1.2 拒绝 JWT 的那条理由的兑现 ——
    「JWT 无法即时吊销（改密码后旧令牌仍有效）」。
    所以这个行为必须真的发生，否则选不透明令牌就白选了。
    """
    old_cookie = ready_client.cookies["suvex_session"]

    response = ready_client.post(
        "/api/v1/settings/password",
        json={"current": GOOD_PASSWORD, "next": OTHER_PASSWORD},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ok"
    assert "at" in response.json()

    # 旧 Cookie 重新装回去也应该失效 —— 光清空客户端 Cookie 不算吊销。
    ready_client.cookies.clear()
    ready_client.cookies.set("suvex_session", old_cookie)
    assert ready_client.get("/api/v1/auth/me").status_code == 401

    ready_client.cookies.clear()
    assert ready_client.post("/api/v1/auth/login", json={"password": OTHER_PASSWORD}).status_code == 200
    assert ready_client.post("/api/v1/auth/login", json={"password": GOOD_PASSWORD}).status_code == 401


def test_change_password_rejects_wrong_current(ready_client: TestClient) -> None:
    response = ready_client.post(
        "/api/v1/settings/password",
        json={"current": "DefinitelyNotIt1", "next": OTHER_PASSWORD},
    )

    assert response.status_code == 400
    # 文案只写「当前密码不正确」，不透露任何额外信息（契约 §1.3 第 3 条，
    # 前端 changePassword 的错误文案已经定了调子）。
    assert response.json()["error"]["message"] == "当前密码不正确。"
