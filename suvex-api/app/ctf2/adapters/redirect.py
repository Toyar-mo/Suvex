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
"某个平台忽然提交失败"）。这里照抄的是 httpx `_client.py` 的四条：

* `303` → 改用 `GET`（`HEAD` 除外）；
* `302` → 改用 `GET`（`HEAD` 除外）—— 浏览器行为，标准之外；
* `301` + `POST` → 改用 `GET`；
* 换成 `GET` 时丢掉请求体与 `Content-Length` / `Transfer-Encoding`。

`Location` 可以是相对地址（RFC 7231 允许），用 `URL.join` 解析 ——
httpx 的 `_redirect_url` 也是这么做的。

## ★★ 为什么 `params` 只在第一跳生效（这一条曾经写错）

httpx 的 `_build_redirect_request` 构造下一跳时**只带 method / url /
headers / stream**，**没有 `params`** —— 也就是说：**下一跳的 query 完全
来自 `Location` 自己带的那些**，第一跳的 `params` 不参与。

我们原来把 `send_kwargs`（含 `params`）原样透传到每一跳，于是每跳都
重新套一遍第一跳的 query，把 `Location` 自带的 query **顶掉**。实测：

    原始请求 params={page:1}，Location=/after?cursor=FROM_LOCATION&sig=abc
    我们（错）  第 2 跳 http://example.com/after?page=1
    httpx 原生  第 2 跳 http://example.com/after?cursor=FROM_LOCATION&sig=abc

**触发面比看上去大**：`transport._request` 每次都传
`query = params + auth.query(key)`，而 **query-style 认证的
`auth.query()` 恒非空**（`spec.py` 里是 `{name: prefix+key}`）——
所以对 query-style 平台，**每一次跳转都会丢 query**。症状是"某个平台
跳转之后同步不了"，看起来像平台的问题，实际是我们自己改的。

修法就是与 httpx 对齐：第一跳用调用方的 `params`，**之后每跳不再带
`params`**，query 由 `Location` 决定。
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


#: TCP 端口的合法区间。
MIN_PORT = 1
MAX_PORT = 65535


def _check_port(target: httpx.URL, *, what: str) -> None:
    """`Location` 里的端口越界时**提前**抛 `Ctf2Error`。

    ## ★ 为什么必须在这里拦，而不是指望调用点的 `except`

    `httpx.URL("http://127.0.0.1:99999/x")` **解析不报错**（`.port` 就是
    `99999`），错误发生在**连接阶段**：`socket.connect` 抛 `OverflowError`。
    而异步 httpx 走 anyio 的 task group，这个 `OverflowError` 会被包成
    **`ExceptionGroup`**（`unhandled errors in a TaskGroup`）——
    它**不是** `httpx.HTTPError` 的子类，所以生产调用点的
    `except httpx.HTTPError` 接不住，请求会变成 500。

    ⚠️ 这一条**实测过两次、结论必须按环境区分**（我第一版在本机 Windows 测，
    得到的是"httpx 返回 502、接得住"，**那个结论是 Windows 特有的、错的**）：
    在 Kali（生产同款 Linux，httpx 0.28.1 / anyio 4.15.1）上，
    `_probe()` 的靶子返回 `302 Location: http://127.0.0.1:99999/x` 时，
    跨 host 检查放行（同 host），真去连，冒出
    `ExceptionGroup[OverflowError]`，**两个调用点都接不住**。
    逐层对照见 `docs/security-checklist.md` 的 F-08。

    所以端口在校验层就当"平台给了一个不可用的地址"处理 —— 这与本模块
    "逐跳校验、不合格就抛 `Ctf2Error`"的形状一致。
    """
    port = target.port
    if port is None:
        return
    if not (MIN_PORT <= port <= MAX_PORT):
        raise Ctf2Error(
            f"{what}里的端口越界（{port}，合法区间 {MIN_PORT}-{MAX_PORT}）。"
            "平台给的地址不可用。"
        )


