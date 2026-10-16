"""`ctf2.adapters.redirect`：跟随跳转，但**只跟到用户填过的那个主机**。

## 这组用例守的是什么（RedTeam 发现 2）

平台探测/同步原先用 `httpx.AsyncClient(..., follow_redirects=True)`，
而 `assert_safe_base_url` **只校验用户填的那个 URL** —— 后续每一跳
都不过校验。实测（本地靶子，走生产代码路径）：

    靶子 A 127.0.0.2:18080 → 302 Location: http://127.0.0.1:18081/internal/secret
    靶子 B 127.0.0.1:18081 → {"ok":true,"secret":"RT-INNER-SERVICE-BODY-SECRET"}

    现有实现 → 200，body 里带着 B 的内容   ★ 内网内容泄漏

## ★ 判据是**白名单**，不是"每跳再查一次黑名单"

核心不是"每一跳过一遍元数据校验"，而是：

> **禁止跳转到用户没填过的主机。**

用户填的 host 就是唯一合法的目标。再查一次黑名单仍然是黑名单思路 ——
而黑名单正是发现 1 栽的地方（等价 IP 写法）。

## 不误伤同样要测

真实平台常用 302 做规范化跳转（去尾斜杠、`http→https`）。禁掉会误伤，
而误伤的表现是"某个平台忽然同步不了"，看起来像平台的问题。
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from app.ctf2.adapters.redirect import MAX_HOPS, request_same_host
from app.ctf2.client import Ctf2Error


def _routes(mapping: dict[str, httpx.Response]) -> Any:
    """按 `<scheme>://<host>[:port]<path>` 分发的假平台。

    ⚠️ 键里**必须带端口**：本组用例的核心就是"同 host 不同端口"，
       漏了端口会让"跨端口跳转"被误判成同 host。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        port = f":{request.url.port}" if request.url.port else ""
        key = f"{request.url.scheme}://{request.url.host}{port}{request.url.path}"
        if key in mapping:
            return mapping[key]
        return httpx.Response(404, json={"error": "no route", "key": key}, request=request)

    return handler


def _run(coro: Any) -> Any:
    """跑一个协程。

    ⚠️ **刻意不用** `@pytest.mark.asyncio`：`httpx.AsyncClient` 会绑定到
    创建它的 loop 上，而每个用例只跑一次 `asyncio.run` —— 与
    `test_slabmatch_driver` / `test_generic_driver` 同一条做法。
    """
    return asyncio.run(coro)


async def _request(base: str, mapping: dict[str, httpx.Response], method: str = "GET") -> httpx.Response:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(_routes(mapping)), follow_redirects=False
    )
    try:
        return await request_same_host(
            client, method, base, base_host=httpx.URL(base).host.lower()
        )
    finally:
        await client.aclose()


# ══════════════════════════════════════════════════════════
# 拒绝：跨 host 跳转（发现 2 的正面）
# ══════════════════════════════════════════════════════════


def test_a_cross_host_redirect_is_refused() -> None:
    """★ 发现 2 的正面：跨 host 跳转必须拒绝。

    复刻的是实测那条链：平台在 `127.0.0.2` 上，把请求 302 到
    `127.0.0.1` 的另一个服务。修复前跟过去并把内网响应体读了出来。
    """
    mapping = {
        "http://127.0.0.2:18080/api": httpx.Response(
            302, headers={"location": "http://127.0.0.1:18081/internal/secret"}
        ),
    }

    with pytest.raises(Ctf2Error) as excinfo:
        _run(_request("http://127.0.0.2:18080/api", mapping))

    assert "另一个主机" in excinfo.value.message
    # 报错里要带出**跳到哪去了** —— 排查时那是最有用的一条信息
    assert "127.0.0.1" in excinfo.value.message


def test_a_redirect_to_the_metadata_service_is_refused() -> None:
    """跳进被拒地址也要拦（这是第二道，与"禁跨 host"并存）。

    ⚠️ 它**不是**主要防线：主要防线是"用户没填过的主机一律不跟"。
       留着它是因为 base_host 恰好解析到 link-local 时，这里还能兜一层。
    """
    mapping = {"http://169.254.169.254/latest": httpx.Response(200, json={"creds": "…"})}

    with pytest.raises(Ctf2Error) as excinfo:
        _run(_request("http://169.254.169.254/latest", mapping))

    assert "被拒绝的地址" in excinfo.value.message


