"""连接层 SSRF 防护：**解析之后、连接之前**再判一次地址。

## 这个模块补的是哪一类洞（判据层补不了的那一类）

`app/core/hostguard.py` 已经把"哪些地址不能打"收敛成一份判据，但它看的是
**字符串**。而"字符串"与"实际会连到哪里"之间隔着一个 DNS：

    169.254.169.254.nip.io   →  169.254.169.254      ★ 判据看不出问题
    rt-meta.test             →  169.254.169.254      ★ 内网 DNS 同理

这类写法（`nip.io` / `sslip.io` / `traefik.me` 把 IP 编进域名，或任何
把域名解析到 link-local 的 DNS）在判据层**没法拦** —— 那里只有字符串。
必须在**连接层**堵：解析拿到地址之后、真正连出去之前。

## 形状：三个动作（与判据层共用同一份清单）

1. **先跑字面量判据** —— 直接调 `hostguard.is_blocked_host(host)`。
   这一步管域名形态（`metadata.google.internal`）与 IP 等价写法
   （`2852039166` / `0x…` / `[::ffff:…]`）。**不新写清单**：
   两份清单一定会漂移，而漂移的那一次不会有任何症状。
2. **再解析，对每一个结果跑同一份判据** —— **任一命中即整条拒**
   （fail closed）。刻意**不是**"挑一个能用的连"：一个域名同时解析出
   合法地址和 link-local 地址时，跳过后者等于"攻击者只要多挂一条
   A 记录就能过"。
3. **把「已验证的 IP 字面量」交给内层去连**。

## ★★ 第 3 步是要害：它顺带消灭了 TOCTOU（DNS 重绑定）

"校验一次再放行"挡不住 DNS 重绑定：校验时解析到公网 A、连接时解析到
内网 B，中间那个窗口就是攻击面。

而这里内层拿到的**是 IP 字面量**，它**不可能**再解析一次 ——
不存在"两次解析结果不一致"的窗口。这是本方案比"多校验一次"强的地方。

**钉 IP 不破坏 TLS**：实测 `httpcore/_async/connection.py` 的 `_connect()`
里 `host` 取自 `self._origin.host.decode("ascii")`，而 TLS 的
`server_hostname` 是 `sni_hostname or self._origin.host.decode("ascii")`
—— **两者同源、都是 origin 域名**。所以 `connect_tcp` 收到 IP 字面量，
而 SNI 与证书校验用的仍是原域名。（同步那条 `_sync/connection.py` 同形。）

## ⚠️⚠️ 硬约束：必须与 `trust_env=False` 一起用

**代理路径下这个模块结构性失效**，不是"可能失效"：走 HTTP 代理时
`connect_tcp` 拿到的是**代理的地址**，而目标主机名在 HTTP 请求行里
（`GET http://rt-meta.test:8099/`）—— **连接层根本看不见目标主机**，
所以无从判断。RedTeam 用真转发的 HTTP 代理端到端打实过，判据是
**元数据靶子的命中计数**（不是"收到 200"），而且**即使把 `_mounts`
里的代理池也一起补上，也拦不住**。

所以本模块的装配是 **fail-closed**：代理池 → `raise`；
`_network_backend` 属性不存在 → `raise`。**绝不"装不上就放行"** ——
静默失效比不装更坏，因为看起来是防住的。

## ★ 依赖 httpcore 私有属性（唯一的脆弱点）

`httpx.AsyncHTTPTransport` **不透传 `network_backend`**（实测它建池那
12 行里没有这个参数、也没有 `**kwargs`），所以只能建完之后替换
`pool._network_backend`。另一种做法（自建连接池、构造时传进去）要依赖
**9 个** httpcore 私有名（`_ssl_context` / `_max_connections` / …），
而 httpx 一改参数就会漂移 —— 所以这里选依赖面更小的那条：
**只依赖 `pool._network_backend` 这一个名字**，并由装配时的自检保证
"名字哪天变了就启动即炸"。

## 这个模块**不解决**什么（别当成万能药）

1. **不拦"解析到内网"** —— 只拦 link-local（`169.254.0.0/16`）与阿里云
   元数据那一个地址，与 `hostguard` / `platforms.py` 注释里"不做内网
   限制是刻意的"完全一致。比赛平台很可能就部署在内网（`192.168.x` /
   `10.x` / docker 网关 / `127.0.0.1`），拦它们等于这个功能整个废掉。
2. **不覆盖非 httpx 的出站** —— 沙箱容器内的网络请求本来就不归我们管。
3. **不管 `http_request` 工具那条路** —— 它另有白名单
   （`guard_tool_url` / `assert_allowed_target`），是两回事，别混。
   本模块覆盖的是"管理员填的平台地址 / LLM `baseUrl`"那条路。
"""