#: 我们自己实现跳转循环，但**httpx 仍然会解析一次 `Location`** ——
#: `client.request(..., follow_redirects=False)` 内部走的是
#: `_send_handling_redirects`，它**先**调 `_build_redirect_request`
#: （`_client.py` 里那两行的顺序是：先 build，再判 `follow_redirects`），
#: 所以一个畸形 `Location` 会在 `client.request()` 里面就抛出来 ——
#: 我们的 `_next_url` 根本轮不到执行。
#:
#: 实测（Kali）会从这里逃出来的两类：
#:
#: * `httpx.InvalidURL` —— `javascript:alert(1)` / `data:text/html,x` 这种
#:   "有 scheme、无 host、path 不以 `/` 开头"的写法，httpx 在
#:   `_redirect_url` 里 `copy_with(host=…)` 时抛。⚠️ `InvalidURL`
#:   **不是** `HTTPError` 的子类（它直接继承 `Exception`）。
#: * `ExceptionGroup` —— anyio 的 task group 把连接期的 `OverflowError`
#:   （端口越界）包起来。同样不是 `HTTPError`。
#:
#: 这两类在调用点（`transport.py` / `probe.py` 的 `except httpx.HTTPError`）
#: 都接不住 → 请求变成 500。所以在这里统一转成 `Ctf2Error`。
_LIBRARY_URL_ERRORS: tuple[type[BaseException], ...] = (httpx.InvalidURL,)


def _leaf_exceptions(exc: BaseException) -> list[BaseException]:
    """把（可能嵌套的）异常组摊平成叶子异常。"""
    if isinstance(exc, BaseExceptionGroup):
        leaves: list[BaseException] = []
        for sub in exc.exceptions:
            leaves.extend(_leaf_exceptions(sub))
        return leaves
    return [exc]


def _is_transport_level(exc: BaseException) -> bool:
    """这个异常是不是"网络/地址本身不可用"那一类。

    ★ 只把这一类转成 `Ctf2Error`。**不要把 `ExceptionGroup` 无条件吞掉** ——
    它还可能是我们自己代码里的编程错误（那会被 task group 一起包起来），
    吞掉它等于用一个"平台的问题"盖住一个真 bug。判据是"每一条叶子都是
    连接/地址层面的错误"：
    `OSError`（连接失败）、`ArithmeticError`（端口越界 = `OverflowError`）、
    `ValueError`、`httpx` 自己的 URL 异常（`_LIBRARY_URL_ERRORS`）、
    以及 `HTTPError` 全族。
    """
    if isinstance(exc, BaseExceptionGroup):
        leaves = _leaf_exceptions(exc)
        return bool(leaves) and all(_is_transport_level(leaf) for leaf in leaves)
    return isinstance(
        exc, (OSError, ArithmeticError, ValueError, httpx.HTTPError, *_LIBRARY_URL_ERRORS)
    )


