"""健康检查与启动自检。"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_health_is_public_and_reports_ok(client: TestClient) -> None:
    """`/health` 必须**无需鉴权**：它的调用方是编排器与监控，它们没有凭据。"""
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    # 版本号要有（契约 §3.8 的响应形状），但**不能**有数据库地址、
    # 延迟、表数量之类的内部信息 —— 这个端点是 public 的。
    assert body["version"]
    assert set(body) == {"status", "version"}


def test_health_sandbox_defaults_to_stopped(client: TestClient) -> None:
    """沙箱状态初始必须是 `stopped`。

    前端演示数据里是 `running`，但真实初始值写 `running` 会让界面
    **宣称沙箱在跑而实际没有** —— 状态语义撒谎，与契约 §3.3 拒绝把
    `queued` 写成 `running` 是同一条原则。
    """
    # 迁移只建表，不插单例行，所以这里先走"没有配置行"的分支：
    # 它也必须回 stopped 而不是 404（容器 healthcheck 不该因为
    # 少一行种子数据就失败）。
    response = client.get("/api/v1/health/sandbox")

    assert response.status_code == 200
    assert response.json()["status"] == "stopped"


def test_openapi_schema_covers_every_contract_endpoint(client: TestClient) -> None:
    """契约里定义的端点全部注册了 —— 未实现的也是 501 而不是 404。

    这条测试的价值在于「端点被悄悄删掉」这件事很难被人发现：
    前端调用它会得到 404，而那可能被当成"数据不存在"处理。

    ⚠️ v0.39：它依赖 `/openapi.json`，而那个端点现在**默认关闭**
    （`SUPEX_EXPOSE_DOCS`，见 `core/config.py` 的说明：公网不该匿名
    可读全部端点清单）。`tests/conftest.py` 为测试显式打开了它。

    这里**显式报出**"文档被关掉了"而不是让它以一个 `KeyError: 'paths'`
    崩掉 —— 后者看起来像测试写错了，而真实原因是配置。
    """
    response = client.get("/api/v1/openapi.json")
    assert response.status_code == 200, (
        "文档端点不可用 —— 对账测试需要它。"
        "测试环境应由 conftest 设 SUPEX_EXPOSE_DOCS=true；"
        "若是外部显式设成了 false，这条对账就失去了依据（不是代码坏了）。"
    )
    schema = response.json()
    paths = schema["paths"]

    def has(method: str, path: str) -> bool:
        return path in paths and method.lower() in paths[path]

    expected = [
        ("get", "/api/v1/health"),
        ("get", "/api/v1/health/sandbox"),
        ("get", "/api/v1/auth/state"),
        ("post", "/api/v1/auth/setup"),
        ("post", "/api/v1/auth/login"),
        ("post", "/api/v1/auth/logout"),
        ("get", "/api/v1/auth/me"),
        ("get", "/api/v1/providers"),
        ("post", "/api/v1/providers"),
        ("patch", "/api/v1/providers/{providerId}"),
        ("delete", "/api/v1/providers/{providerId}"),
        ("post", "/api/v1/providers/{providerId}/verify"),
        ("post", "/api/v1/providers/{providerId}/default"),
        ("get", "/api/v1/scans"),
        ("post", "/api/v1/scans"),
        ("get", "/api/v1/scans/{scanId}"),
        ("post", "/api/v1/scans/{scanId}/abort"),
        ("post", "/api/v1/scans/{scanId}/rerun"),
        ("get", "/api/v1/scans/{scanId}/agents"),
        ("get", "/api/v1/scans/{scanId}/events"),
        ("get", "/api/v1/scans/{scanId}/events/recent"),
        ("get", "/api/v1/events"),
        ("get", "/api/v1/findings"),
        ("get", "/api/v1/findings/stats"),
        ("post", "/api/v1/findings/status"),
        ("get", "/api/v1/findings/{findingId}"),
        ("get", "/api/v1/reports"),
        ("post", "/api/v1/reports"),
        ("post", "/api/v1/reports/preview"),
        ("get", "/api/v1/reports/{reportId}"),
        ("get", "/api/v1/reports/{reportId}/download"),
        ("delete", "/api/v1/reports/{reportId}"),
        ("get", "/api/v1/overview"),
        ("get", "/api/v1/settings"),
        ("put", "/api/v1/settings/sandbox"),
        ("put", "/api/v1/settings/notices/rule"),
        ("post", "/api/v1/settings/notices/channels"),
        ("patch", "/api/v1/settings/notices/channels/{channelId}"),
        ("delete", "/api/v1/settings/notices/channels/{channelId}"),
        ("post", "/api/v1/settings/notices/channels/{channelId}/test"),
        ("get", "/api/v1/settings/tokens"),
        ("post", "/api/v1/settings/tokens"),
        ("delete", "/api/v1/settings/tokens/{tokenId}"),
        ("post", "/api/v1/settings/password"),
    ]

    missing = [f"{method.upper()} {path}" for method, path in expected if not has(method, path)]
    assert not missing, f"契约里有但没注册的端点：{missing}"


def test_startup_verifies_schema(client: TestClient) -> None:
    """lifespan 里的 `verify_database()` 真的跑过。

    间接验证方式：应用能起来就说明它没抛 —— 而如果它没被调用，
    这条测试也不会失败。所以这里直接调一次函数本身，
    确认它在结构完整时安静通过。
    """
    from app.core.bootstrap import verify_database

    verify_database()  # 不抛即通过


# ══════════════════════════════════════════════════════════
# ★★ v0.39：公网暴露面开关
# ══════════════════════════════════════════════════════════
#
# 这一组守的是"三个端点别默认挂到公网上"。它们的**默认值**是本次
# 改动的实质，所以要用测试钉住 —— 否则哪天有人把默认改回 True，
# 只有等下一次公网审计才会发现。


def test_docs_are_off_by_default() -> None:
    """★ `SUPEX_EXPOSE_DOCS` 默认必须是 **False**。

    ⚠️ 这条**不看 client**，直接看配置默认值 —— 因为
    `tests/conftest.py` 为了那条契约对账测试把它设成了 true。
    要守的是"**默认**值"，所以绕开被测试环境改过的实例。
    """
    from app.core.config import Settings

    assert Settings.model_fields["expose_docs"].default is False


def test_sandbox_image_is_off_by_default() -> None:
    """★ `SUPEX_EXPOSE_SANDBOX_IMAGE` 默认必须是 **False**。

    一个具体到版本号的镜像 tag 是可定向的（能据此找已知 CVE）。
    """
    from app.core.config import Settings

    assert Settings.model_fields["expose_sandbox_image"].default is False


def test_sandbox_image_is_hidden_when_disabled(
    client: TestClient, monkeypatch
) -> None:
    """★ 关掉时**不返回镜像名**，但 `status` 仍要可读。

    ⚠️ `status` 不能一起挡掉：容器的 healthcheck 要在没有凭据的
    情况下判断沙箱是否可用，而 status 本身不泄露什么。
    """
    from app.core.config import get_settings

    monkeypatch.setenv("SUPEX_EXPOSE_SANDBOX_IMAGE", "false")
    get_settings.cache_clear()
    try:
        response = client.get("/api/v1/health/sandbox")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "stopped"
        assert body["image"] == "", "关掉时不该返回镜像名"
    finally:
        get_settings.cache_clear()


def test_metrics_needs_both_switches(client: TestClient, monkeypatch) -> None:
    """★ `/metrics` 的暴露条件是 `metrics_enabled` **且** `expose_metrics`。

    采集关掉而暴露开着是自相矛盾的，所以取**与**。
    这条真的去请求，验证"只关一个也能挡住"。
    """
    from app.core.config import get_settings

    # ① 只关"暴露" → 应当 404
    monkeypatch.setenv("SUPEX_EXPOSE_METRICS", "false")
    monkeypatch.setenv("SUPEX_METRICS_ENABLED", "true")
    get_settings.cache_clear()
    try:
        assert client.get("/api/v1/metrics").status_code == 404
    finally:
        get_settings.cache_clear()

    # ② 只关"采集" → 同样应当 404（AND 语义）
    monkeypatch.setenv("SUPEX_EXPOSE_METRICS", "true")
    monkeypatch.setenv("SUPEX_METRICS_ENABLED", "false")
    get_settings.cache_clear()
    try:
        assert client.get("/api/v1/metrics").status_code == 404
    finally:
        get_settings.cache_clear()

    # ③ 两个都开 → 200（证明前两条不是因为别的原因 404）
    monkeypatch.setenv("SUPEX_EXPOSE_METRICS", "true")
    monkeypatch.setenv("SUPEX_METRICS_ENABLED", "true")
    get_settings.cache_clear()
    try:
        assert client.get("/api/v1/metrics").status_code == 200
    finally:
        get_settings.cache_clear()


def test_openapi_declares_the_auth_schemes(client: TestClient) -> None:
    """★ F4：契约里必须写出**怎么鉴权**（加固前是 null）。

    实测加固前：`securitySchemes` 与顶层 `security` 都是 `null` ——
    而这份文档自称"机器可读的接口说明"，调用方照着它写客户端时
    完全不知道要带 Bearer 还是 Cookie。

    ⚠️ 本平台有**两套**通道（机器令牌 / 会话 Cookie），
    所以 `security` 里应当是**两个项**（OpenAPI 里项之间是 OR）。
    """
    schema = client.get("/api/v1/openapi.json").json()

    schemes = schema.get("components", {}).get("securitySchemes")
    assert schemes, "securitySchemes 不能为空 —— 文档没说怎么鉴权"
    assert "BearerAuth" in schemes
    assert "SessionCookie" in schemes
    assert schemes["SessionCookie"]["in"] == "cookie"

    security = schema.get("security")
    assert security, "顶层 security 不能为空"
    # 两项 = "Bearer 或 Cookie"，而不是一项里的两个（那是 AND）
    assert len(security) >= 2
