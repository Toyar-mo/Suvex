"""跟随跳转，但**只跟到用户填过的那个主机**。

## 为什么不能直接用 `follow_redirects=True`

那个开关把"跳转目标"交给 httpx 决定，而我们的白名单只校验**用户填的那个
URL** —— 后续每一跳都不过校验。实测过的后果：用户填的地址返回
`302 Location: http://127.0.0.1:<内网端口>/...`，httpx 跟过去，把内网服务的
响应体读出来交给上层。

## 判据是**白名单**，不是"每跳再查一次黑名单"

核心不是"每一跳过一遍元数据校验"，而是：

> **禁止跳转到用户没填过的主机。**

用户填的 host 就是唯一合法的目标。再查一次黑名单仍然是黑名单思路 ——
而黑名单正是"等价 IP 写法"那个漏洞栽的地方（见 `app/core/hostguard.py`）。

同 host 的**协议升级**（`http→https`）刻意放行：它不扩大攻击面
（还是同一个主机、同一个端口策略由目标决定），而真实平台常用 302 做
规范化跳转，禁掉会误伤。

## 与 httpx 自身语义保持一致

自己实现循环就必须复刻 httpx 的规则，否则会**悄悄改变行为**（表现为
"某个平台忽然提交失败"）。这里照抄的是 httpx `_client.py` 的三条：

* `303` → 改用 `GET`（`HEAD` 除外）；
* `302` → 改用 `GET`（`HEAD` 除外）—— 浏览器行为，标准之外；
* `301` + `POST` → 改用 `GET`；
* 换成 `GET` 时丢掉请求体与 `Content-Length` / `Transfer-Encoding`。

`Location` 可以是相对地址（RFC 7231 允许），用 `URL.join` 解析 ——
httpx 的 `_redirect_url` 也是这么做的。
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core import hostguard
from app.ctf2.client import Ctf2Error

#: 跳转状态码。
REDIRECT_CODES: frozenset[int] = frozenset({301, 302, 303, 307, 308})

#: 一跳链最多跟多少跳。
#:
#: 真实平台的规范化跳转通常 1 跳（去尾斜杠、http→https），2 跳已经少见。
#: 给 5 是留余量；超过就说明要么目标在绕圈（A→B→A），要么在故意拉长链
#: 来试探我们的校验 —— 两种都该停下来。
MAX_HOPS = 5


def _next_method(status_code: int, method: str) -> str:
    """跳转之后该用什么方法。与 httpx 的 `_redirect_method` 一致。"""
    upper = method.upper()
    if status_code == 303 and upper != "HEAD":
        return "GET"
    if status_code == 302 and upper != "HEAD":
        return "GET"
    if status_code == 301 and upper == "POST":
        return "GET"
    return upper


def _next_url(current: httpx.URL, response: httpx.Response) -> httpx.URL:
    """解析 `Location`。相对地址按 RFC 7231 用当前 URL 解析。"""
    location = response.headers.get("location")
    if not location:
        raise Ctf2Error("平台返回了跳转，但没有给出 Location 头。")
    try:
        target = httpx.URL(location)
    except Exception as exc:  # noqa: BLE001 - httpx 对畸形 URL 抛的异常类型不固定
        raise Ctf2Error(f"平台给的跳转地址不是一个合法 URL：{exc}") from exc
    # 只有 scheme 没有 host 的畸形 Location（`http:///path`）—— 补上当前 host，
    # 与 httpx 的处理一致，否则它会被当成"跨 host"而误拒。
    if target.scheme and not target.host:
        target = target.copy_with(host=current.host)
    if target.is_relative_url:
        target = current.join(target)
    return target


async def request_same_host(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    base_host: str,
    max_hops: int = MAX_HOPS,
    **kwargs: Any,
) -> httpx.Response:
    """发一次请求，**只在 `base_host` 范围内**跟随跳转。

    `base_host` 是用户填的那个地址的主机（小写、已剥端口）。
    `kwargs` 原样透传给 `client.request`（headers / params / json 等）。

    抛 `Ctf2Error` 的两种情况：
    1. 跳转目标换了主机 —— 那是在打一个用户没填过的地址；
    2. 跳转目标不是 http/https。

    ⚠️ 每一跳都过 `hostguard.is_blocked_host`。这一条**不是**主要防线
    （主要防线是上面那条"禁跨 host"），但它保证：即便用户自己填了一个
    元数据地址（那会在入口被拦），或者 base_host 恰好解析到 link-local，
    也不会在这里被当成合法目标。
    """
    current = httpx.URL(url)
    current_method = method.upper()
    send_kwargs = dict(kwargs)

    for _ in range(max_hops + 1):
        # ① 每一跳都过一遍"明确拒绝的地址"判据（与入口同一份实现）。
        if hostguard.is_blocked_host(current.host):
            raise Ctf2Error(f"平台把请求指向了被拒绝的地址（{current.host}）。")

        response = await client.request(
            current_method, current, follow_redirects=False, **send_kwargs
        )
        if response.status_code not in REDIRECT_CODES:
            return response

        target = _next_url(current, response)

        # ② 协议必须仍是 http(s)：`file://` 之类不能跟。
        if target.scheme not in ("http", "https"):
            raise Ctf2Error(f"平台把请求重定向到了不支持的协议（{target.scheme}）。")

        # ③ ★ 核心判据：**禁止跳转到用户没填过的主机**。
        if (target.host or "").lower() != base_host:
            raise Ctf2Error(
                f"平台把请求重定向到了另一个主机（{target.host}），已拒绝。"
                "平台地址只允许指向你填写的那个主机。"
            )

        # ④ 与 httpx 一致：302/303（以及 301+POST）改成 GET，并丢掉请求体。
        next_method = _next_method(response.status_code, current_method)
        if next_method != current_method:
            current_method = next_method
            send_kwargs.pop("json", None)
            send_kwargs.pop("data", None)
            send_kwargs.pop("content", None)
            headers = dict(send_kwargs.get("headers") or {})
            for name in ("Content-Length", "Transfer-Encoding", "content-length", "transfer-encoding"):
                headers.pop(name, None)
            send_kwargs["headers"] = headers

        current = target

    raise Ctf2Error(f"平台连续跳转超过 {max_hops} 次，已中止（可能在绕圈）。")


__all__ = ["MAX_HOPS", "REDIRECT_CODES", "request_same_host"]
