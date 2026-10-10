"""可观测性：进程内指标 + Prometheus 文本格式导出（v0.23）。

## 为什么是"手写 Prometheus 文本"而不是 `prometheus_client`

`prometheus_client` 会带来一个依赖、一套注册表生命周期、以及一个
"指标对象必须模块级单例"的隐式约束（在测试里反复 import 会重复注册）。

而我们要暴露的东西很少，格式本身也简单到可以逐行写：

    # HELP suvex_http_requests_total HTTP 请求总数
    # TYPE suvex_http_requests_total counter
    suvex_http_requests_total{method="GET",status="200"} 42

**零依赖**换来的不只是少一个包：它让这个模块可以在任何地方被调用
（包括没有安装监控栈的开发机），而"导不出指标"这件事本身不会成为故障。

> ⚠️ 如果将来需要直方图（分位数）、多进程聚合、或 pushgateway，
> 那时再换 `prometheus_client` 是合理的 —— 但**不是现在**。
> 现在需要的是"线上出问题时能看到几个关键数字"，而计数器就够。

## 暴露什么、不暴露什么

**暴露**（都是"出事时第一个要看的"）：

* HTTP 请求计数（按方法 + 状态码 + 路由模板）
* 请求耗时**总量**（不是直方图 —— 平均耗时能从 sum/count 算出来）
* 扫描的当前状态分布（queued / running / …）
* 数据库连通性

**刻意不暴露**：

* **不做按 URL 原始路径的标签**。`/scans/scan_01M4J…` 每个 id 都是
  一个新时间序列，几千次扫描会把内存撑爆（这是 Prometheus 使用里
  最经典的"标签爆炸"）。所以用**路由模板**（`/scans/{scanId}`）。
* 不暴露任何业务数据内容（漏洞标题、目标地址、凭据）。
  指标端点是 public 的（监控系统没有凭据），所以它只能放**数字**。

## 为什么指标端点是 public

与 `/health` 同一条理由：抓取方是 Prometheus，它没有我们的凭据。
代价是"任何人都能看到请求量" —— 而那是可接受的（它不泄露业务数据），
换来的是"监控能用"。

⚠️ 生产环境如果不想公开它，两个做法（都比"加个 token 参数"好）：
  · 在反代上限制 `/metrics` 只允许监控网段访问；
  · 或配 `SUPEX_METRICS_ENABLED=false` 直接关掉。
"给指标端点加个静态 token"是最差的做法 —— 它会被写进抓取配置里，
而那份配置往往比指标本身更容易泄漏。
"""

from __future__ import annotations

import threading
from collections import Counter
from typing import Any

# ─────────────────────────────────────────────
# 指标存储
# ─────────────────────────────────────────────
#
# ⚠️ 全部用 `threading.Lock` 保护。
#
# FastAPI 的同步端点跑在线程池里（不是单线程），而 `Counter` 的
# `+=` 不是原子操作（读-改-写三步）。并发下的表现是**计数偏少** ——
# 而那正是最难发现的一类错误：数字看起来"有点小"，但没人知道少了多少。
#
# 用锁而不是 `collections.defaultdict` + GIL 侥幸：GIL 只保证单个
# 字节码原子，不保证 `d[k] = d[k] + 1` 原子。
_lock = threading.Lock()

# (method, status, route_template) → 次数
_requests: Counter[tuple[str, str, str]] = Counter()
# (method, status, route_template) → 总耗时（秒）
_duration_sum: Counter[tuple[str, str, str]] = Counter()
# 进程启动以来的请求总数（不受标签维度影响，用于快速看一眼）
_total = 0


def observe_request(
    *, method: str, status: int, route: str, seconds: float
) -> None:
    """记一次 HTTP 请求。

    `route` 必须是**路由模板**（`/scans/{scanId}`），不是实际路径。
    传实际路径会导致标签爆炸 —— 见模块 docstring。
    """
    global _total
    key = (method, str(status), route)
    with _lock:
        _requests[key] += 1
        _duration_sum[key] += seconds
        _total += 1


def snapshot() -> dict[str, Any]:
    """当前指标的一份拷贝。给导出用，也给测试用。"""
    with _lock:
        return {
            "total": _total,
            "requests": dict(_requests),
            "duration_sum": dict(_duration_sum),
        }


def reset() -> None:
    """清空全部指标。**只给测试用** —— 进程内没有别的场景需要它。"""
    global _total
    with _lock:
        _requests.clear()
        _duration_sum.clear()
        _total = 0


# ─────────────────────────────────────────────
# Prometheus 文本导出
# ─────────────────────────────────────────────


def _escape_label(value: str) -> str:
    """转义标签值。Prometheus 文本格式要求转义 `\\` `"` 与换行。"""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(method: str, status: str, route: str) -> str:
    """渲染标签串。

    ★ **标签顺序固定为 method → status → route**（字母序）。

    为什么不按元组的自然顺序：Prometheus 里同一条时间序列的标签顺序
    必须**稳定**，否则同一个 (method, status, route) 组合在两次导出里
    可能写出不同顺序的行 —— 而抓取端会把它当成两条不同的序列。

    实现上就是把顺序写死在这里，而不是依赖调用方传参的顺序。
    """
    return (
        f'method="{_escape_label(method)}",'
        f'status="{_escape_label(status)}",'
        f'route="{_escape_label(route)}"'
    )


def render(*, extra: dict[str, float] | None = None) -> str:
    """渲染成 Prometheus 文本格式。

    `extra` 是调用方现算的即时值（如"当前 running 的扫描数"）——
    它们不适合放进进程内的计数器（那要维护状态），而是**导出时读一次库**。
    这样指标永远是"此刻的真实值"，不会与数据库漂移。
    """
    lines: list[str] = []
    data = snapshot()

    lines.append("# HELP suvex_http_requests_total HTTP 请求总数")
    lines.append("# TYPE suvex_http_requests_total counter")
    for (method, status, route), count in sorted(data["requests"].items()):
        lines.append(f"suvex_http_requests_total{{{_labels(method, status, route)}}} {count}")
    # 一个请求都没有时也要输出 HELP/TYPE（否则 Prometheus 认为这个指标不存在）
    if not data["requests"]:
        lines.append("# （还没有请求）")

    lines.append("# HELP suvex_http_request_duration_seconds_sum 请求耗时总和")
    lines.append("# TYPE suvex_http_request_duration_seconds_sum counter")
    for (method, status, route), total in sorted(data["duration_sum"].items()):
        lines.append(
            f"suvex_http_request_duration_seconds_sum{{{_labels(method, status, route)}}} "
            f"{total:.6f}"
        )

    lines.append("# HELP suvex_http_requests_observed 进程内观察到的请求总数")
    lines.append("# TYPE suvex_http_requests_observed counter")
    lines.append(f"suvex_http_requests_observed {data['total']}")

    for name, value in sorted((extra or {}).items()):
        lines.append(f"# TYPE suvex_{name} gauge")
        lines.append(f"suvex_{name} {value}")

    return "\n".join(lines) + "\n"


__all__ = ["observe_request", "render", "reset", "snapshot"]
