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

from app.kernel.adapters.redirect import MAX_HOPS, request_same_host
from app.kernel.client import Ctf2Error


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


async def _request(
    base: str,
    mapping: dict[str, httpx.Response],
    method: str = "GET",
    base_host: str | None = None,
) -> httpx.Response:
    """发一次请求。`base_host` 缺省取 `base` 的 host。

    ⚠️ 显式传 `base_host` 的场合：`base` 自己带着越界端口时，
    `httpx.URL(base).host` 仍能取到 host（端口越界不影响 `.host`），
    但为了让用例的意图清楚（"用户填的主机是这个"），允许调用方指定。
    """
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(_routes(mapping)), follow_redirects=False
    )
    try:
        return await request_same_host(
            client,
            method,
            base,
            base_host=base_host if base_host is not None else (httpx.URL(base).host or "").lower(),
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


# ══════════════════════════════════════════════════════════
# ★★ 畸形 Location 不许逃逸成非领域异常（F-08）
# ══════════════════════════════════════════════════════════


#: 畸形 `Location` → 都必须在**调用点接得住**的范围内收场
#: （`Ctf2Error` 或 `httpx.HTTPError` 的子类），不允许冒出别的东西。
#:
#: ⚠️ 这一组是**在 Kali（生产同款 Linux）上定出来的**。第一版我在本机
#: Windows 测，得到"httpx 返回 502、接得住"——**那个结论是错的**：
#: 越界端口在 Linux 上走 anyio 的 task group，冒出
#: `ExceptionGroup[OverflowError]`，而它**不是** `HTTPError` 的子类，
#: 生产调用点（`transport.py` / `probe.py` 的 `except httpx.HTTPError`）接不住。
MALFORMED_LOCATIONS = [
    # ★ 端口越界：ExceptionGroup[OverflowError] 的来源
    "http://127.0.0.1:99999/x",
    "http://127.0.0.1:65536/x",
    "http://127.0.0.1:70000/x",
    "http://127.0.0.1:100000/x",
    "http://127.0.0.1:4294967296/x",
    "https://127.0.0.1:99999/x",
    "http://:99999/x",
    "http://[::1]:99999/x",
    # ★ 协议异常：httpx 内部解析时会抛 InvalidURL（不是 HTTPError）
    "javascript:alert(1)",
    "data:text/html,x",
    # 其它畸形形态
    "http://[::1/x",
    "http://:80/x",
    "ftp://127.0.0.1/x",
    "file:///etc/passwd",
    "   ",
    # ★★ R-4：IDNA 惰性解析这一族（`idna.IDNAError` / `InvalidCodepoint`）。
    #
    # 它们与上面那些**机制完全不同**：`httpx.URL(loc)` 的构造**不抛**
    # （惰性），要到第一次访问 `.host` 时才抛（`httpx/_urls.py:191`
    # `host = idna.decode(host)`）。而 `idna.IDNAError` 既不是
    # `httpx.InvalidURL` 也不是 `httpx.HTTPError` 的子类 —— 它只是
    # `ValueError` 的子类，所以 R-1 删掉裸 `ValueError` 之后没人接它。
    #
    # ⚠️ 这一族是**唯一**能证明 `_LIBRARY_URL_ERRORS` 必须含
    # `idna.IDNAError` 的输入；上面那些由 `InvalidURL` / `OverflowError`
    # 覆盖，删掉 `idna.IDNAError` 它们照样绿。
    #
    # 两条不同的可达路径都放进来（Kali 实测各自都真的抛，但**抛点不同**）：
    # * 绝对地址 —— httpx 的 `_redirect_url` 走到
    #   `if url.scheme and not url.host` 时碰 `.host`（`_client.py:532`）；
    # * 协议相对地址（`//host/x`）—— `scheme` 是空串，上面那句**短路**，
    #   `_redirect_url` 自己不抛；抛点在紧随其后的
    #   `_redirect_headers:552` → `_same_origin:89`。
    #   ⚠️ 不是 `request.url.join(url)` —— 实测 `join` 对 `.host` 求值 0 次
    #   （它只做字符串拼装）。两者是**不同函数**，所以两类都要有用例。
    "http://xn--.invalid/x",
    "http://xn--0.invalid/x",
    "http://xn--a.invalid/x",
    "//xn--.invalid/x",
    "//xn--a.invalid/x",
    "ftp://xn--.invalid/x",
]


@pytest.mark.parametrize("location", MALFORMED_LOCATIONS)
def test_a_malformed_location_never_escapes_as_a_non_domain_error(location: str) -> None:
    """★★ 畸形 `Location` 必须以领域异常收场，**不许冒出 `ExceptionGroup` / `InvalidURL`**。

    这是 F-08 的回归闸。那两类都不是 `httpx.HTTPError` 的子类：

    * `ExceptionGroup`（anyio task group 包住连接期的 `OverflowError`）
      —— 端口越界时出现；
    * `httpx.InvalidURL`（直接继承 `Exception`）—— `javascript:` / `data:`
      那类"有 scheme、无 host、path 不以 `/` 开头"的写法，httpx 在
      `_redirect_url` 里 `copy_with(host=…)` 时抛。

    两者的后果一样：`transport.py` 与 `probe.py` 的
    `except httpx.HTTPError` 接不住 → 用户看到 500。

    ⚠️ `probe.py` 那处**也**接 `Ctf2Error`（它把不合格的候选端点跳过），
       所以这组用例对两条调用点都成立。
    """
    mapping = {
        "http://127.0.0.1:18080/start": httpx.Response(
            302, headers={"location": location}
        ),
    }

    try:
        response = _run(_request("http://127.0.0.1:18080/start", mapping))
    except (Ctf2Error, httpx.HTTPError):
        # Ctf2Error：probe 会换下一个候选端点；HTTPError：transport 转成
        # Ctf2Unavailable。两者都是**调用点接得住**的，过关。
        return
    except BaseException as exc:  # noqa: BLE001
        raise AssertionError(
            f"Location={location!r} 冒出了领域异常之外的 "
            f"{type(exc).__name__}，调用点的 except 接不住 → 会变成 500"
        ) from exc

    # 未抛也算过关：说明这一次被当成正常响应处理了。本用例只守
    # "不许有非领域异常逃逸"这一件事。
    assert response.status_code >= 0


def test_a_relative_location_is_still_followed() -> None:
    """★ 反向闸：**相对** `Location`（`/final`）必须仍然被跟随。

    这条钉住一个我实测踩到的坑：给"畸形 Location"加校验时，判据若写成
    `scheme not in ("http","https")`，相对地址（`scheme` 是**空串**）
    会被当成"不支持的协议"全部拒掉 —— 实测让 6 个既有用例挂掉。
    相对地址是 RFC 7231 允许的，真实平台也常用。

    ⚠️ 与上面那组是一对：只测"畸形都被拦"的话，一个"把什么都拒掉"的
       实现也能通过。
    """
    mapping = {
        "http://127.0.0.1:18080/": httpx.Response(302, headers={"location": "/final"}),
        "http://127.0.0.1:18080/final": httpx.Response(200, json={"ok": True}),
    }

    response = _run(_request("http://127.0.0.1:18080/", mapping))

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_an_out_of_range_port_is_refused_with_a_domain_error() -> None:
    """★ 端口越界要以**可读的领域异常**收场，而不是被连接层炸掉。

    它同时证明 `_check_port` 在起作用：错误信息里带得出那个越界端口，
    而不是 `ExceptionGroup` 那串 `unhandled errors in a TaskGroup`。
    """
    mapping = {
        "http://127.0.0.1:18080/start": httpx.Response(
            302, headers={"location": "http://127.0.0.1:99999/x"}
        ),
    }

    with pytest.raises(Ctf2Error) as excinfo:
        _run(_request("http://127.0.0.1:18080/start", mapping))

    assert "端口" in excinfo.value.message
    assert "99999" in excinfo.value.message


def test_an_out_of_range_port_in_the_base_url_is_refused() -> None:
    """★ 入口（`base_url`）带越界端口也要拦 —— 不只 `Location` 这一处。

    `transport` 拼的是 `base_url + path`，所以第一跳的 URL 直接带着
    管理员填的端口。实测把 base_url 配成 `http://127.0.0.1:99999` 时，
    第一跳就冒出 `ExceptionGroup[OverflowError]`。
    """
    mapping = {"http://127.0.0.1:18080/x": httpx.Response(200, json={"ok": True})}

    with pytest.raises(Ctf2Error) as excinfo:
        _run(
            _request(
                "http://127.0.0.1:99999/x",
                mapping,
                base_host="127.0.0.1",
            )
        )

    assert "端口" in excinfo.value.message


# ══════════════════════════════════════════════════════════
# ★★ R-1：判据收窄（只吞"真的传输层"，编程错误必须上抛）
# ══════════════════════════════════════════════════════════


def test_a_plain_value_error_is_not_swallowed() -> None:
    """★★ 我们自己抛的 `ValueError` 必须**原样上抛**，不许变成 `Ctf2Error`。

    ## 这条钉的是判据收窄（R-1）

    原判据里有裸 `ValueError` / `OSError` / `ArithmeticError`，而这三个
    **同时是编程错误的常见类型**（解包失败、`int()` 失败、除零）——
    整段 `client.request(...)` 都在那层包装里，于是我们自己的 bug
    会被报成「平台给的跳转地址无法使用」。那正是该函数 docstring 里
    "别用平台的问题盖住真 bug"要防的事。

    实测依据：当初那 21 种畸形 `Location` 里裸 `ValueError` 出现 **0** 次，
    所以这一档看起来没有任何正当用途。

    ⚠️ **R-4 更正**：那句"0 次"是**错的** —— 它漏了 **IDNA 惰性解析**
    那一族（`idna.IDNAError` / `InvalidCodepoint` 都是 `ValueError` 的子类，
    只是既不是 `InvalidURL` 也不是 `HTTPError`）。所以裸 `ValueError`
    **不能加回来**（`UnicodeDecodeError` 那一类解码 bug 会被一起吞掉），
    正确做法是精确收 `idna.IDNAError` —— 见 `redirect.py` 的
    `_LIBRARY_URL_ERRORS`。本条用例守的判据本身没变：**我们自己抛的
    `ValueError` 仍必须原样上抛**。

    ## 怎么构造出"我们自己的 `ValueError`"

    在 `client.request` 的路径上插一个**会在请求里抛 `ValueError`** 的
    transport —— 它模拟的正是"我们这边的代码坏了"。
    真实场景举例：某个 helper 在拼 URL 时 `int()` 一个非数字字符串。
    """
    def broken_handler(request: httpx.Request) -> httpx.Response:
        raise ValueError("解包失败——这是我们自己的 bug，不是平台的问题")

    async def go() -> Any:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(broken_handler), follow_redirects=False
        )
        try:
            return await request_same_host(
                client, "GET", "http://example.com/x", base_host="example.com"
            )
        finally:
            await client.aclose()

    with pytest.raises(ValueError) as excinfo:
        _run(go())

    assert not isinstance(excinfo.value, Ctf2Error), (
        "我们自己抛的 `ValueError` 被吞成了 `Ctf2Error` —— 判据又把裸 "
        "`ValueError` 收进去了。那会用一个'平台的问题'盖住一个真 bug。"
    )


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("解包失败"),
        KeyError("配置项缺失"),
        TypeError("参数类型不对"),
        ZeroDivisionError("除零"),
        AttributeError("没有那个属性"),
        RuntimeError("不该到这里"),
        FileNotFoundError("/etc/不存在"),
        PermissionError("没权限"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_programming_errors_are_never_reported_as_platform_problems(exc) -> None:
    """★★ 一类编程错误整体不得被吞成 `Ctf2Error`（R-1 的判据表）。

    ⚠️ 这里**直接打 `_is_transport_level`** 而不是绕一圈构造请求：
    这一组要穷举的是**判据本身**，而"怎么把某个异常从 httpx 里引出来"
    与判据无关。绕一圈只会让用例更长、更难读，且覆盖的还是同一批判断。

    ⚠️ 其中 `FileNotFoundError` / `PermissionError` 是**收窄的关键项**：
    它们都是 `OSError` 子类，原判据（裸 `OSError`）会吞掉它们 ——
    而在这个上下文里它们只可能是我们自己的 bug（去读一个不存在的
    路径），连接层的失败永远被 httpx 包成 `ConnectError`，不会以裸
    `OSError` 冒出来。
    """
    from app.kernel.adapters.redirect import _is_transport_level

    assert _is_transport_level(exc) is False, (
        f"{type(exc).__name__} 被判成「传输层错误」了 —— 它会被转成 "
        "`Ctf2Error`，于是我们自己的 bug 变成「平台给的地址不可用」。"
    )


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionError("连接失败"),
        ConnectionRefusedError("拒绝连接"),
        OverflowError("端口越界"),
        httpx.ConnectError("连不上"),
        httpx.ConnectTimeout("超时"),
        httpx.UnsupportedProtocol("ftp://"),
        httpx.RemoteProtocolError("协议错"),
        httpx.InvalidURL("畸形 URL"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_real_transport_failures_are_still_swallowed(exc) -> None:
    """★ 反向闸：**真的**传输层错误必须仍然被转成 `Ctf2Error`。

    ⚠️ 与上面那组是一对。只测"编程错误上抛"的话，一个
    `return False` 的实现也能全过 —— 而那样 F-08 的 9 条逃逸路径
    会全部重现（调用点接不住 → 500）。

    这几个类型都是实测出现的：`ConnectionError` 是连接层面的确切类型；
    `OverflowError` 是端口越界的真身（`socket.connect` 对 >65535 抛它）；
    `InvalidURL` / 其余 `HTTPError` 族在 Kali 上各有对应的触发写法。
    """
    from app.kernel.adapters.redirect import _is_transport_level

    assert _is_transport_level(exc) is True, (
        f"{type(exc).__name__} 被判成「不是传输层错误」了 —— 它会原样逃逸，"
        "而调用点的 `except httpx.HTTPError` 接不住 → 用户看到 500。"
    )


def test_a_group_of_gaierror_leaves_is_swallowed() -> None:
    """★ `ExceptionGroup[gaierror]` 必须被吞 —— 这是收窄时唯一的"险"项。

    ## 为什么单独钉它

    `socket.gaierror`（DNS 解析失败）是**唯一**不在 `ConnectionError`
    之下的网络类 `OSError`：

        issubclass(gaierror, OSError)        = True
        issubclass(gaierror, ConnectionError) = False   ★

    所以把裸 `OSError` 收窄成 `ConnectionError` 时，**必须**显式把
    `gaierror` 一起留着 —— 否则 anyio 直连 DNS 失败时抛的
    `ExceptionGroup[gaierror]` 会逃逸成 500（实测 `anyio.connect_tcp`
    对不存在的域名抛的就是 `gaierror`）。
    """
    import socket

    from app.kernel.adapters.redirect import _is_transport_level

    group = ExceptionGroup("unhandled errors in a TaskGroup", [socket.gaierror("名字解析失败")])

    assert _is_transport_level(group) is True, (
        "`ExceptionGroup[gaierror]` 没被吞 —— `gaierror` 不是 "
        "`ConnectionError` 子类，收窄时必须显式留着它，否则 DNS 失败会 500。"
    )


def _redirect_go(url: str) -> Any:
    """走**真实** transport 请求一次（`follow_redirects=False`，只看这一跳）。"""

    async def run() -> Any:
        client = httpx.AsyncClient(follow_redirects=False, timeout=4.0, trust_env=False)
        try:
            return await request_same_host(
                client, "GET", url, base_host=httpx.URL(url).host or ""
            )
        finally:
            await client.aclose()

    return _run(run())


def _redirect_go_raising_overflow(url: str) -> Any:
    """走 `MockTransport`，**主动**抛 `OverflowError`。

    为什么不能靠"打一个越界端口、等连接层自己炸"：那依赖 `getaddrinfo`
    对越界端口抛 `OverflowError`，而那是 **Linux 的行为**。Windows 上是
    **静默截断**（实测 `getaddrinfo("127.0.0.1", 99999)` 返回
    `('127.0.0.1', 34463)`，端口 `99999 % 65536`），于是抛的是
    `httpx.ConnectError` 而不是 `OverflowError` —— 靠它测兜底，
    在 Windows 上测的其实是别的路径，还测过了。
    """

    def raise_overflow(request: httpx.Request) -> httpx.Response:
        raise OverflowError("port must be 0-65535")

    async def run() -> Any:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(raise_overflow),
            follow_redirects=False,
            timeout=4.0,
            trust_env=False,
        )
        try:
            return await request_same_host(
                client, "GET", url, base_host=httpx.URL(url).host or ""
            )
        finally:
            await client.aclose()

    return _run(run())


def _without_check_port() -> Any:
    """临时把 `_check_port` 改成空实现，返回一个恢复函数。"""
    from app.kernel.adapters import redirect as redirect_mod

    real = redirect_mod._check_port

    def _noop(target: Any, *, what: str) -> None:
        return None

    redirect_mod._check_port = _noop
    return lambda: setattr(redirect_mod, "_check_port", real)


def test_the_overflow_fallback_is_the_second_line() -> None:
    """★★ `OverflowError` 兜底是**第二道**防线，它必须独立生效。

    ## 为什么必须有这条（主管点名要的）

    否则会出现"靠 `OverflowError` 兜底"的**隐式依赖** —— 那种依赖在
    换成同步 client、或 httpx/anyio 改版时会静默失效。

    ## ★ 一个必须说清的事实：既有用例证明不了这一层

    `test_redirect_guard.py` 的畸形 `Location` 用例（R-4 后 21 条）走的是
    `httpx.MockTransport`，而 mock **永远不会真的连接** ——
    所以 `OverflowError` 兜底在那些用例里**根本不可测**。
    实测：在 MockTransport 下把 `_check_port` 去掉，越界端口
    一律"返回 200"，一条都不会被兜底拦住。那些用例实际守住的是
    `_check_port`（和 `InvalidURL` 那一档）。

    ## 这条只测**兜底逻辑本身**，与 OS 无关

    用 `MockTransport` 主动抛 `OverflowError`，把 `_check_port` 去掉后
    它必须仍然以 `Ctf2Error` 收场。"真实连接层是不是真的抛
    `OverflowError`"是另一件事，见
    `test_the_real_transport_really_raises_overflow_on_out_of_range_port`。
    """
    restore = _without_check_port()
    try:
        with pytest.raises(Ctf2Error) as excinfo:
            _redirect_go_raising_overflow("http://127.0.0.1:99999/x")
        assert "无法使用" in excinfo.value.message, (
            "连接层抛 `OverflowError` 后没有转成 `Ctf2Error` —— "
            "第二道防线失效了（调用点的 except 接不住 ExceptionGroup → 500）"
        )
    finally:
        restore()


def test_overflow_is_recognised_bare_and_wrapped_in_an_exception_group() -> None:
    """分类函数本身要对：裸的与 anyio 包过的两种形态都要认得。

    anyio 的 task group 会把连接的 `OverflowError` 包成
    `ExceptionGroup`，只判裸异常会让它逃逸成 500。
    """
    from app.kernel.adapters.redirect import _is_transport_level

    assert _is_transport_level(OverflowError()), (
        "裸 `OverflowError` 没被归类成传输层错误 —— 兜底第一道判断就失效了"
    )
    assert _is_transport_level(
        BaseExceptionGroup("unhandled errors in a TaskGroup", [OverflowError()])
    ), "anyio 的 task group 会把连接的 OverflowError 包成 ExceptionGroup，必须认得"


def test_the_real_transport_really_raises_overflow_on_out_of_range_port() -> None:
    """越界端口在**真实连接层**上确实抛 `OverflowError`（只在 Linux 成立）。

    这条与前一条分工：前一条测"就算抛了 `OverflowError`，兜底能不能接住"
    （逻辑，OS 无关）；这条测"真实连接层到底抛不抛"（OS 行为）。

    在 Windows 上 `getaddrinfo("127.0.0.1", 99999)` 不抛，而是**静默截断**
    成 `('127.0.0.1', 34463)`，于是抛 `ConnectError` —— 在这里断言
    等于测了别的东西，所以如实 skip，而不是假装它生效了。
    """
    import os

    if os.name != "posix":
        pytest.skip(
            "真实连接层的越界端口行为只在 Linux 上可验 —— Windows 的 "
            "getaddrinfo 会把 99999 静默截断成 34463，抛的是 ConnectError "
            "而不是 OverflowError，在这里断言等于测别的东西"
        )

    restore = _without_check_port()
    try:
        with pytest.raises(Ctf2Error) as excinfo:
            _redirect_go("http://127.0.0.1:99999/x")
        assert "无法使用" in excinfo.value.message, (
            "越界端口在去掉 `_check_port` 后没有由 `OverflowError` 兜底拦住 —— "
            "第二道防线失效了（调用点的 except 接不住 ExceptionGroup → 500）"
        )
    finally:
        restore()


def test_zero_port_is_caught_by_check_port_not_by_the_overflow_fallback() -> None:
    """★ 对照：`:0` 是**非溢出语义**的越界，兜底管不了，只能靠 `_check_port`。

    这个对照划清两层各自管什么：
    `_check_port` 管"语义上不合法"，`OverflowError` 只管"连接层真的炸了"。
    """
    restore = _without_check_port()
    try:
        try:
            _redirect_go("http://127.0.0.1:0/x")
        except Ctf2Error as exc:
            raise AssertionError(
                "`:0` 在去掉 `_check_port` 后就被拦住了？那说明它走的不是 "
                "兜底路径 —— 这个对照的前提不成立，请重看两层分工。"
                f"（实际报错：{exc.message}）"
            ) from exc
        except BaseException:  # noqa: BLE001
            pass  # 连接层报什么都行，重点是**没有**被 `_check_port` 拦
    finally:
        restore()

    with pytest.raises(Ctf2Error) as excinfo:
        _redirect_go("http://127.0.0.1:0/x")
    assert "端口越界" in excinfo.value.message, (
        "`:0` 必须由 `_check_port` 的区间比较拦住（它是非溢出语义的越界，"
        "连接层不会抛 `OverflowError`）—— 这正是 `_check_port` 必须在"
        "连接之前跑的理由"
    )