def test_a_redirect_loop_is_aborted() -> None:
    """同 host 绕圈（A→A）要中止，而不是无限跟下去。

    真实平台不该这样，但一个**被攻陷的**平台可以 —— 而无限循环会
    把一次同步变成一次挂死。
    """
    mapping = {"http://127.0.0.1:18080/loop": httpx.Response(302, headers={"location": "/loop"})}

    with pytest.raises(Ctf2Error) as excinfo:
        _run(_request("http://127.0.0.1:18080/loop", mapping))

    assert "跳转" in excinfo.value.message
    assert str(MAX_HOPS) in excinfo.value.message


def test_a_redirect_without_a_location_header_is_refused() -> None:
    """给了 302 却没给 `Location` —— 说清楚，而不是当成正常响应返回。"""
    mapping = {"http://127.0.0.1:18080/x": httpx.Response(302)}

    with pytest.raises(Ctf2Error) as excinfo:
        _run(_request("http://127.0.0.1:18080/x", mapping))

    assert "Location" in excinfo.value.message


# ══════════════════════════════════════════════════════════
# 放行：规范化跳转不能误伤（这是"修法不能被平台当成故障"的闸）
# ══════════════════════════════════════════════════════════


def test_a_same_host_normalizing_redirect_still_works() -> None:
    """★ 同 host 的规范化跳转（`/` → `/final`）必须仍然 200。

    真实平台常用 302 去尾斜杠、补路径。禁掉会误伤，而误伤的表现是
    "某个平台忽然同步不了" —— 看起来像平台的问题。
    """
    mapping = {
        "http://127.0.0.1:18080/": httpx.Response(302, headers={"location": "/final"}),
        "http://127.0.0.1:18080/final": httpx.Response(200, json={"ok": True}),
    }

    response = _run(_request("http://127.0.0.1:18080/", mapping))

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_a_same_host_scheme_upgrade_is_allowed() -> None:
    """★ 同 host 的 `http→https` **刻意放行**。

    它不扩大攻击面（还是同一个主机），而真实平台常用它做规范化。
    """
    mapping = {
        "http://ctf2.example.com/api": httpx.Response(
            301, headers={"location": "https://ctf2.example.com/api"}
        ),
        "https://ctf2.example.com/api": httpx.Response(200, json={"ok": True}),
    }

    response = _run(_request("http://ctf2.example.com/api", mapping))

    assert response.status_code == 200


def test_an_internal_platform_is_reachable_directly() -> None:
    """★ 内网靶机直连必须仍然 200（平台部署在内网是**主要**场景）。"""
    mapping = {"http://192.168.1.10:8080/api": httpx.Response(200, json={"ok": True})}

    response = _run(_request("http://192.168.1.10:8080/api", mapping))

    assert response.status_code == 200


def test_a_non_redirect_response_is_returned_as_is() -> None:
    """非 3xx 直接返回，一次多余的跳转都不跟。"""
    mapping = {"http://127.0.0.1:18080/x": httpx.Response(404, json={"error": "nope"})}

    response = _run(_request("http://127.0.0.1:18080/x", mapping))

    assert response.status_code == 404


# ══════════════════════════════════════════════════════════
# 与 httpx 自身语义一致（自己实现循环就必须复刻这些）
# ══════════════════════════════════════════════════════════


def test_a_302_turns_a_post_into_a_get_without_a_body() -> None:
    """★ 302 之后必须改成 GET，且**不能带着请求体**。

    不复刻这条的话，某些平台会在跳转后收到一个带 body 的 GET ——
    而那种请求的失败方式很难归因（平台报的是参数错）。
    """
    seen: list[tuple[str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.content))
        port = f":{request.url.port}" if request.url.port else ""
        key = f"{request.url.scheme}://{request.url.host}{port}{request.url.path}"
        if key == "http://127.0.0.1:18080/submit":
            return httpx.Response(302, headers={"location": "/result"})
        return httpx.Response(200, json={"ok": True})

    async def go() -> httpx.Response:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        )
        try:
            return await request_same_host(
                client,
                "POST",
                "http://127.0.0.1:18080/submit",
                base_host="127.0.0.1",
                json={"flag": "flag{x}"},
            )
        finally:
            await client.aclose()

    response = _run(go())

    assert response.status_code == 200
    assert seen[0][0] == "POST" and seen[0][1], "第一次应当是带 body 的 POST"
    assert seen[1][0] == "GET", "302 之后应当改成 GET"
    assert seen[1][1] == b"", "改成 GET 之后不该再带请求体"