from __future__ import annotations

import socket
from typing import Any

import anyio
import httpcore
import httpx

from app.core import hostguard

#: 连接失败时可以**换下一个地址重试**的异常。
#:
#: ## ★★ 这个元组写错过一次（RedTeam 原型里的真缺陷，C15）
#:
#: `httpcore` 的 `AnyIOBackend.connect_tcp` / `SyncBackend.connect_tcp`
#: 内部把 `OSError` 映射成 `httpcore.ConnectError`，而
#: **`ConnectError` 不是 `OSError` 子类**（实测 MRO：
#: `ConnectError → NetworkError → Exception`）。所以只捕 `OSError`
#: 会导致**第一个地址失败后不回退** —— `localhost` 解析出 `::1` 优先、
#: 连不上就不再试 `127.0.0.1`，症状是"某个站点间歇性连不上"。
#:
#: `ConnectTimeout` 也**不是** `NetworkError` 子类
#: （实测 MRO：`ConnectTimeout → TimeoutException → Exception`），
#: 所以两个都要列。
#:
#: ## 这一条对本模块有实际含义
#:
#: **钉 IP 之后，anyio 原本的"多地址回退"职责转移到了我们身上**
#: （见 `_connect_tcp_with_fallback`）。写错就是上面那个"间歇性连不上"。
_RETRYABLE: tuple[type[BaseException], ...] = (
    OSError,
    httpcore.NetworkError,
    httpcore.ConnectTimeout,
)

#: 代理连接池的类名（异步 / 同步各两个）。
#:
#: ⚠️ **不能**用 `pool._proxy is not None` 判 —— 实测代理池上 `_proxy`
#: 就是 `None`。要按**类型**判。
_PROXY_POOLS_ASYNC: tuple[type[Any], ...] = (
    httpcore.AsyncHTTPProxy,
    httpcore.AsyncSOCKSProxy,
)
_PROXY_POOLS_SYNC: tuple[type[Any], ...] = (
    httpcore.HTTPProxy,
    httpcore.SOCKSProxy,
)

#: 判"这个连接池是异步还是同步"所用的**两个池类型**。
#:
#: ## ★★ 为什么不用 `isinstance(client, httpx.AsyncClient)`（2026-10-16 修）
#:
#: 原实现是 `is_async = isinstance(client, httpx.AsyncClient)`，而它
#: **在测试里会炸**，原因是一整条污染链：
#:
#: 1. 夹具打的是 `app.kernel.client.httpx.AsyncClient`，而
#:    `app.kernel.client.httpx` **就是全局那一个 httpx 模块对象**
#:    （实测 `app.kernel.client.httpx is httpx` → `True`）；
#: 2. 于是那一步改的是 **httpx 模块自己的属性** —— 全局污染；
#: 3. 本模块里的 `httpx` 也是同一个模块对象，所以这里读到的
#:    `httpx.AsyncClient` 变成了夹具的**工厂函数**；
#: 4. `isinstance(x, 工厂函数)` → `TypeError: arg 2 must be a type`。
#:    实测 51 条用例全倒在这一行（`netguard.py:412`）。
#:
#: ## 为什么选"按池类型判"而不是"把 client 类型在导入时固定下来"
#:
#: | 做法 | 语义 | 抗污染 |
#: | --- | --- | --- |
#: | `_ASYNC_CLIENT_TYPE = httpx.AsyncClient`（导入时固定） | 判"client 是不是异步 client" | ✅ 但**绕了一层**：真正决定 `_guard_pool` 走哪个分支的是**池**的异步性 |
#: | ★ 按**池**的类型判 | 判"这个池本身是不是异步的" | ✅ 且**不依赖任何可被替换的名字** |
#:
#: 选后者的理由：`_guard_pool(pool, is_async=…)` 这个参数从头到尾只用来说
#: "该套 `GuardedAsyncBackend` 还是 `GuardedSyncBackend`"，而那**完全由池
#: 的异步性决定**，与 client 是什么类无关。按池判把判据放回了它真正该在的
#: 地方，顺带彻底离开了"可被 monkeypatch 的名字"这条攻击面。
#:
#: ## ⚠️ 两个池类型是**导入时固定**的常量（不是每次去 `httpcore` 查）
#:
#: 它们同样是"可被替换的名字"的潜在目标（夹具理论上也能替换
#: `httpcore.AsyncConnectionPool`）。在这里取一次就固定下来，
#: 与上面那张"不依赖可替换名字"的目标一致。
#:
#: 实测（httpx 0.28.1 / httpcore 1.0.9）：这两个类**互不匹配**
#: （`isinstance(异步池, ConnectionPool)` → `False`，反之亦然），
#: 所以"两个都不是"只可能是**第三种池**（见 `_pool_is_async`）。
_ASYNC_POOL_TYPE: type[Any] = httpcore.AsyncConnectionPool
_SYNC_POOL_TYPE: type[Any] = httpcore.ConnectionPool


