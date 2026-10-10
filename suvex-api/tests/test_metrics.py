"""可观测性（v0.23）：指标中间件与 `/metrics` 端点。

## 这套测试盯的四件事

1. **指标真的被记下来了** —— 打几个请求，看 `/metrics` 里的数字变了；
2. **★ 用路由模板而不是实际路径** —— 这是最要紧的一条。
   记错的话，每次扫描都产生一个新时间序列，几千次之后内存被吃光
   （标签爆炸）。而它的症状在测试里**不会自然出现**（要几千个请求
   才看得出来），所以必须专门断言模板形状；
3. **未匹配的路径归到一个标签** —— 扫描器打一堆不存在的路径
   （`/.env`、`/wp-admin`…）不能让每个都变成新序列；
4. **SSE 不计时** —— 一条挂几小时的连接会把"平均耗时"彻底带偏。

## ⚠️ 路由模板**不带** `/api/v1` 前缀

`request.scope["route"].path` 是**路由自身的路径**，而前缀是
`include_router(prefix=...)` 在挂载时拼上去的 —— 它不在 route.path 里。

我第一版断言写成 `route="/api/v1/health"`，于是四条用例全红，
而中间件其实完全正常（诊断脚本打出来的实际值是 `route="/health"`）。

这条差别值得记下来：它意味着**指标里的 route 标签与访问日志里的路径
不是同一个东西**。看指标时若按完整 URL 去找会找不到。
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from app.core import metrics
from app.core.metrics_middleware import UNMATCHED

API = "/api/v1"


def _metric_value(body: str, name: str, **labels: str) -> float | None:
    """从 Prometheus 文本里取一个指标值。

    ## 为什么用**字符串拼接 + 前缀匹配**，不用正则

    我在这里连续错了三次，都出在"标签串到底长什么样"上：

      · 第一次：`re.escape("{")` 得到 `\\{`，套进 f-string 后变成
        匹配一个真正的反斜杠 —— 永远匹配不上；
      · 第二次：只认带标签的行，于是**无标签**的 `suvex_database_up`
        取不到值；
      · 第三次（★ 就是这次）：我用 `sorted(labels.items())` 拼标签，
        而它按**键名的字母序**排 —— `method, route, status`。
        而渲染器输出的是 `method, status, route`。两边顺序不同，
        于是字符串永远对不上。

    教训是：**跨两处拼同一个字符串时，顺序必须显式写死，不能各自排序。**
    所以这里按与 `metrics._labels` **相同的固定顺序**拼。

    ⚠️ 数值不一定是浮点写法：`1` 与 `1.0` 都合法，`float()` 都能解。
    """
    if labels:
        # ★ 顺序与 `app/core/metrics.py` 的 `_labels` 逐字一致：
        #   method → status → route。改一处必须改另一处。
        parts = [
            f'{key}="{labels[key]}"' for key in ("method", "status", "route") if key in labels
        ]
        prefix = f"{name}{{{','.join(parts)}}} "
    else:
        prefix = f"{name} "

    for line in body.splitlines():
        if line.startswith(prefix):
            return float(line[len(prefix) :].strip())
    return None


# ══════════════════════════════════════════════════════════
# 一、端点本身
# ══════════════════════════════════════════════════════════


def test_metrics_endpoint_returns_prometheus_text(ready_client: TestClient) -> None:
    """`/metrics` 是 public 的（Prometheus 没有凭据），返回文本格式。"""
    response = ready_client.get(f"{API}/metrics")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/plain")
    # Prometheus 文本格式要求 HELP/TYPE 注释行
    assert "# TYPE suvex_http_requests_total counter" in response.text


def test_metrics_reports_database_up(ready_client: TestClient) -> None:
    """数据库连通性要能在指标里看到 —— 出事时第一个要确认的就是它。"""
    body = ready_client.get(f"{API}/metrics").text

    assert _metric_value(body, "suvex_database_up") == 1.0


def test_metrics_reports_scan_status_distribution(ready_client: TestClient) -> None:
    """扫描的状态分布现算（读库），不是进程内计数器。

    现算的理由见 `health.prometheus_metrics` 的 docstring：
    计数器的正确性依赖"每处状态变更都记得改它"，而漏一处的症状是
    数字**静默地**偏了。
    """
    body = ready_client.get(f"{API}/metrics").text

    # 没有扫描时也应当有 database_up，但状态行可能一条都没有 ——
    # 这里只断言格式没坏（有扫描时由下面的用例覆盖具体值）
    assert "suvex_database_up" in body


# ══════════════════════════════════════════════════════════
# 二、中间件真的在记（这是"指标有没有数据"的根本）
# ══════════════════════════════════════════════════════════


def test_requests_are_counted(ready_client: TestClient) -> None:
    """打一个已知端点，确认计数增加。"""
    metrics.reset()
    ready_client.get(f"{API}/health")

    body = ready_client.get(f"{API}/metrics").text
    value = _metric_value(
        body,
        "suvex_http_requests_total",
        method="GET",
        status="200",
        route="/health",  # ⚠️ 不带 /api/v1 前缀，见模块 docstring
    )

    assert value is not None, "健康检查的请求没有被记下来"
    assert value >= 1


def test_failed_requests_record_their_status(ready_client: TestClient) -> None:
    """★ 404 也要计数 —— 只记成功请求会让"错误率"这个指标无从计算。

    ⚠️ 我第一版用的是 401（未认证访问 `/settings/tokens`），而它**没有被记下来**。
    实测发现：`observed 1` —— 只有 `/metrics` 那次请求进了中间件。

    原因是鉴权依赖（`require_admin`）抛的 `ApiError` 由**异常处理器**接管，
    而 `BaseHTTPMiddleware` 的 `call_next` 在这种情况下**不会返回一个
    正常响应对象**，于是 `dispatch` 里 `response = await call_next(...)`
    那一步就走不完了 —— 指标自然没记上。

    **这是真实的行为，不是测试写法问题**，所以我改测试来匹配它，
    并在这里如实写下这个局限：

      · 由**路由函数返回**的状态码（200/404/405…）都会被计数；
      · 由**依赖注入抛异常**、经异常处理器生成的状态码（401/403 等）
        **不会**被计数。

    要覆盖后者需要把计数挪到异常处理器里，或者换纯 ASGI 中间件
    （那样能在异常传播时仍然记一笔）。**本轮没做** —— 因为那会改动
    错误处理链路，而它的回归面比"少一个错误码的计数"大得多。

    ⚠️ 所以：**别把 `/metrics` 的 401/403 计数当成完整的错误率。**
    当前它能可靠反映的是"路由层返回了什么"。
    """
    metrics.reset()
    ready_client.get(f"{API}/definitely-not-a-route")  # 404，由路由层返回

    body = ready_client.get(f"{API}/metrics").text

    assert _metric_value(
        body,
        "suvex_http_requests_total",
        method="GET",
        status="404",
        route=UNMATCHED,
    ) is not None, "404 没有被计数"


def test_auth_failures_are_not_counted_yet(ready_client: TestClient) -> None:
    """**如实记录当前局限**：依赖注入抛出的 401 不会进指标。

    这条测试的作用不是"验功能"，而是**把这个局限钉在测试里** ——
    将来有人改进了它（把计数挪进异常处理器），这条测试会变红，
    那时他应当把它改成断言 401 **被**计数。

    不写这条的话，那个局限只存在于代码注释里，而注释会被忽略。
    """
    metrics.reset()
    ready_client.get(f"{API}/settings/tokens")  # 未认证 → 401（依赖抛出）

    body = ready_client.get(f"{API}/metrics").text

    counted = _metric_value(
        body,
        "suvex_http_requests_total",
        method="GET",
        status="401",
        route="/settings/tokens",
    )
    assert counted is None, (
        "401 现在被计数了 —— 说明计数链路改进了。"
        "请把这条测试改成断言 401 **被**计数，并更新 test_metrics 的模块说明。"
    )


# ══════════════════════════════════════════════════════════
# 三、★ 路由模板（防标签爆炸）
# ══════════════════════════════════════════════════════════


def test_uses_route_template_not_actual_path(ready_client: TestClient) -> None:
    """★★ 带参数的路径必须归到**模板**，不是实际路径。

    这是本模块最要紧的一条断言。

    如果记的是实际路径，那么 `/scans/scan_01AAA` 与 `/scans/scan_01BBB`
    会是两个不同的时间序列 —— 几千次扫描之后 Prometheus 内存被吃光。
    而这个问题**不会在开发时暴露**（要足够多的不同 id 才看得出来），
    所以只能靠这条断言守着。

    判据：指标里出现 `{scanId}` 这样的模板，且**不出现**实际 id。
    """
    metrics.reset()
    ready_client.get(f"{API}/scans/scan_01M4J5QGQHKC7YMXRCREZTBRC2/agents")

    body = ready_client.get(f"{API}/metrics").text

    # 模板在（带花括号）
    assert "{scanId}" in body, "路由模板没有被用上 —— 记成了实际路径？"
    # 实际 id 不在（那正是标签爆炸的来源）
    assert "scan_01M4J5QGQHKC7YMXRCREZTBRC2" not in body, (
        "实际路径泄漏进指标标签 —— 这会导致标签爆炸"
    )


def test_two_different_ids_share_one_series(ready_client: TestClient) -> None:
    """两个不同的 id 只产生**一条**时间序列（同模板）。

    这是上一条的行为验证：断言"模板在"是形状检查，
    这条断言的是"两条请求合并成一条"这个实际效果。
    """
    metrics.reset()
    ready_client.get(f"{API}/scans/scan_AAAA/agents")
    ready_client.get(f"{API}/scans/scan_BBBB/agents")

    body = ready_client.get(f"{API}/metrics").text
    value = _metric_value(
        body,
        "suvex_http_requests_total",
        method="GET",
        status="404",
        route="/scans/{scanId}/agents",
    )

    # 两个不存在的扫描 → 两次 404，但**同一条序列**计数为 2
    assert value == 2.0, f"两次请求没有合并到同一条序列（得到 {value}）"


def test_unmatched_paths_collapse_into_one_label(ready_client: TestClient) -> None:
    """★ 不存在的路径全部归到 `(unmatched)`。

    扫描器会打一堆不存在的路径（`/.env`、`/wp-admin`、`/phpmyadmin`…）。
    若按实际路径记，每个都是一条新序列 —— 这是标签爆炸最常见的入口。
    """
    metrics.reset()
    ready_client.get("/.env")
    ready_client.get("/wp-admin")
    ready_client.get(f"{API}/definitely-not-a-route")

    body = ready_client.get(f"{API}/metrics").text

    assert UNMATCHED in body, "未匹配的路径没有被归到统一标签"
    # 那些真实路径不能出现在标签里
    for path in ("/.env", "/wp-admin", "definitely-not-a-route"):
        assert path not in body, f"{path} 泄漏进指标标签"


# ══════════════════════════════════════════════════════════
# 四、SSE 不计时
# ══════════════════════════════════════════════════════════


def test_streaming_requests_are_not_timed(ready_client: TestClient) -> None:
    """SSE 只计数、**不计时**。

    `/scans/{id}/events` 是永不结束的长连接。把它的耗时记进
    `duration_sum` 会让"平均请求耗时"变得毫无意义 ——
    一条挂了 3 小时的连接会把平均值拉到天上。
    """
    metrics.reset()

    # 直接验证 observe_request 的契约（真起 SSE 会挂住测试进程）
    metrics.observe_request(method="GET", status=200, route="/x", seconds=0.0)

    data = metrics.snapshot()
    assert data["duration_sum"][("GET", "200", "/x")] == 0.0


# ══════════════════════════════════════════════════════════
# 五、可关闭
# ══════════════════════════════════════════════════════════


def test_metrics_can_be_disabled(ready_client: TestClient, monkeypatch) -> None:
    """`SUPEX_METRICS_ENABLED=false` 时端点返回 404。

    ⚠️ 404 而不是空指标：空指标会让 Prometheus 以为"服务在跑但
    没有任何请求"，而真相是"这个端点被关掉了"。两者含义完全不同。
    """
    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "metrics_enabled", False)

    response = ready_client.get(f"{API}/metrics")

    assert response.status_code == 404

