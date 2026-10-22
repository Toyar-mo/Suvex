"""`core.netguard`：连接层判据（域名写法 / DNS 重绑定）。

## 这组用例守的是什么

`app/core/hostguard.py` 把"哪些地址不能打"收敛成了一份判据，但它看的是
**字符串**。而"字符串"与"实际会连到哪里"之间隔着一个 DNS：

    169.254.169.254.nip.io  →  169.254.169.254     ★ 判据层看不出问题
    rt-meta.test            →  169.254.169.254     ★ 内网 DNS 同理

这类写法在判据层**没法拦**（那里只有字符串）。`netguard` 在
**解析之后、连接之前**再判一次，并把"已验证的 IP 字面量"交给内层去连
—— 后者顺带消灭了 TOCTOU（内层不可能再解析一次）。

## ★ 为什么放行组和拒绝组**都要断言**

只测"这些被拒"是不够的：一个"把什么都拒掉"的实现也能通过。而这里的
误伤代价特别大 —— 比赛平台很可能就部署在内网（`192.168.x` / `10.x` /
docker 网关 / `127.0.0.1`），拒掉它们等于这个功能整个废掉。
所以下面同时钉住"内网必须继续放行"。

## 打桩方式：换掉 `getaddrinfo`，不真解析、不真连

⚠️ 本文件**不连任何非本机地址**。`getaddrinfo` 全部换成固定映射，
而"放行"那几条只走到连接层就返回（内层是记录型假 backend，
不碰真 socket）。这也是 `tests/conftest.py` 的 `_block_egress` 要求的：
测试里不该有任何真实外呼。
"""

from __future__ import annotations

import socket
from typing import Any

import httpcore
import httpx
import pytest

from app.core import netguard

# ══════════════════════════════════════════════════════════
# 测试替身
# ══════════════════════════════════════════════════════════


class RecordingAsyncBackend(httpcore.AsyncNetworkBackend):
    """记录 `connect_tcp` 收到的 host，并按脚本决定连不连得上。

    ★ 它同时是 **TOCTOU 的探针**：断言"内层收到的是 IP 字面量而不是域名"
    就靠它记录的 `hosts`。
    """

    def __init__(self, *, fail_times: int = 0, exc: type[BaseException] | None = None) -> None:
        self.hosts: list[str] = []
        self.ports: list[int] = []
        self._fail_times = fail_times
        self._exc = exc or httpcore.ConnectError

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> Any:
        self.hosts.append(host)
        self.ports.append(port)
        if len(self.hosts) <= self._fail_times:
            # ⚠️ 刻意用 httpcore 的类型（`ConnectError`），而不是 `OSError`
            #    —— 真实 anyio backend 抛的就是它，而它**不是** `OSError`
            #    子类。用 `OSError` 打桩会让回退判据写错也测不出来。
            raise self._exc("模拟连接失败")
        raise httpcore.ConnectError("停在这里：本组用例不需要真的连上")

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Any = None
    ) -> Any:
        raise NotImplementedError

    async def sleep(self, seconds: float) -> None:
        return None


class RecordingSyncBackend(httpcore.NetworkBackend):
    def __init__(self, *, fail_times: int = 0) -> None:
        self.hosts: list[str] = []
        self._fail_times = fail_times

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> Any:
        self.hosts.append(host)
        if len(self.hosts) <= self._fail_times:
            raise httpcore.ConnectError("模拟连接失败")
        raise httpcore.ConnectError("停在这里：本组用例不需要真的连上")

    def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Any = None
    ) -> Any:
        raise NotImplementedError

    def sleep(self, seconds: float) -> None:
        return None


def _patch_dns(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, list[str]]) -> list[str]:
    """把 `socket.getaddrinfo` 换成固定映射。返回被查询过的 host 列表。

    ⚠️ 打的是 `socket.getaddrinfo`，**不是** `anyio.getaddrinfo`：
    实测 `anyio.getaddrinfo` 内部走的就是 `socket.getaddrinfo`
    （`anyio._backends._asyncio` 把它丢进工作线程执行），所以打这一处
    对同步/异步两条路都生效 —— 与 `tests/conftest.py` 的 `_block_egress`
    同一个理由（它也是打这一处）。
    """
    queried: list[str] = []

    def fake(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        text = host.decode() if isinstance(host, bytes) else str(host)
        queried.append(text)
        addrs = mapping.get(text)
        if addrs is None:
            raise socket.gaierror(f"（打桩）没有为 {text!r} 配置映射")
        out = []
        for addr in addrs:
            if ":" in addr:
                out.append(
                    (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (addr, port, 0, 0))
                )
            else:
                out.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, port)))
        return out

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return queried


async def _connect(backend: Any, host: str, port: int = 80) -> Any:
    return await backend.connect_tcp(host, port, timeout=1.0)