class BlockedAddressError(httpcore.ConnectError):
    """命中判据时抛的异常（连接层的"这个地址不能打"）。

    ## ★★ 为什么继承 `httpcore.ConnectError`，而不是像原型那样抛裸 `OSError`

    原型里抛的是 `OSError("[RT-GUARD] blocked …")`，而**那在生产里会
    变成 500**：httpx 的 `map_httpcore_exceptions()` 只映射它那张表里的
    httpcore 异常（`ConnectError → httpx.ConnectError` 等），
    而**实测那张表里没有 `OSError`** —— 于是裸 `OSError` 会原样穿过
    httpx 一路冒到调用点，而调用点只 `except httpx.HTTPError`
    （`transport.py` / `probe.py` / `llm.py` 都是），接不住。

    继承 `ConnectError` 之后：
      · httpx 把它映射成 `httpx.ConnectError`，**调用点接得住**；
      · 语义也对 —— 这就是"连接层面拒绝"，与真实的连不上同类；
      · 而它是 `NetworkError` 的子类，于是**多地址回退**对它天然成立。
    """


def _blocked(host: str, addr: str) -> BlockedAddressError:
    return BlockedAddressError(
        f"[netguard] 拒绝连接：{host} 解析到被禁止的地址 {addr}"
        "（link-local / 云厂商元数据）。这条判据见 app/core/hostguard.py。"
    )


def _normalize_address(addr: str) -> str:
    """把 `getaddrinfo` 给的地址规整一下再判。

    ★ **scope id 必须剥掉**：IPv6 的 link-local 地址在 `sockaddr` 里可能
    带接口后缀（`fe80::1%eth0`）。不剥的话它是个"字符串比对不中、
    `ipaddress` 也可能不认"的形态 —— 那正是一个绕过。
    """
    return addr.split("%", 1)[0] if "%" in addr else addr


def _check_addresses(host: str, addresses: list[str]) -> None:
    """对解析出来的**每一个**地址跑判据；任一命中即整条拒（fail closed）。

    ⚠️ 刻意**不是**"过滤掉被禁的、连剩下的"：那样攻击者只要给一个域名
    多挂一条 link-local 的 A 记录，就会被自动跳过、照连不误。
    """
    if not addresses:
        # 解析成功但一个地址都没有 —— 不可达，按失败处理（fail closed）。
        raise BlockedAddressError(
            f"[netguard] 无法连接：{host} 没有解析出任何地址。"
        )
    for addr in addresses:
        if hostguard.is_blocked_host(addr):
            raise _blocked(host, addr)