def test_a_307_keeps_the_method_and_body() -> None:
    """307 的语义是"原样重发" —— 方法与 body 都要保留。

    与上一条是一对：只测 302 的话，一个"一律改成 GET"的实现也能通过。
    """
    seen: list[tuple[str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.content))
        port = f":{request.url.port}" if request.url.port else ""
        key = f"{request.url.scheme}://{request.url.host}{port}{request.url.path}"
        if key == "http://127.0.0.1:18080/submit":
            return httpx.Response(307, headers={"location": "/result"})
        return httpx.Response(200, json={"ok": True})

    async def go() -> httpx.Response:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        )
        try:
            return await request_same_host(
                client,
                "POST",
                "http://127.0.0.1:18080/submit",
                base_host="127.0.0.1",
                json={"flag": "flag{x}"},
            )
        finally:
            await client.aclose()

    response = _run(go())

    assert response.status_code == 200
    assert seen[1][0] == "POST", "307 应当保留 POST"
    # ⚠️ httpx 序列化 JSON 时**不带空格**（`separators=(",", ":")`），
    #    所以这里比对的是紧凑形式 —— 不是 `{"flag": "flag{x}"}`。
    assert seen[1][1] == b'{"flag":"flag{x}"}', "307 应当保留请求体"


# ══════════════════════════════════════════════════════════
# ★★ 跳转后的 query 必须来自 Location，而不是重放第一跳的 params
# ══════════════════════════════════════════════════════════


def test_a_redirect_query_comes_from_the_location_not_from_params() -> None:
    """★★ 回归：跳转之后 `Location` 自带的 query **不能被 `params` 顶掉**。

    这是 Review 对抗审查找到的一处真回归（F-3）。原来的实现把
    `send_kwargs`（含 `params`）原样透传到每一跳，于是每跳都重新套一遍
    第一跳的 query —— 实测把 `Location` 的 query 整个顶掉了：

        原始请求 params={page:1}，Location=/after?cursor=FROM_LOCATION&sig=abc
        修复前  第 2 跳 http://example.com/after?page=1                      ← 丢了
        httpx   第 2 跳 http://example.com/after?cursor=FROM_LOCATION&sig=abc

    ⚠️ **触发面比看上去大**：`transport.request` 每次都传
       `query = params + auth.query(key)`，而 query-style 认证的
       `auth.query()` **恒非空**（`spec.py`：`{name: prefix+key}`）——
       所以对 query-style 平台，**每一次跳转都会丢 query**。症状是
       "某个平台跳转之后同步不了"，看起来像平台的问题。
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/start":
            return httpx.Response(
                302,
                headers={"location": "/after?cursor=FROM_LOCATION&sig=abc"},
                request=request,
            )
        return httpx.Response(200, json={"ok": True}, request=request)

    async def go() -> httpx.Response:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        )
        try:
            return await request_same_host(
                client,
                "GET",
                "http://example.com/start",
                base_host="example.com",
                params={"page": "1", "key": "SECRET"},
            )
        finally:
            await client.aclose()

    response = _run(go())

    assert response.status_code == 200
    # 第一跳：params 照旧生效（修法不能把第一跳也改坏）
    assert seen[0] == "http://example.com/start?page=1&key=SECRET"
    # ★ 第二跳：query 完全来自 Location，第一跳的 params 不再参与
    assert seen[1] == "http://example.com/after?cursor=FROM_LOCATION&sig=abc"


def test_a_redirect_without_query_does_not_get_params_replayed() -> None:
    """★ 边界：`Location` **不带** query 时，也不能把第一跳的 params 补回去。

    这一条是与 httpx 对齐的关键：httpx 的 `_build_redirect_request`
    构造下一跳时**根本没有 `params` 这个参数** —— 所以 Location 没有
    query 就是没有，不会"继承"第一跳的。

    ⚠️ 与上一条是一对：只测"Location 有 query"的话，一个
       "把 Location 的 query 和 params 合并"的实现也能通过 ——
       而那个实现同样是错的（httpx 不合并）。
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/after"}, request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    async def go() -> httpx.Response:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        )
        try:
            return await request_same_host(
                client,
                "GET",
                "http://example.com/start",
                base_host="example.com",
                params={"page": "1", "key": "SECRET"},
            )
        finally:
            await client.aclose()

    response = _run(go())

    assert response.status_code == 200
    assert seen[0] == "http://example.com/start?page=1&key=SECRET"
    assert seen[1] == "http://example.com/after", "Location 没有 query 就不该有 query"