def _connect_sync_run(backend: Any, host: str, port: int = 80) -> Any:
    """同步地把 `_connect` 跑完（本文件里所有断言都走这一条）。

    ⚠️ 必须真的把协程跑掉：写 `_connect(...)` 而不 await 会让断言"通过"
    得毫无意义 —— 协程根本没执行，什么异常都不会抛（实测踩到过，
    pytest 只报一条 `RuntimeWarning: coroutine was never awaited`，
    而用例是 FAILED 的）。
    """
    import asyncio

    return asyncio.run(_connect(backend, host, port))


#: 域名 → 地址的固定映射。左边是"看起来人畜无害"的写法。
#:
#: ⚠️ 字面量 IP（`127.0.0.1` 等）也在这里列出来：不列的话它们会走
#: "解析失败"那条路，于是放行组变成"因为 DNS 失败所以没被拦" ——
#: 那样断言虽然也绿，但**测的不是解析后放行**。列出来才是真的走到
#: "解析 → 对每个结果跑判据 → 放行"这条路径。
DNS_MAP: dict[str, list[str]] = {
    # ★ 内网 DNS 把域名指向 link-local —— 判据层看不见这个域名有什么问题
    "rt-meta.test": ["169.254.169.254"],
    # ★ 发现 1 的残余风险：把 IP 编进域名的通配 DNS 服务
    "169.254.169.254.nip.io": ["169.254.169.254"],
    "100.100.100.200.sslip.io": ["100.100.100.200"],
    # ★ 多 A 记录：一个合法、一个 link-local（fail-closed 的正面）
    "mixed.test": ["1.2.3.4", "169.254.169.254"],
    # ★★ IPv6 link-local（`fe80::/10`，2026-10-16 补）：
    #    域名解析到 IPv6 的 link-local —— 与 rt-meta.test 同一个形状，
    #    而这条在补 `fe80::/10` 之前是**放行**的。
    "v6meta.test": ["fe80::1"],
    # 带 scope id 的形态（`getaddrinfo` 真的会这么返回，实测过）
    "v6scope.test": ["fe80::1%eth0"],
    # ★ AWS 的 IPv6 元数据地址
    "aws6meta.test": ["fd00:ec2::254"],
    # 正常的（必须放行）
    "ok.test": ["1.2.3.4"],
    "internal.test": ["192.168.1.10"],
    "localhost": ["::1", "127.0.0.1"],
    # ★ IPv6 的内网（ULA）必须放行
    "v6internal.test": ["fd00::1"],
    # 字面量 IP（放行组用，见上面的说明）
    "127.0.0.1": ["127.0.0.1"],
    "192.168.1.10": ["192.168.1.10"],
    "10.0.0.5": ["10.0.0.5"],
    "172.18.0.1": ["172.18.0.1"],
    "203.0.113.9": ["203.0.113.9"],
    "::1": ["::1"],
    # 空解析（fail-closed 的另一个正面）
    "empty.test": [],
}


