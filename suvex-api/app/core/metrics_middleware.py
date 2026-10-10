"""HTTP 请求指标中间件（v0.23）。

## ★ 这个中间件最要紧的一件事：拿到**路由模板**，不是实际路径

`/scans/scan_01M4J5QGQHKC7YMXRCREZTBRC2` 与 `/scans/scan_01M4J5RPP…`
在指标里必须是**同一个时间序列**（`route="/api/v1/scans/{scanId}"`）。

不这么做的话，每次扫描都产生一个新的标签值 —— 几千次扫描之后
Prometheus 里就有几千条时间序列，内存被吃光。这是标签爆炸
（cardinality explosion），也是 Prometheus 使用里最经典的事故。

## 怎么拿到模板

FastAPI/Starlette 在**路由匹配之后**把匹配到的路由对象放在
`request.scope["route"]` 里，而 `route.path` 就是模板
（`/api/v1/scans/{scanId}`）。

⚠️ 关键是**时机**：中间件在 `call_next` **之前**拿不到它（那时还没匹配），
   必须在 `call_next` **之后**读。这一点写错的表现是"所有请求的 route
   都是空串"——而它看起来像"路由没匹配上"，排查方向会跑偏。

## 没匹配上路由的请求怎么办

404 的请求（`route` 不在 scope 里）统一归到 `"(unmatched)"` 一个标签。
**不能**用 `request.url.path` —— 那正是标签爆炸的来源：
扫描器打一堆不存在的路径（`/.env`、`/wp-admin`…），每条都会变成新序列。

## 为什么不记 SSE 长连接的耗时

`/scans/{id}/events` 是**永不结束**的长连接（见 `events/stream.py`）。
把它的耗时记进 `duration_sum` 会让"平均请求耗时"这个数字变得毫无意义 ——
一条挂了 3 小时的连接会把平均值拉到天上。

所以对 SSE 路径**只计数、不计时**。判据是响应头 `content-type`
以 `text/event-stream` 开头 —— 用响应头而不是路径白名单，
因为将来新增 SSE 端点时不必记得来改这里。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core import metrics

# 没匹配到任何路由时的标签值。**所有** 404 共用它，见模块 docstring。
UNMATCHED = "(unmatched)"


def _route_template(request: Request) -> str:
    """取路由模板。

    ⚠️ 必须在 `call_next` **之后**调用 —— 那时 `scope["route"]` 才有值。
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) and path else UNMATCHED


class MetricsMiddleware(BaseHTTPMiddleware):
    """记请求计数与耗时。

    ## 为什么用 `BaseHTTPMiddleware` 而不是纯 ASGI 中间件

    纯 ASGI 中间件更轻，但它要自己处理 `http.response.start` /
    `http.response.body` 两条消息，而"拿到状态码与响应头"这件事
    在 `BaseHTTPMiddleware` 里就是 `response.status_code` 一行。

    代价是它对**流式响应**有一层包装（已知会让 SSE 多一层缓冲）。
    ⚠️ 这正是本项目踩过的坑（见 `events/stream.py` 的 `encode_open`）：
    任何在响应链上加一层的东西都可能让响应头被攒住。

    所以这里**不碰 body**（不读、不改、不包装），只读状态码与响应头 ——
    不消费 `response.body_iterator` 就不会改变流式行为。
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        started = time.perf_counter()
        response = await call_next(request)
        elapsed = time.perf_counter() - started

        template = _route_template(request)
        content_type = response.headers.get("content-type", "")
        is_stream = content_type.startswith("text/event-stream")

        metrics.observe_request(
            method=request.method,
            status=response.status_code,
            route=template,
            # SSE 不计时（见模块 docstring）：记 0 而不是记真实耗时，
            # 因为那条连接可能挂几小时，会把平均值彻底带偏。
            seconds=0.0 if is_stream else elapsed,
        )
        return response


__all__ = ["MetricsMiddleware", "UNMATCHED"]
