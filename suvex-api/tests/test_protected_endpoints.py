"""未带凭据的请求一律 401（任务 1 的验收标准之一）。

这张表是照着 OpenAPI 里"除 public 之外的全部端点"写的。
用参数化而不是每个端点一个函数：漏掉一个端点的代价是
「它悄悄变成公开的」，而参数化的表能让遗漏一眼看出来。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

# 契约 §1.2 里标为 public 的端点。它们**必须**能不带凭据调用。
PUBLIC_ENDPOINTS: list[tuple[str, str]] = [
    ("GET", "/api/v1/health"),
    ("GET", "/api/v1/health/sandbox"),
    ("GET", "/api/v1/auth/state"),
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/setup"),
]

# 其余全部端点 → 401。
PROTECTED_ENDPOINTS: list[tuple[str, str]] = [
    ("GET", "/api/v1/auth/me"),
    ("GET", "/api/v1/providers"),
    ("POST", "/api/v1/providers"),
    ("PATCH", "/api/v1/providers/prov_x"),
    ("DELETE", "/api/v1/providers/prov_x"),
    ("POST", "/api/v1/providers/prov_x/verify"),
    ("POST", "/api/v1/providers/prov_x/default"),
    ("GET", "/api/v1/scans"),
    ("POST", "/api/v1/scans"),
    ("GET", "/api/v1/scans/scan_x"),
    ("POST", "/api/v1/scans/scan_x/abort"),
    ("POST", "/api/v1/scans/scan_x/rerun"),
    ("GET", "/api/v1/scans/scan_x/agents"),
    ("GET", "/api/v1/scans/scan_x/events"),
    ("GET", "/api/v1/scans/scan_x/events/recent"),
    ("GET", "/api/v1/events"),
    ("GET", "/api/v1/findings"),
    ("GET", "/api/v1/findings/stats"),
    ("POST", "/api/v1/findings/status"),
    ("GET", "/api/v1/findings/finding_x"),
    ("GET", "/api/v1/reports"),
    ("POST", "/api/v1/reports"),
    ("POST", "/api/v1/reports/preview"),
    ("GET", "/api/v1/reports/rpt_x"),
    ("GET", "/api/v1/reports/rpt_x/download"),
    ("DELETE", "/api/v1/reports/rpt_x"),
    ("GET", "/api/v1/overview"),
    ("GET", "/api/v1/settings"),
    ("PUT", "/api/v1/settings/sandbox"),
    ("PUT", "/api/v1/settings/notices/rule"),
    ("POST", "/api/v1/settings/notices/channels"),
    ("PATCH", "/api/v1/settings/notices/channels/ch_x"),
    ("DELETE", "/api/v1/settings/notices/channels/ch_x"),
    ("POST", "/api/v1/settings/notices/channels/ch_x/test"),
    ("GET", "/api/v1/settings/tokens"),
    ("POST", "/api/v1/settings/tokens"),
    ("DELETE", "/api/v1/settings/tokens/tok_x"),
    ("POST", "/api/v1/settings/password"),
]


@pytest.mark.parametrize(("method", "path"), PROTECTED_ENDPOINTS)
def test_protected_endpoint_returns_401_without_credentials(
    client: TestClient, method: str, path: str
) -> None:
    """无凭据 → 401 `UNAUTHENTICATED`，且用的是统一错误信封。

    ⚠️ 状态码必须是 **401 而不是 501/404**：鉴权依赖在端点函数体之前执行，
    所以「未实现的端点」也是先 401 —— 否则一个未实现的端点会变成
    **免鉴权的存在性探测**（能区分"端点存在但没做"与"没有这个端点"）。
    """
    response = client.request(method, path)

    assert response.status_code == 401, f"{method} {path} → {response.status_code} {response.text}"
    body = response.json()
    assert body["error"]["code"] == "UNAUTHENTICATED"
    # message 是可直接展示的中文完整句。
    assert body["error"]["message"] == "请先登录。"


@pytest.mark.parametrize(("method", "path"), PUBLIC_ENDPOINTS)
def test_public_endpoint_does_not_require_credentials(
    client: TestClient, method: str, path: str
) -> None:
    """public 端点**不能**返回 401。

    它们的存在理由就是"登录前要能调用"：`/auth/state` 决定显示初始化页
    还是登录页，`/health` 的调用方是编排器。任何一条变成 401，
    启动流程就死锁在登录页上。
    """
    response = client.request(method, path, json={} if method == "POST" else None)

    assert response.status_code != 401, f"{method} {path} 不该要求凭据"


def test_invalid_token_shape_returns_401_without_touching_database(
    ready_client: TestClient,
) -> None:
    """形状不对的 Bearer 直接 401，不查库。

    `looks_like_machine_token` 先看 `svx_` 前缀再看库 ——
    否则任何人往 Authorization 头里塞点东西都会触发一次带索引的查询，
    这是一个不需要认证的放大面。
    """
    response = ready_client.get(
        "/api/v1/scans", headers={"Authorization": "Bearer not-a-suvex-token"}
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def test_unimplemented_endpoint_returns_501_not_404(ready_client: TestClient) -> None:
    """未实现的端点回 **501**，不是 404。

    404 的语义是「这个资源不存在」，而真实情况是「端点还没做」——
    对调用方来说是两种完全不同的处理：前者该换 id，后者该等版本。
    而且响应里要说清**计划在哪个任务交付**，否则调用方只能去翻任务清单。

    ## ⚠️ 这个测试**曾经是一条把过期承诺洗成绿灯的测试**

    它原来拿 `/overview` 当例子，断言 `"任务 6" in message` 通过。
    但 `/overview` 的 501 消息写着「计划在后端**任务 6**中交付」，
    而任务 6 真的来了 —— 交付的是**报告**，不是 `/overview`。
    于是那条消息变成了假话，而这条测试**照旧通过**：
    它钉的是"机制在"（501 + 任务号），恰好把"承诺已经过期"这件事
    读成了绿的。

    教训：用某个具体端点当"未实现"的例子时，那条断言会随着任务推进
    **悄悄变成谎话**。所以现在的做法是：

    * 挑一个**当前确实**未实现的端点（`/settings`，计划任务 7）；
    * 并额外断言它**还没被实现** —— 一旦它被做了，这条测试会红，
      提醒把它换到下一个还没做的端点上，而不是继续假装。
    """
    response = ready_client.get("/api/v1/settings")

    assert response.status_code == 501, (
        f"/settings 已经不是占位了（HTTP {response.status_code}）—— "
        "请把这条测试的目标换成下一个尚未实现的端点，别让它继续断言旧行为"
    )
    error = response.json()["error"]
    assert error["code"] == "NOT_IMPLEMENTED"
    assert "尚未实现" in error["message"]
    assert "任务 7" in error["message"]


def test_overview_is_not_a_stub_anymore(ready_client: TestClient) -> None:
    """★ `/overview` **不再是 501 占位** —— 它已按契约 §9 在任务 6 交付。

    这条与上面那条是**成对**的：上面钉"占位机制还在"，
    这条钉"已经交付的端点不再假装没做"。

    为什么值得单独一条：`/overview` 的 501 消息里写着"计划在任务 6 中交付"，
    而任务 6 交付的是报告 —— 那条消息从任务 6 结束那一刻起就是假话了。
    没有这条测试的话，它会一直假下去：**一个永远返回 501 的端点
    不会自己报错**，而验收脚本当时把这个 501 断言成了 [PASS]。
    """
    response = ready_client.get("/api/v1/overview")

    assert response.status_code == 200, (
        f"/overview 仍在返回 {response.status_code}：{response.text[:200]}"
    )
    body = response.json()
    assert set(body) == {"metrics", "activity", "severityBreakdown", "hasAnyScan"}


def test_unknown_path_returns_404_envelope(ready_client: TestClient) -> None:
    """真·不存在的路径仍然是 404，而且也用统一信封。

    Starlette 默认返回 `{"detail": "Not Found"}`（英文、形状不同）——
    前端按契约解析 `error.code` 会得到 undefined。
    """
    response = ready_client.get("/api/v1/no-such-endpoint")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