# ══════════════════════════════════════════════════════════
# 拒绝：域名写法（这是本模块存在的理由）
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "host",
    [
        "rt-meta.test",
        "169.254.169.254.nip.io",
        "100.100.100.200.sslip.io",
        "mixed.test",
        # ★★ IPv6 link-local 与 AWS 的 IPv6 元数据地址（2026-10-16 补）。
        #    补 `fe80::/10` 之前，这三条在**连接层**也是放行的。
        "v6meta.test",
        "v6scope.test",
        "aws6meta.test",
    ],
)
def test_a_domain_resolving_to_a_blocked_address_is_refused(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    """★★ 域名解析到被禁地址 → 连接层拒掉。

    这一组**判据层一条都拦不住**（它们都是普通域名，字符串层面没有
    任何特征）—— 这正是连接层要补的那个洞。

    ⚠️ `mixed.test` 是 **fail-closed** 的正面：它同时解析出合法地址
    （`1.2.3.4`）和 link-local。实现若写成"过滤掉被禁的、连剩下的"，
    这条就会**放行** —— 而那样攻击者只要多挂一条 A 记录就能过。
    """
    _patch_dns(monkeypatch, DNS_MAP)
    inner = RecordingAsyncBackend()
    guard = netguard.GuardedAsyncBackend(inner)

    with pytest.raises(netguard.BlockedAddressError) as excinfo:
        _connect_sync_run(guard, host)

    # ⚠️ 断言"错误信息里带得出**被拦的那个地址**"，而不是写死某几个 IPv4
    #    地址 —— 写死的话，IPv6 那几条（`fe80::1` / `fd00:ec2::254`）
    #    就会因为"信息里没有 169.254.169.254"而假失败（实测踩到过）。
    #    这里用 DNS_MAP 里给这个域名配的地址去比，于是 IPv4/IPv6 都成立。
    expected_addrs = DNS_MAP[host]
    assert any(addr.split("%")[0] in str(excinfo.value) for addr in expected_addrs), (
        f"错误信息里没有带出被拦的地址（{expected_addrs}）—— "
        "排查时'它解析到了哪'是最有用的一条信息"
    )
    assert inner.hosts == [], (
        "被拒的地址**一个都不该**被送去连接 —— 现在连接层被调用了，"
        "说明判据没有在连接之前拦下它"
    )


def test_a_blocked_literal_is_refused_without_resolving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ 字面量判据（第 ① 步）必须**在解析之前**跑。

    这条钉的是顺序：`169.254.169.254` 这种直接写出来的字面量，不该先
    去问一次 DNS。除了省一次查询，更重要的是 —— 字面量根本不需要解析，
    而"先解析再判"会让一个**解析失败**的合法字面量变成另一种错误。
    """
    queried = _patch_dns(monkeypatch, DNS_MAP)
    inner = RecordingAsyncBackend()
    guard = netguard.GuardedAsyncBackend(inner)

    with pytest.raises(netguard.BlockedAddressError):
        _connect_sync_run(guard, "169.254.169.254")

    assert queried == [], (
        "字面量判据应当在解析**之前**就拦下它，这里却先查了 DNS —— "
        "顺序反了"
    )


@pytest.mark.parametrize(
    "host",
    [
        # IP 等价写法（hostguard 的归一化负责，这里验"接上了"）
        "2852039166",
        "0xA9FEA9FE",
        "::ffff:169.254.169.254",
        "169.254.169.254.",
        # ★ IPv6 link-local 的几种写法（2026-10-16 补）
        "fe80::1",
        "fe80::1%eth0",
        "[fe80::1]",
        "fd00:ec2::254",
        # 域名形态（靠字符串那条判据）
        "metadata.google.internal",
        "metadata.goog",
    ],
)
def test_equivalent_spellings_are_refused_at_the_connection_layer(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    """★ 判据层的等价写法归一化，在连接层同样生效。

    ⚠️ 这一组**不是**重复测 `hostguard`（那有它自己的用例）。这里验的是
    `netguard` **真的用了那一份判据**，而不是自己写了一份字符串比对 ——
    两份判据一定会漂移，而漂移的那一次不会有任何症状。
    """
    _patch_dns(monkeypatch, DNS_MAP)
    guard = netguard.GuardedAsyncBackend(RecordingAsyncBackend())

    with pytest.raises(netguard.BlockedAddressError):
        _connect_sync_run(guard, host)


# ══════════════════════════════════════════════════════════
# ★★ 放行：内网必须继续可用（防"把什么都拒掉"的实现）
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "host",
    [
        "ok.test",  # 普通公网
        "internal.test",  # 域名 → 192.168.x（比赛平台很可能就这样部署）
        "127.0.0.1",  # 本机部署
        "192.168.1.10",
        "10.0.0.5",
        "172.18.0.1",  # docker 网关
        "::1",  # IPv6 回环
        "localhost",  # 域名 → 回环
        "203.0.113.9",
        # ★★ IPv6 的内网段必须继续放行（2026-10-16 补 fe80::/10 时
        #    最容易连坐的就是它 —— ULA 是内网，按项目口径该放行）
        "v6internal.test",  # 域名 → fd00::1（ULA）
        "fc00::1",
        "fd12:3456::1",
        "fec0::1",  # 已废弃的 site-local，在 fe80::/10 之外
    ],
)
def test_internal_and_normal_targets_are_still_allowed(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    """★★ 放行组 —— **绝不能**退化成"禁止内网"。

    `hostguard` / `platforms.py` 的注释写明这是刻意的：比赛平台很可能
    就部署在内网，而"拒绝内网"会把最可能的使用场景直接挡在门外。

    ⚠️ 与上面的拒绝组是**一对**：只有两组一起，才能证明修的是"域名写法"
    而不是"把范围扩大成内网全禁"。

    ⚠️ 这里内层会抛 `ConnectError`（假 backend 不真连）—— 那是**预期**
    的：本组只断言"没被 guard 拒"，连接成功与否不在判据范围内。
    """
    import asyncio

    _patch_dns(monkeypatch, DNS_MAP)
    guard = netguard.GuardedAsyncBackend(RecordingAsyncBackend())

    async def go() -> None:
        try:
            await _connect(guard, host)
        except netguard.BlockedAddressError as exc:
            raise AssertionError(
                f"{host!r} 被 guard 拒了 —— 本项目刻意不做内网限制"
                f"（见 platforms.py 的说明）。原因：{exc}"
            ) from exc
        except httpcore.ConnectError:
            pass  # 预期：假 backend 不真连

    asyncio.run(go())


def test_the_allow_group_reaches_the_connection_layer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ 放行组要**真的走到连接层**，而不是被更早的分支吞掉。

    这条与上面那组配合：上面证明"没被拒"，这条证明"确实尝试去连了"
    （内层收到了地址）。少了这条，一个"解析成功就直接返回、从不连接"
    的实现也能让放行组全绿。
    """
    import asyncio

    _patch_dns(monkeypatch, DNS_MAP)
    inner = RecordingAsyncBackend()
    guard = netguard.GuardedAsyncBackend(inner)

    async def go() -> None:
        with pytest.raises(httpcore.ConnectError):
            await _connect(guard, "ok.test")

    asyncio.run(go())
    assert inner.hosts == ["1.2.3.4"], (
        f"放行的地址应当被送去连接，实际收到 {inner.hosts}"
    )


# ══════════════════════════════════════════════════════════
# ★★ 结构断言一：内层收到的是 IP 字面量（防 TOCTOU 回归）
# ══════════════════════════════════════════════════════════


def test_the_inner_backend_receives_an_ip_literal_not_the_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★★ 内层 `connect_tcp` 必须收到 **IP 字面量**。

    ## 这条是本方案的核心断言（不是多余的）

    把已验证的 IP 钉给内层，内层就**不可能**再解析一次 —— 于是不存在
    "校验时解析到 A、连接时解析到 B"的窗口。**DNS 重绑定能成立，靠的正是
    那个窗口**；没有它，重绑定不成立。

    ⚠️ 这条一旦回归（比如有人"顺手"把 host 改回域名传下去），
    上面所有拒绝组**仍然全绿** —— 因为校验确实做了，只是白做了。
    所以必须单独钉住"传下去的是什么"。
    """
    import asyncio

    _patch_dns(monkeypatch, DNS_MAP)
    inner = RecordingAsyncBackend()
    guard = netguard.GuardedAsyncBackend(inner)

    async def go() -> None:
        with pytest.raises(httpcore.ConnectError):
            await _connect(guard, "ok.test")

    asyncio.run(go())

    assert inner.hosts == ["1.2.3.4"], (
        f"内层收到的应当是已验证的 IP 字面量，实际是 {inner.hosts} —— "
        "若这里是域名，说明内层还会自己再解析一次，TOCTOU 窗口回来了"
    )
    assert "ok.test" not in inner.hosts


def test_the_sync_path_also_pins_the_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 同步那条路（`app/providers/llm.py` 用的）同样钉 IP。"""
    _patch_dns(monkeypatch, DNS_MAP)
    inner = RecordingSyncBackend()
    guard = netguard.GuardedSyncBackend(inner)

    with pytest.raises(httpcore.ConnectError):
        guard.connect_tcp("ok.test", 80, timeout=1.0)

    assert inner.hosts == ["1.2.3.4"], f"实际 {inner.hosts}"


# ══════════════════════════════════════════════════════════
# ★★ 多地址回退（钉 IP 之后，这个职责转移到我们身上）
# ══════════════════════════════════════════════════════════


def test_a_failed_address_falls_back_to_the_next_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★★ 第一个地址连不上时，必须换下一个试。

    ## 为什么这条特别重要

    钉 IP 之后，**anyio 原本的"多地址回退"职责转移到了我们身上**。
    而 `httpcore` 的 backend 内部把 `OSError` 映射成
    `httpcore.ConnectError`，**它不是 `OSError` 子类**（实测 MRO：
    `ConnectError → NetworkError → Exception`）。只捕 `OSError` 就会
    导致"第一个地址失败后不回退" —— 典型症状是 `localhost` 解析出
    `::1` 优先、连不上就不再试 `127.0.0.1`，表现为"某个站点间歇性连不上"。

    ⚠️ 假 backend 抛的**正是** `ConnectError`（不是 `OSError`），
    所以这条能真的测出回退判据写错。
    """
    import asyncio

    _patch_dns(monkeypatch, DNS_MAP)
    inner = RecordingAsyncBackend(fail_times=1)
    guard = netguard.GuardedAsyncBackend(inner)

    async def go() -> None:
        with pytest.raises(httpcore.ConnectError):
            await _connect(guard, "localhost")

    asyncio.run(go())

    assert inner.hosts == ["::1", "127.0.0.1"], (
        f"第一个地址失败后应当回退到第二个，实际只试了 {inner.hosts} —— "
        "回退判据漏了 httpcore 的异常类型（ConnectError 不是 OSError 子类）"
    )


def test_the_sync_path_also_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 同步那条路的回退同样要生效。"""
    _patch_dns(monkeypatch, DNS_MAP)
    inner = RecordingSyncBackend(fail_times=1)
    guard = netguard.GuardedSyncBackend(inner)

    with pytest.raises(httpcore.ConnectError):
        guard.connect_tcp("localhost", 80, timeout=1.0)

    assert inner.hosts == ["::1", "127.0.0.1"], f"实际 {inner.hosts}"


def test_a_blocked_address_is_not_retried_as_a_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★★ 被禁的地址**不许**被当成"换个地址再试"。

    判据命中必须**整条拒**（fail closed）。若把校验放进回退循环里，
    `BlockedAddressError` 是 `NetworkError` 的子类，会被回退的
    `except` 自己吞掉 —— 于是"被禁的地址"退化成"换下一个试试"，
    而 `mixed.test` 那种（一个合法一个 link-local）就会**连上合法那个
    并成功**，看起来一切正常。
    """
    import asyncio

    _patch_dns(monkeypatch, DNS_MAP)
    inner = RecordingAsyncBackend()
    guard = netguard.GuardedAsyncBackend(inner)

    async def go() -> None:
        with pytest.raises(netguard.BlockedAddressError):
            await _connect(guard, "mixed.test")

    asyncio.run(go())
    assert inner.hosts == [], (
        f"被拒的域名不该有任何地址被送去连接，实际 {inner.hosts}"
    )


# ══════════════════════════════════════════════════════════
# ★★ 结构断言二：装配后池后端是 guard（防"装不上但没报错"）
# ══════════════════════════════════════════════════════════


def test_the_async_transport_replaces_the_pool_backend() -> None:
    """★★ 装配后 `pool._network_backend` 必须是 guard。

    这条防的是"装不上但没报错"：`httpx.AsyncHTTPTransport` **不透传**
    `network_backend`（实测它建池那 12 行里没有这个参数、也没有
    `**kwargs`），所以实现只能建完之后替换 `pool._network_backend`。
    替换若因属性改名而失效，**默认行为是静默不设防** —— 看起来防住了，
    其实一个地址都没拦。
    """
    transport = netguard.GuardedAsyncTransport()
    assert isinstance(transport._pool._network_backend, netguard.GuardedAsyncBackend)
    # 内层仍是真的 backend（不是把池换掉）
    assert transport._pool._network_backend._inner is not None


def test_the_sync_transport_replaces_the_pool_backend() -> None:
    """★ 同步 transport 同理（`app/providers/llm.py` 用的是它）。"""
    transport = netguard.GuardedHTTPTransport()
    assert isinstance(transport._pool._network_backend, netguard.GuardedSyncBackend)


def test_ensure_guarded_covers_a_client_that_was_built_elsewhere() -> None:
    """★ `ensure_guarded` 能把一个"用普通 transport 建的" client 补上。

    调用点会用它做最后确认（正常情况下 transport 已经是 guard 了，
    这一步是幂等的）。
    """
    client = httpx.AsyncClient(trust_env=False)
    try:
        assert not isinstance(
            client._transport._pool._network_backend, netguard.GuardedAsyncBackend
        )
        netguard.ensure_guarded(client)
        assert isinstance(
            client._transport._pool._network_backend, netguard.GuardedAsyncBackend
        )
    finally:
        import asyncio

        asyncio.run(client.aclose())


def test_guarding_is_idempotent() -> None:
    """★ 重复装配不叠加包装。

    每次装配都套一层的话，`connect_tcp` 会被反复解析 N 次 ——
    而症状是"请求变慢"，不会有任何报错。
    """
    transport = netguard.GuardedAsyncTransport()
    first = transport._pool._network_backend
    netguard._guard_pool(transport._pool, is_async=True)
    assert transport._pool._network_backend is first, "重复装配叠加了一层包装"


# ══════════════════════════════════════════════════════════
# ★★ fail-closed：装不上必须响亮失败
# ══════════════════════════════════════════════════════════


def test_a_pool_without_the_private_attribute_is_refused() -> None:
    """★★ 池上没有 `_network_backend`（httpcore 改名）→ **raise**。

    这是本方案唯一的脆弱点（依赖一个 httpcore 私有名）。它必须表现为
    "启动就炸"，而不是"静默不设防" —— 后者比不装更坏，因为看起来是防住的。
    """

    class PoolWithoutBackend:
        """模拟 httpcore 改了实现。"""

    with pytest.raises(RuntimeError) as excinfo:
        netguard._guard_pool(PoolWithoutBackend(), is_async=True)

    assert "_network_backend" in str(excinfo.value)


def test_a_proxy_pool_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """★★ 代理池 → **raise**（防护在代理路径下结构性失效）。

    ## 为什么代理池必须拒，而不是"尽力而为"

    走 HTTP 代理时 `connect_tcp` 拿到的是**代理的地址**，而目标主机名在
    HTTP 请求行里（`GET http://rt-meta.test:8099/`）—— **连接层根本看不见
    目标主机**，所以无从判断。RedTeam 用真转发的代理端到端打实过，判据是
    元数据靶子的**命中计数**（不是"收到 200"），而且**即使把 `_mounts`
    里的代理池也一起补上，也拦不住**。

    所以这里只能"响亮失败"：让"环境里出现了代理变量"这件事**当场报错**，
    而不是静默地让防护失效。

    ⚠️ 代理检测**不能**用 `pool._proxy is not None` —— 实测代理池上
    `_proxy` 就是 `None`。要按类型判。
    """
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7898")
    client = httpx.AsyncClient(trust_env=True)
    try:
        assert client._mounts, "前置条件不成立：带 trust_env=True 且有 HTTP_PROXY 时应当有挂载"
        with pytest.raises(RuntimeError) as excinfo:
            netguard.ensure_guarded(client)
        assert "代理" in str(excinfo.value)
    finally:
        import asyncio

        asyncio.run(client.aclose())


def test_the_guarded_transport_itself_refuses_a_proxy() -> None:
    """★ 直接构造带 `proxy=` 的 guarded transport 也要拒。

    这条守的是另一个入口：调用点如果写
    `GuardedAsyncTransport(proxy="http://…")`，装配时就必须炸 ——
    不能等到有人去调 `ensure_guarded` 才炸。
    """
    with pytest.raises(RuntimeError) as excinfo:
        netguard.GuardedAsyncTransport(proxy="http://127.0.0.1:7898")

    assert "代理" in str(excinfo.value)


# ══════════════════════════════════════════════════════════
# ★★ 异步/同步的判据：按**池**判，不按 client 的类判
# ══════════════════════════════════════════════════════════


def test_the_async_decision_comes_from_the_pool_not_the_client_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★★★ 夹具把 `httpx.AsyncClient` 换成工厂函数时，`ensure_guarded` 仍须正常。

    ## 这条是那 51 条回归的回归闸（2026-10-16）

    失败现场是：夹具打的是 `app.kernel.client.httpx.AsyncClient`，而
    `app.kernel.client.httpx` **就是全局那个 httpx 模块对象** ——
    于是那一步污染的是 **httpx 模块自己的属性**，本模块读到的
    `httpx.AsyncClient` 变成了工厂函数，
    `isinstance(client, httpx.AsyncClient)` 抛
    `TypeError: arg 2 must be a type`。

    这里**主动制造那个污染场景**（把 `netguard.httpx` 里的
    `AsyncClient` 换成工厂），然后断言 `ensure_guarded` **照样工作** ——
    因为它按**池**的类型判，根本不读那个名字。

    ⚠️ 这条在旧实现下必红（实测过）。测试侧那个污染也已修掉
    （`tests/kernel/conftest.py` 改成换模块绑定），但**生产侧的加固
    必须独立成立** —— 否则生产代码仍然依赖一个"运行时可被替换的名字"，
    而那是这次事故的根因，不是它的表象。
    """
    real = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("这个工厂不该被 netguard 调用")

    # 人工制造"名字被换掉"的场景
    monkeypatch.setattr(netguard.httpx, "AsyncClient", factory)

    import asyncio

    async def go() -> None:
        # ⚠️ 用**真类**建 client（工厂是假的、一调就抛）
        client = real(trust_env=False)
        try:
            netguard.ensure_guarded(client)  # 旧实现：TypeError
            assert isinstance(
                client._transport._pool._network_backend,
                netguard.GuardedAsyncBackend,
            )
        finally:
            await client.aclose()

    asyncio.run(go())


def test_an_unknown_pool_type_is_refused() -> None:
    """★★ 不认识的池类型 → **raise**，不许猜它是异步还是同步。

    猜错的后果是"给异步池套了同步 guard"（或反过来），而那个错
    **不会有任何报错**：guard 会安静地待着，直到某次请求真走到
    `connect_tcp` 才炸在别处 —— 排查方向会指到完全无关的地方。

    ⚠️ 它对应的是"新增第三类池"这个真实可能：httpx/httpcore 加了
    一种新的池实现时，我们要**启动就炸**，而不是静默套错。
    """

    class UnknownPool:
        """模拟一种我们没见过的池。"""

    with pytest.raises(RuntimeError) as excinfo:
        netguard._pool_is_async(UnknownPool())

    assert "不认识的连接池类型" in str(excinfo.value)


def test_the_two_pool_types_are_disjoint() -> None:
    """★ 两个池类型**互不匹配** —— 这是"判据能分辨"的前提。

    实测（httpx 0.28.1 / httpcore 1.0.9）：`AsyncConnectionPool` 与
    `ConnectionPool` 没有继承关系。若有交叉，"先判哪个"就会决定结果，
    而那种实现细节会在某个版本悄悄反转。
    """
    import httpcore

    assert not issubclass(httpcore.AsyncConnectionPool, httpcore.ConnectionPool)
    assert not issubclass(httpcore.ConnectionPool, httpcore.AsyncConnectionPool)

    async_transport = httpx.AsyncHTTPTransport()
    sync_transport = httpx.HTTPTransport()
    assert netguard._pool_is_async(async_transport._pool) is True
    assert netguard._pool_is_async(sync_transport._pool) is False


def test_a_real_proxy_pool_still_reports_the_proxy_message() -> None:
    """★★★ **真实**代理路径下，报的必须是「代理」那条错误，不能是「不认识的池」。

    ## 这条守的是什么（以及它为什么用**真实构造**）

    `ensure_guarded` 的调用点是
    `_guard_pool(pool, is_async=_pool_is_async(pool))`，而**实参先求值** ——
    所以 `_pool_is_async` 一定跑在 `_guard_pool` 里那句代理检查**之前**。
    于是有个理论风险：代理池若被判成"不认识的类型"，用户看到的会是
    「不认识的连接池类型」而不是「环境里有代理、请用 trust_env=False」，
    而后者才是他真正需要的那条信息。

    ## ⚠️ 这条测试的写法本身是有讲究的（我第一版写错了）

    我第一版是**把一个假类注册进 `_PROXY_POOLS_ASYNC`**，然后断言
    `_guard_pool(假实例, ...)` 报「代理」。**那个对象在真实路径里造不出来** ——
    它只证明"我手造的场景会走进那句话"，**不证明真实路径会**。

    实测（Kali，真实构造）：真实代理池是
    `AsyncHTTPProxy`（**是 `AsyncConnectionPool` 的子类**），
    所以 `_pool_is_async` 稳稳返回 `True`、顺利走到代理检查，文案正确。

    所以这条改成**真造一个带代理的 client**：那才是用户真的会遇到的路径。

    ⚠️ 不用 SOCKS 代理来测：`socksio` **不是本项目的依赖**（实测未安装），
    `httpx.AsyncClient(proxy="socks5://…")` 会直接 `ImportError`。
    而那个失败与这条断言无关 —— 也就是说 SOCKS 池在这套部署里
    **根本不存在**，所以"它会不会被降级"是一个**没有真实触发路径**的
    理论问题（已在 `netguard._pool_is_async` 的 docstring 里如实记下，
    并按"只修注释、不动行为"处理）。
    """
    c = httpx.AsyncClient(proxy="http://127.0.0.1:7898", trust_env=False)
    try:
        with pytest.raises(RuntimeError) as excinfo:
            netguard.ensure_guarded(c)
        message = str(excinfo.value)
        assert "代理" in message, (
            "真实代理 client 走的这条路上，报的应当是「代理池」那条错误 —— "
            f"实际是：{message[:90]}"
        )
        assert "不认识的连接池类型" not in message, (
            "代理池被降级成了「不认识的池类型」—— 那会把运维引向"
            "『是不是 httpcore 换实现了』，而不是『是不是有人加了 HTTP_PROXY』"
        )
        assert "trust_env" in message, "错误文案里应当给出可操作的修法"
    finally:
        import asyncio

        asyncio.run(c.aclose())


def test_a_real_proxy_pool_is_classified_as_async() -> None:
    """★ 真实代理池在"按池判"下必须归到**异步**那一侧。

    这是上一条能成立的**前提**：`AsyncHTTPProxy` 是
    `AsyncConnectionPool` 的子类（实测 MRO：
    `AsyncHTTPProxy → AsyncConnectionPool → AsyncRequestInterface → object`），
    所以 `_pool_is_async` 判得出答案、不会走到"未知类型"那个分支。

    ⚠️ 这条与上一条是**一对**：只测"报对了"的话，一个"把什么都当异步"
        的实现也能过；这条钉住"它确实被判成了异步"。
    """
    c = httpx.AsyncClient(proxy="http://127.0.0.1:7898", trust_env=False)
    try:
        pool = c._transport._pool
        assert isinstance(pool, netguard._ASYNC_POOL_TYPE), (
            f"真实代理池的类型是 {type(pool).__name__}，它应当是 "
            f"{netguard._ASYNC_POOL_TYPE.__name__} 的子类"
        )
        assert netguard._pool_is_async(pool) is True
    finally:
        import asyncio

        asyncio.run(c.aclose())


# ══════════════════════════════════════════════════════════
# ★★ 集成：调用点**接得住**（否则就是一个 500）
# ══════════════════════════════════════════════════════════


def test_the_error_is_a_catchable_httpx_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """★★ 端到端：被拦的请求必须抛**调用点接得住**的异常。

    ## 这条是本实现与"原型形状"的关键差别，必须有测试

    原型里抛的是裸 `OSError("[RT-GUARD] …")`，而**那在生产里会变成 500**：
    httpx 的 `map_httpcore_exceptions()` 只映射它那张表里的 httpcore 异常，
    而**实测那张表里没有 `OSError`** —— 裸 `OSError` 会原样穿过 httpx
    冒到调用点，而调用点（`transport.py` / `probe.py` / `llm.py`）只
    `except httpx.HTTPError`，接不住。

    `netguard.BlockedAddressError` 继承 `httpcore.ConnectError`，于是被
    映射成 `httpx.ConnectError`。这条用例走**真实 httpx 栈**（不是直接调
    backend），验的正是这条映射真的成立。
    """
    import asyncio

    _patch_dns(monkeypatch, {"evil.test": ["169.254.169.254"]})

    async def go() -> None:
        client = httpx.AsyncClient(
            transport=netguard.GuardedAsyncTransport(), trust_env=False
        )
        try:
            with pytest.raises(httpx.ConnectError) as excinfo:
                await client.get("http://evil.test/")
            assert "netguard" in str(excinfo.value)
        finally:
            await client.aclose()

    asyncio.run(go())


def test_a_dns_failure_is_also_a_catchable_httpx_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★★ DNS 解析失败同样必须变成 `httpx.HTTPError` 家族。

    不转的话，`socket.gaierror`（是 `OSError`，但**不在 httpx 的映射表里**）
    会原样冒到调用点 → 500。而"平台域名解析不了"是很常见的一种失败
    （配错域名、内网 DNS 不通），它必须是**用户看得懂的错误**，不是 500。

    ⚠️ 这里**显式打桩** `getaddrinfo` 抛 `gaierror`，而不是去打一个真域名：
    ① 测试里不该有真实外呼（`tests/conftest.py` 的 `_block_egress` 会拦，
    而它抛的 `EgressBlocked` **也是** `OSError` —— 那样这条用例就会变成
    "在测 conftest 的护栏"，而不是测我们的转换）；
    ② 打桩之后失败原因是确定的，不受运行环境 DNS 影响。
    """
    import asyncio

    def failing_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        raise socket.gaierror(f"（打桩）解析失败：{host!r}")

    monkeypatch.setattr(socket, "getaddrinfo", failing_getaddrinfo)

    async def go() -> None:
        client = httpx.AsyncClient(
            transport=netguard.GuardedAsyncTransport(), trust_env=False
        )
        try:
            with pytest.raises(httpx.HTTPError) as excinfo:
                await client.get("http://dns-fails.test/")
            # 必须是 httpx 家族（调用点接得住），而不是裸 OSError
            assert isinstance(excinfo.value, httpx.ConnectError)
        finally:
            await client.aclose()

    asyncio.run(go())


# ══════════════════════════════════════════════════════════
# 小零件：地址规整
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("169.254.169.254", "169.254.169.254"),
        # ★ IPv6 的 scope id 必须剥掉：`fe80::1%eth0` 这种写法
        #   `ipaddress` 不认、字符串比对也不中 —— 不剥就是一个绕过。
        ("fe80::1%eth0", "fe80::1"),
        ("::1%lo", "::1"),
    ],
)
def test_address_normalization_strips_the_ipv6_scope(raw: str, expected: str) -> None:
    """★ `getaddrinfo` 给的 IPv6 地址可能带 scope id，判据前要剥掉。

    ⚠️ 实测（Kali）：`getaddrinfo("fe80::1%eth0", …)` 返回的 sockaddr
    里地址是 `'fe80::1'`、scope 在**第 4 个元素**（`('fe80::1', 80, 0, 2)`）。
    所以按 `sockaddr[0]` 取地址时通常已经不带 scope 了 ——
    但**不能依赖这一点**：不同平台/不同写法都可能把它留在字符串里，
    而留着它就是一个"字符串比对与 `ipaddress` 都可能认不出"的形态。

    这条钉的是"剥"这个动作本身，与上一步 `_check_addresses` 的判据
    是两回事（那条钉"剥完之后拦得住"）。
    """
    assert netguard._normalize_address(raw) == expected


def test_an_empty_resolution_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 解析出来一个地址都没有 → 拒（fail closed，不是"放行去连"）。"""
    _patch_dns(monkeypatch, {"empty.test": []})
    guard = netguard.GuardedAsyncBackend(RecordingAsyncBackend())

    import asyncio

    async def go() -> None:
        with pytest.raises(httpcore.ConnectError):
            await _connect(guard, "empty.test")

    asyncio.run(go())
