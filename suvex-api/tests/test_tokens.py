"""机器令牌：签发、使用、scope 判定、吊销（契约 §1.2 / §2.9 / §3.7）。

任务 1 的验收标准里「注册/登录 → 拿到 token」在这个设计里对应两条通道：
浏览器拿到的是会话 Cookie，而**机器拿到的是 `svx_…` 令牌**。
后者由 `POST /settings/tokens` 签发，所以它属于认证范围，在任务 1 就实现。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import GOOD_USERNAME


def _create_token(client: TestClient, **overrides) -> dict:
    payload = {"label": "CI 流水线", "scopes": ["read"], "expiresAt": None}
    payload.update(overrides)
    response = client.post("/api/v1/settings/tokens", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_created_token_plaintext_is_returned_exactly_once(ready_client: TestClient) -> None:
    """★ 完整令牌只在创建响应里出现**一次**，之后任何接口都拿不到。"""
    created = _create_token(ready_client, label="CI 流水线", scopes=["read", "scan"])

    plaintext = created["plaintext"]
    token = created["token"]

    assert plaintext.startswith("svx_")
    # 前端 `ApiToken.prefix` 是 `plaintext.slice(0, 8)` —— 服务端必须用同一个
    # 切片长度，否则界面上显示的前缀和实际令牌对不上。
    assert token["prefix"] == plaintext[:8]
    assert len(token["prefix"]) == 8

    # 列表里**只有 prefix**，没有明文、也没有 value 字段。
    listed = ready_client.get("/api/v1/settings/tokens").json()
    assert len(listed) == 1
    assert listed[0]["prefix"] == token["prefix"]
    assert "plaintext" not in listed[0]
    assert "value" not in listed[0]
    assert plaintext not in ready_client.get("/api/v1/settings/tokens").text


def test_token_works_as_bearer(ready_client: TestClient) -> None:
    plaintext = _create_token(ready_client)["plaintext"]
    # 清掉会话 Cookie，确认真的是**令牌**在起作用，而不是 Cookie 顺带通过了。
    ready_client.cookies.clear()

    response = ready_client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {plaintext}"}
    )

    assert response.status_code == 200
    assert response.json()["id"]


def test_scopes_are_cumulative(ready_client: TestClient) -> None:
    """`admin ⊃ scan ⊃ read`（契约 §1.2 的措辞是「read+scan scope 令牌」，
    前端 `TOKEN_SCOPE_NOTE.scan` 写着「在只读基础上」）。

    所以持 `scan` 令牌的 CI 既能发扫描也能读结果，不必同时申请两个 scope。

    判"scope 通过了"的方式：拿一个**必然 404 的扫描 id** 去调 scan 级端点 ——
    404 说明鉴权与 scope 都过了，只是资源不存在；
    而 scope 不够时是 403，两者不会混淆。
    """
    scan_token = _create_token(ready_client, label="扫描用", scopes=["scan"])["plaintext"]
    headers = {"Authorization": f"Bearer {scan_token}"}
    ready_client.cookies.clear()

    # scan 令牌能读（因为它隐含 read）
    assert ready_client.get("/api/v1/scans", headers=headers).status_code == 200
    # 也能调 scan 级端点（404 = 通过了鉴权，只是那个扫描不存在）
    assert ready_client.post("/api/v1/scans/scan_nope/abort", headers=headers).status_code == 404
    # 但不能改设置
    denied = ready_client.put("/api/v1/settings/sandbox", headers=headers)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "INSUFFICIENT_SCOPE"


def test_read_only_token_cannot_call_scan_level_endpoints(ready_client: TestClient) -> None:
    """只读令牌不能碰 `scan` 级端点 → 403 `INSUFFICIENT_SCOPE`。

    这一条是 scope 体系存在的意义所在：`POST /scans` **会对真实目标发包**，
    而"能访问到 API"不该等于"能发起扫描"。终止同理 ——
    能终止扫描的人本来就该是有权发起扫描的人。
    """
    read_token = _create_token(ready_client, label="报表脚本", scopes=["read"])["plaintext"]
    ready_client.cookies.clear()
    headers = {"Authorization": f"Bearer {read_token}"}

    # 读没问题
    assert ready_client.get("/api/v1/scans", headers=headers).status_code == 200

    for method, path in (
        ("post", "/api/v1/scans"),
        ("post", "/api/v1/scans/scan_nope/abort"),
        ("post", "/api/v1/scans/scan_nope/rerun"),
    ):
        response = getattr(ready_client, method)(path, headers=headers)
        assert response.status_code == 403, f"{method} {path} → {response.status_code}"
        error = response.json()["error"]
        assert error["code"] == "INSUFFICIENT_SCOPE"
        # 文案是中文完整句，并说明需要什么权限
        assert "扫描" in error["message"]


def test_revoked_token_stops_working(ready_client: TestClient) -> None:
    """吊销是**即时**的 —— 这正是契约选择不透明令牌而非 JWT 的原因。"""
    created = _create_token(ready_client)
    token_id = created["token"]["id"]
    headers = {"Authorization": f"Bearer {created['plaintext']}"}

    ready_client.cookies.clear()
    assert ready_client.get("/api/v1/auth/me", headers=headers).status_code == 200

    # 用会话吊销令牌（先重新登录，因为上面把 Cookie 清掉了）。
    logged_in = ready_client.post(
        "/api/v1/auth/login", json={"username": GOOD_USERNAME, "password": "SuvexDevPass2026"}
    )
    assert logged_in.status_code == 200
    assert ready_client.delete(f"/api/v1/settings/tokens/{token_id}").status_code == 204

    ready_client.cookies.clear()
    assert ready_client.get("/api/v1/auth/me", headers=headers).status_code == 401


def test_expired_token_is_rejected(ready_client: TestClient) -> None:
    """`expiresAt` 已过 → 401。永不过期是**显式选项**，不是默认值。"""
    created = _create_token(ready_client, expiresAt="2020-01-01T00:00:00.000Z")
    headers = {"Authorization": f"Bearer {created['plaintext']}"}
    ready_client.cookies.clear()

    assert ready_client.get("/api/v1/auth/me", headers=headers).status_code == 401


def test_never_expiring_token_has_null_expires_at(ready_client: TestClient) -> None:
    """`expiresAt: null` = 永不过期。契约 §6 要求它是显式选项并标注风险。"""
    created = _create_token(ready_client, expiresAt=None)

    assert created["token"]["expiresAt"] is None
    assert created["token"]["lastUsedAt"] is None


def test_unknown_scope_is_rejected(ready_client: TestClient) -> None:
    response = ready_client.post(
        "/api/v1/settings/tokens",
        json={"label": "乱写的", "scopes": ["read", "sudo"], "expiresAt": None},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_token_endpoints_require_admin_scope(ready_client: TestClient) -> None:
    """管令牌是 `admin` 级 —— 一个只读令牌不能给自己签发新令牌。"""
    read_token = _create_token(ready_client, scopes=["read"])["plaintext"]
    ready_client.cookies.clear()

    response = ready_client.post(
        "/api/v1/settings/tokens",
        json={"label": "自提权", "scopes": ["admin"], "expiresAt": None},
        headers={"Authorization": f"Bearer {read_token}"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "INSUFFICIENT_SCOPE"
