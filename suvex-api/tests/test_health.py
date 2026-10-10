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
    """
    schema = client.get("/api/v1/openapi.json").json()
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
