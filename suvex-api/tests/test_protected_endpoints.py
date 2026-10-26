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


def test_no_endpoint_returns_501_anymore(ready_client: TestClient) -> None:
    """★ **501 占位已全部清零**（v0.22 起）。

    ## 这条测试的历史

    它原来叫 `test_unimplemented_endpoint_returns_501_not_404`，
    断言某个"当前确实未实现"的端点返回 501 且带任务号。

    那个写法有个它**自己就预见到的**缺陷，注释原文是：

    > 用某个具体端点当"未实现"的例子时，那条断言会随着任务推进
    > **悄悄变成谎话**。所以额外断言它**还没被实现** —— 一旦它被做了，
    > 这条测试会红，提醒把它换到下一个还没做的端点上。

    它确实红了（`/settings` 从 501 变成 200），而那次红是**对的** ——
    只是现在已经**没有"下一个未实现的端点"**可以换了：v0.22 把最后
    六个（`GET /settings` + 五个通知端点）都实现了。

    所以这条测试换个方向：从"某个端点返回 501"改成"**没有端点返回 501**"。

    ## 为什么这样写仍然有价值

    它从"钉住某个具体行为"变成了**守卫一个全局不变量**：
    将来有人加回一个 501 占位（比如新功能的接口先占位），
    这条测试会立刻红 —— 而那正是我们想要的提醒：
    "你又开始用 501 了，确认这是有意的，并同步更新这条测试与契约 §9"。

    ## 怎么查

    遍历全部路由，挑出那些**在 `core.stubs` 里登记过的**（`pending()`）。
    不直接打每个端点：那需要为每个端点构造合法参数，
    而"哪些端点是占位"这件事在代码里已经有唯一答案。
    """
    from app.core import stubs

    # `stubs` 模块里记录着所有"注册了但没实现"的端点。
    # 它现在是空的 —— 这正是要断言的东西。
    pending_endpoints = getattr(stubs, "_PENDING", None)

    if pending_endpoints is None:
        # 实现细节变了（没有那个私有变量）。退化成"源码里搜不到 pending 调用"。
        import pathlib

        app_dir = pathlib.Path(__file__).resolve().parents[1] / "app"
        callers = [
            path
            for path in app_dir.rglob("*.py")
            if path.name != "stubs.py" and "pending(" in path.read_text(encoding="utf-8")
        ]
        assert not callers, (
            f"这些文件里还有 `pending()` 调用：{[str(p) for p in callers]} —— "
            "501 占位又出现了。若是有意为之，请更新本测试与契约 §9"
        )
    else:
        assert not pending_endpoints, (
            f"还有未实现的端点：{sorted(pending_endpoints)} —— "
            "若是有意为之，请更新本测试与契约 §9"
        )


def test_stub_mechanism_still_exists() -> None:
    """501 的**机制**要留着，即使现在没人用它。

    ⚠️ 这一条与上一条是一对，别删其中一个：

      · 上一条说"现在没有 501"；
      · 这一条说"但 `stubs.pending()` 这个工具还在"。

    只留上一条的话，下一个人可能顺手把 `core/stubs.py` 删掉
    （"反正没人用"）—— 而那时想给新接口占位就得重新写一遍，
    并且会忘了那条最要紧的约定：**501 的 message 里要写清
    计划在哪个任务交付**（调用方据此判断"该等版本还是该换 id"）。
    """
    from app.core import stubs

    assert hasattr(stubs, "pending"), "stubs.pending 不见了 —— 501 占位的机制被删了？"


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

    # ★ 断言"不是占位"而不是"字段集合恰好等于某四个名字"。
    #
    # 原来这里写的是 `set(body) == {"metrics", "activity", "severityBreakdown",
    # "hasAnyScan"}` —— 用**相等**来证明"这不是 501 占位"。但相等同时禁止了
    # 这个端点**增加任何字段**：v0.95 加 `attention` 与 `metrics.riskScore`
    # 时它立刻变红，而那几个字段恰恰是这个端点还在正常演进的证据。
    #
    # 判据收紧到意图本身：必须有那四个基线字段（占位实现给不出它们），
    # 且不得出现占位实现特有的形状（`detail`/`error` 信封）。
    assert {"metrics", "activity", "severityBreakdown", "hasAnyScan"} <= set(body), (
        f"/overview 缺少基线字段，返回的是 {sorted(body)}"
    )
    assert "error" not in body and "detail" not in body, (
        f"/overview 返回了错误信封，不是正常响应：{body}"
    )


def test_unknown_path_returns_404_envelope(ready_client: TestClient) -> None:
    """真·不存在的路径仍然是 404，而且也用统一信封。

    Starlette 默认返回 `{"detail": "Not Found"}`（英文、形状不同）——
    前端按契约解析 `error.code` 会得到 undefined。
    """
    response = ready_client.get("/api/v1/no-such-endpoint")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