def _next_url(current: httpx.URL, response: httpx.Response) -> httpx.URL:
    """解析 `Location`。相对地址按 RFC 7231 用当前 URL 解析。

    ⚠️ 校验的**顺序**在这里是有意义的，不能随便调换：

    1. 先解析（解析失败 → `Ctf2Error`）；
    2. **先判协议**，再补 host —— 这样 `copy_with(host=…)` 只会作用在
       http(s) 上（相对地址另说，见 docstring 末尾那条）。
    3. 判端口越界（见 `_check_port`）。

    ⚠️ **这里判不到所有畸形 Location**：`javascript:alert(1)` /
    `data:text/html,x` 那类会让 `client.request()` **自己先抛**
    （httpx 内部的 `_build_redirect_request` 在我们的循环之前跑），
    所以本函数根本轮不到执行 —— 那两类由 `request_same_host` 里
    `client.request` 外面的包装兜住。这里判协议管的是会走到本函数的那些
    非 http(s) 写法（`ftp://`、`file://` 等）。
    """
    location = response.headers.get("location")
    if not location:
        raise Ctf2Error("平台返回了跳转，但没有给出 Location 头。")
    try:
        target = httpx.URL(location)
    except Exception as exc:  # noqa: BLE001 - httpx 对畸形 URL 抛的异常类型不固定
        raise Ctf2Error(f"平台给的跳转地址不是一个合法 URL：{exc}") from exc

    # ★ ② 先判协议：后面补 host、连接都只对 http(s) 有意义。
    #   ⚠️ 判据是"**有 scheme 且不是 http(s)**"：相对地址（`/final`）的
    #      scheme 是空串，那是合法的（RFC 7231 允许），要留给下面的 join。
    #      第一版写成 `scheme not in ("http","https")`，把相对跳转全拒了 ——
    #      实测让 6 个既有用例挂掉（"同 host 规范化跳转"等）。
    if target.scheme and target.scheme not in ("http", "https"):
        raise Ctf2Error(f"平台把请求重定向到了不支持的协议（{target.scheme}）。")

    # 只有 scheme 没有 host 的畸形 Location（`http:///path`）—— 补上当前 host，
    # 与 httpx 的处理一致，否则它会被当成"跨 host"而误拒。
    # ⚠️ 判据里的 `target.scheme and` **不能省**：相对地址（`/final`）的
    #    `host` 同样是空串，但它是**合法**的相对引用，要留给下面的 `join`
    #    去补全。省掉这个条件就会对相对地址调 `copy_with(host=…)`，
    #    把 `/final` 拗成一个没有端口的新绝对地址 → 请求打错地方
    #    （实测让"同 host 规范化跳转"和"绕圈中止"两个既有用例挂掉）。
    if target.scheme and not target.host:
        target = target.copy_with(host=current.host)

    # ★ ③ 端口越界要在这里拦掉（它是 `ExceptionGroup[OverflowError]` 的来源）。
    _check_port(target, what="平台给的跳转地址")

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
    `kwargs` 原样透传给 `client.request`（headers / params / json 等），
    ⚠️ 但 `params` **只在第一跳**生效 —— 后续每跳的 query 来自 `Location`
    自己带的（与 httpx 的 `_build_redirect_request` 一致，见模块 docstring）。

    抛 `Ctf2Error` 的情况（都表示"平台给的东西不可用"）：
    1. 跳转目标换了主机 —— 那是在打一个用户没填过的地址；
    2. 跳转目标不是 http/https；
    3. 跳转没给 `Location`，或 `Location` 不是合法 URL；
    4. **端口越界**（`http://host:99999/`）—— 见 `_check_port`：
       不拦的话异步 httpx 会冒出 `ExceptionGroup[OverflowError]`，
       而它**不是** `httpx.HTTPError` 的子类，调用点接不住 → 500。

    ⚠️ 每一跳都过 `hostguard.is_blocked_host`。这一条**不是**主要防线
    （主要防线是上面那条"禁跨 host"），但它保证：即便用户自己填了一个
    元数据地址（那会在入口被拦），或者 base_host 恰好解析到 link-local，
    也不会在这里被当成合法目标。
    """
    current = httpx.URL(url)
    current_method = method.upper()
    send_kwargs = dict(kwargs)

    for hop in range(max_hops + 1):
        # ① 每一跳都过一遍"明确拒绝的地址"判据（与入口同一份实现）。
        if hostguard.is_blocked_host(current.host):
            raise Ctf2Error(f"平台把请求指向了被拒绝的地址（{current.host}）。")

        # ★ 端口越界也在这里拦。第一跳的 URL 是调用方给的
        #   （`transport` 拼的是 `base_url + path`），所以**入口**同样可能带着
        #   越界端口 —— 实测把 base_url 配成 `http://127.0.0.1:99999` 时，
        #   第一跳就会冒出 `ExceptionGroup[OverflowError]`。
        #   `_check_port` 在 `_next_url` 里也调了一次：那一次管的是
        #   "Location 指过来的端口"，这一次管的是"当前这个 URL"。
        _check_port(current, what="要请求的地址")

        # ★★ 只有第一跳用调用方的 `params`；之后 query 由 `Location` 决定。
        #    （httpx 的 `_build_redirect_request` 不带 params 到下一跳。）
        hop_kwargs = dict(send_kwargs)
        if hop > 0:
            hop_kwargs.pop("params", None)

        try:
            response = await client.request(
                current_method, current, follow_redirects=False, **hop_kwargs
            )
        except (httpx.HTTPError, Ctf2Error):
            # 这两类是调用点认识的，原样上抛（`HTTPError` 由 `transport`
            # 转成 `Ctf2Unavailable`，`probe` 则直接跳过这个候选端点）。
            raise
        except BaseException as exc:  # noqa: BLE001
            # ★ 这里是 httpx **内部**解析 Location 时抛出来的那两类
            #   （见 `_LIBRARY_URL_ERRORS` 上的说明）：畸形 Location 会在
            #   `client.request()` 里就炸，我们的 `_next_url` 还没机会执行。
            #   只有"确实是地址/网络层面"的才转成 `Ctf2Error`；其它的
            #   （我们自己的编程错误）原样上抛，别用一个"平台的问题"盖住它。
            if _is_transport_level(exc):
                raise Ctf2Error(
                    f"平台给的跳转地址无法使用（{type(exc).__name__}）：{exc}"
                ) from exc
            raise
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