def _resolve_sync(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        # ⚠️ 必须转成 `ConnectError`：`gaierror` 是 `OSError`，而它在
        #    httpx 的映射表里**没有**（同 `BlockedAddressError` 的说明）。
        #    不转的话，一个 DNS 解析失败会以裸 `OSError` 冒到调用点 → 500。
        #    转成 `ConnectError` 之后，调用点看到的东西与"没装 guard 时"
        #    完全一致（那时是 anyio 把 `OSError` 映射成 `ConnectError`）。
        raise httpcore.ConnectError(f"[netguard] 无法解析主机名 {host}：{exc}") from exc
    return [_normalize_address(sockaddr[0]) for *_rest, sockaddr in infos]


async def _resolve_async(host: str, port: int) -> list[str]:
    try:
        # ★ 用 `anyio.getaddrinfo` 而不是 `socket.getaddrinfo`：后者会
        #   **阻塞事件循环**，而这个服务是 FastAPI —— 一次慢 DNS 会让
        #   所有并发请求一起卡住。`anyio.getaddrinfo` 把解析放到工作线程，
        #   与 anyio 自己 `connect_tcp` 的做法一致（anyio 是 httpx 的
        #   必需依赖，httpcore 的 `AnyIOBackend` 也用它）。
        infos = await anyio.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise httpcore.ConnectError(f"[netguard] 无法解析主机名 {host}：{exc}") from exc
    return [_normalize_address(sockaddr[0]) for *_rest, sockaddr in infos]


class GuardedAsyncBackend(httpcore.AsyncNetworkBackend):
    """包住真实 backend，在 `connect_tcp` 里做那三件事（见模块 docstring）。"""

    def __init__(self, inner: httpcore.AsyncNetworkBackend) -> None:
        self._inner = inner

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        # ① 字面量判据（域名黑名单 + IP 等价写法归一化）。
        if hostguard.is_blocked_host(host):
            raise _blocked(host, host)

        # ② 解析，并对每一个结果跑同一份判据。
        addresses = await _resolve_async(host, port)
        _check_addresses(host, addresses)

        # ③ 用**已验证的 IP 字面量**去连 → 内层不会再解析 → 无 TOCTOU。
        return await _connect_tcp_with_fallback(
            self._inner.connect_tcp,
            host,
            addresses,
            port=port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        # UDS 没有主机名，也就没有"解析到哪"这回事 —— 直接放行。
        return await self._inner.connect_unix_socket(
            path, timeout=timeout, socket_options=socket_options
        )

    async def sleep(self, seconds: float) -> None:
        # httpcore 用它做重试退避，必须转发（不转发会让重试退避失效）。
        await self._inner.sleep(seconds)


class GuardedSyncBackend(httpcore.NetworkBackend):
    """同步版（`app/providers/llm.py` 的 `probe()` 用的是 `httpx.Client`）。"""

    def __init__(self, inner: httpcore.NetworkBackend) -> None:
        self._inner = inner

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        if hostguard.is_blocked_host(host):
            raise _blocked(host, host)

        addresses = _resolve_sync(host, port)
        _check_addresses(host, addresses)

        last: BaseException | None = None
        for addr in addresses:
            try:
                return self._inner.connect_tcp(
                    host=addr,
                    port=port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except _RETRYABLE as exc:
                last = exc
        assert last is not None  # addresses 非空（_check_addresses 保证）
        raise last

    def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Any = None
    ) -> httpcore.NetworkStream:
        return self._inner.connect_unix_socket(
            path, timeout=timeout, socket_options=socket_options
        )

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)


async def _connect_tcp_with_fallback(
    connect: Any,
    host: str,
    addresses: list[str],
    *,
    port: int,
    timeout: float | None,
    local_address: str | None,
    socket_options: Any,
) -> httpcore.AsyncNetworkStream:
    """逐个地址试连；**只有连接失败才换下一个**。

    ★ 这一段是"钉 IP"之后必须自己承担的责任：原本 anyio 的
    `connect_tcp` 会自己遍历 `getaddrinfo` 的结果，现在换成我们了。
    回退判据见 `_RETRYABLE`（写错就变成"间歇性连不上"）。

    ⚠️ **判据命中不会走到这里**：`_check_addresses` 在任何连接之前就把
    整条拒了。顺序很重要 —— 若把校验放进这个循环，`BlockedAddressError`
    是 `NetworkError` 的子类，会被 `except _RETRYABLE` 自己吞掉，
    于是"被禁的地址"会退化成"换下一个试试"。
    """
    last: BaseException | None = None
    for addr in addresses:
        try:
            return await connect(
                host=addr,
                port=port,
                timeout=timeout,
                local_address=local_address,
                socket_options=socket_options,
            )
        except _RETRYABLE as exc:
            last = exc
    assert last is not None  # addresses 非空（_check_addresses 保证）
    raise last


def _guard_pool(pool: Any, *, is_async: bool) -> None:
    """把一个 httpcore 连接池的后端换成 guard。**装不上就 raise**。"""
    proxy_pools = _PROXY_POOLS_ASYNC if is_async else _PROXY_POOLS_SYNC
    if isinstance(pool, proxy_pools):
        # 代理路径下 `connect_tcp` 拿到的是代理地址，目标主机名在 HTTP
        # 请求行里 —— guard 看不见目标，等于没装。**响亮失败**，
        # 绝不静默放行（见模块 docstring 的硬约束）。
        raise RuntimeError(
            "[netguard] 这个连接池是代理池：代理路径下连接层看不见目标主机，"
            "防护会结构性失效。请用 trust_env=False（或去掉 proxy=），"
            "而不是让一个装不上的 guard 静默放行。"
        )
    if "_network_backend" not in vars(pool):
        # httpcore 改了属性名 —— 启动即炸，别静默不设防。
        raise RuntimeError(
            "[netguard] 连接池上没有 `_network_backend`（httpcore 可能改了实现）："
            "无法安装连接层防护。拒绝在无防护的情况下继续 —— "
            f"池类型 {type(pool).__name__}，实际属性 {sorted(vars(pool))}。"
        )
    inner = pool._network_backend
    if isinstance(inner, (GuardedAsyncBackend, GuardedSyncBackend)):
        return  # 幂等：重复装配不叠加包装
    pool._network_backend = (
        GuardedAsyncBackend(inner) if is_async else GuardedSyncBackend(inner)
    )


class GuardedAsyncTransport(httpx.AsyncHTTPTransport):
    """异步 transport：建好池之后把后端换成 guard。

    ## 为什么是"子类化 + 事后替换"而不是"自建连接池"

    实测 `httpx.AsyncHTTPTransport.__init__` **没有透传 `network_backend`，
    也没有 `**kwargs`**，所以塞不进去。两种装法对比：

    | 方式 | 依赖的 httpcore 私有面 |
    | --- | --- |
    | **本实现**（事后替换） | **1 个**（`pool._network_backend`） |
    | 自建池（构造时传进去） | **9 个**（`_ssl_context` / `_max_connections` / `_max_keepalive_connections` / `_keepalive_expiry` / `_http1` / `_http2` / `_retries` / `_local_address` / `_uds`），外加 `AutoBackend` 要从 `httpcore._backends.auto` 取（顶层没有） |

    两者拦截/放行行为等价，但依赖面差 9 倍、且 httpx 一改参数就会漂移。
    所以选这条，并由 `_guard_pool` 的自检保证"哪天装不上就炸"。
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        _guard_pool(self._pool, is_async=True)


class GuardedHTTPTransport(httpx.HTTPTransport):
    """同步版（`httpx.Client`）。理由同 `GuardedAsyncTransport`。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        _guard_pool(self._pool, is_async=False)


def _pool_is_async(pool: Any) -> bool:
    """这个连接池是不是异步的。**按池自己的类型判**（见 `_ASYNC_POOL_TYPE`）。

    ⚠️ **未知池类型 → raise，不是猜一个**。猜错的后果是"给异步池套了同步
    guard"（或反过来），而那个错**不会有任何报错**：guard 会安安静静地
    待在那儿，直到某次请求真的走到 `connect_tcp` 才炸在别处 ——
    排查方向会指到一个完全无关的地方。

    所以这里 fail closed：只认实测过的类型，其它一律响亮失败，
    把"我们没见过这种池"变成启动时就能看到的一条错误。

    ## ★ 一条**理论上**的边界：未知类型会盖过代理池的文案（如实记录，未改行为）

    调用点是 `_guard_pool(pool, is_async=_pool_is_async(pool))`，而
    **实参先求值** —— 所以本函数一定跑在 `_guard_pool` 函数体里那句
    "代理池 → raise"**之前**。于是有个理论上的顺序问题：一个**既是代理池、
    又不是已知池类型**的对象，会先被本函数判成"不认识的类型"，
    报出「不认识的连接池类型」而不是「环境里有代理、请用 trust_env=False」。
    两条错误把运维引向不同的方向。

    **实测结论：这条在本部署里没有触发路径，所以刻意没改行为。**

    * 真实代理路径产出的池是 **`AsyncHTTPProxy`**，而它**是
      `AsyncConnectionPool` 的子类**（实测 MRO：
      `AsyncHTTPProxy → AsyncConnectionPool → AsyncRequestInterface → object`），
      所以本函数稳稳返回 `True`、顺利走到代理检查，文案正确 ——
      已有一条**用真实构造**的测试钉住它（`test_a_real_proxy_pool_still_reports_the_proxy_message`）。
    * 唯一"不是任何池子类"的是 SOCKS 代理池（`AsyncSOCKSProxy` 的 MRO
      只有 `['AsyncSOCKSProxy', 'object']`），而 **`socksio` 不是本项目的依赖**
      （实测未安装；`httpx.AsyncClient(proxy="socks5://…")` 直接 `ImportError`）。
      也就是说 **SOCKS 池在这套部署里根本构造不出来**。

    ⚠️ 所以这里**没有**把 `_PROXY_POOLS_*` 判成"已知类型"：
    那会让行为变复杂，而**没有任何真实路径能守住那个改动**
    （测试只能靠手造假对象来触发它，而那正是"手造对象 ≠ 真实路径"
    这条纪律所禁止的）。等哪天真装了 `socksio`、或 httpx 真产出了
    第三种池，这条要重看。

    ⚠️ **不能用"有没有 `connect_tcp` 的 async 版本"之类的鸭子判据** ——
    `httpcore.AsyncSOCKSProxy` 实测**不是任何池的子类**，
    这类鸭子判据会把它判错。这也正说明"按已知类型判 + 未知就拒"
    比"按形状猜"可靠。
    """
    if isinstance(pool, _ASYNC_POOL_TYPE):
        return True
    if isinstance(pool, _SYNC_POOL_TYPE):
        return False
    raise RuntimeError(
        "[netguard] 不认识的连接池类型，无法判断它是异步还是同步："
        f"{type(pool).__name__}。拒绝猜一个 —— 猜错会给池套上错误的 guard，"
        "而那种错不会有任何报错，只会在某次请求里炸在别处。"
        f"（实测过的两类：{_ASYNC_POOL_TYPE.__name__} / {_SYNC_POOL_TYPE.__name__}）"
    )


def ensure_guarded(client: httpx.AsyncClient | httpx.Client) -> None:
    """把 `client` 上**所有**连接池都装上 guard（含 `_mounts` 里的）。

    ## 为什么要在 client 这一层再查一次

    `GuardedAsyncTransport` 只管**自己那个池**。而 httpx 的 client 还会
    因为环境变量（`HTTP_PROXY` 等，`trust_env=True` 时）额外挂一批
    **代理池**到 `_mounts` 里 —— 那些池不走我们传进去的 transport。
    实测过：代理路径下防护**结构性失效**（见模块 docstring）。

    所以调用点在建完 client 之后调一次这个函数：
    正常情况下它只是确认"guard 真的装上了"；一旦环境里出现了代理变量，
    它**当场 raise**，把"静默失效"变成"启动就报错"。

    ⚠️ 对**非 httpcore** 的 transport（测试用的 `httpx.MockTransport`、
    自定义 transport）不做处理：它们没有 `_pool`，也就不经过
    `network_backend` 这一层 —— 那里没有"解析到哪"的问题可管。
    这是刻意的跳过，不是漏做。

    ## ★★ 异步/同步按**池**判，不按 client 的类判（2026-10-16 修）

    原实现第一行是 `is_async = isinstance(client, httpx.AsyncClient)`，
    它**在测试里会炸** —— 夹具替换 `app.kernel.client.httpx.AsyncClient`
    时污染的是全局 httpx 模块（实测两者是同一个模块对象），
    于是 51 条用例全倒在 `TypeError: arg 2 must be a type`。
    现在改成按每个池自己的类型判，见 `_pool_is_async` 与
    `_ASYNC_POOL_TYPE` 的说明。
    """
    transports: list[Any] = [client._transport]
    transports.extend((client._mounts or {}).values())
    for transport in transports:
        pool = getattr(transport, "_pool", None)
        if pool is None:
            continue
        # ⚠️ 逐个池判，而**不是在循环外判一次 client**：`_mounts` 里的
        #    代理池与主池理论上可以是不同类型，按池判天然覆盖这种情况。
        _guard_pool(pool, is_async=_pool_is_async(pool))


__all__ = [
    "BlockedAddressError",
    "GuardedAsyncBackend",
    "GuardedAsyncTransport",
    "GuardedHTTPTransport",
    "GuardedSyncBackend",
    "ensure_guarded",
]
