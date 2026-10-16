"""`core.hostguard`：**平台地址的等价写法**都要被拦下。

## 这组用例守的是什么（RedTeam 发现 1）

`assert_safe_base_url` 原来的判据是**字符串比对**：

    parsed.host.lower() in {"169.254.169.254", "metadata.google.internal", …}

而 `169.254.169.254` 有无穷多种**等价写法**，`getaddrinfo` 全部认，
字符串却一个都不等 —— 于是它们**全部放行**。实测过的四条绕过：

    2852039166                （十进制整数）
    0xA9FEA9FE                （十六进制）
    0251.0376.0251.0376       （八进制点分）
    [::ffff:169.254.169.254]  （IPv4-mapped IPv6）

一次 GET 就能换到云主机的临时凭据，而"让服务端替你打一个 URL"
正是这个功能的形状 —— 所以这一条必须堵死。

## ★ 为什么两种判据**并存**，不是替换

`metadata.google.internal` / `metadata.goog` 是**域名**，IP 归一化
分支对它们返回 `None`（它们不是字面量 IP）。只留 IP 分支就等于把
它们放过去了 —— RedTeam 第一版原型正是栽在这里。

## ★ 为什么放行组和拒绝组**都要断言**

只测"这些写法被拒"是不够的：一个"把什么都拒掉"的实现也能通过。
所以下面同时钉住"内网地址必须继续放行" —— 见
`test_internal_targets_are_still_allowed` 的说明。
"""

from __future__ import annotations

import pytest

from app.core import hostguard

# ══════════════════════════════════════════════════════════
# 拒绝：等价写法（这是发现 1 的正面）
# ══════════════════════════════════════════════════════════

#: 每一条都是 `getaddrinfo` 认、而**字符串比对认不出**的写法。
BLOCKED_HOSTS = [
    # 标准写法（修复前就拦得住，留作对照）
    "169.254.169.254",
    # ★ 以下四条是修复前**全部放行**的
    "2852039166",  # 十进制整数
    "0xA9FEA9FE",  # 十六进制
    "0251.0376.0251.0376",  # 八进制点分
    "::ffff:169.254.169.254",  # IPv4-mapped IPv6
    # 尾点（DNS 里与不带点等价）
    "169.254.169.254.",
    # 网段里的其它地址（不只那一个 IP）
    "169.254.0.1",
    "169.254.255.254",
    # 阿里云元数据的另一个地址
    "100.100.100.200",
    # 域名形态（必须靠字符串那条判据，见模块 docstring）
    "metadata.google.internal",
    "metadata.goog",
    # ★★ IPv6 link-local（`fe80::/10`，2026-10-16 补）。
    #    补之前这几条**全部放行** —— 而 `netguard` 的连接层判据也取自
    #    本模块，所以那是**两层同时漏**的一条。
    "fe80::1",
    "fe80::1%eth0",  # 带 scope id（接口名）的写法
    "[fe80::1]",  # URL 里的方括号形态
    "fe80::",  # 网段起点
    "febf::1",  # 网段上边界（`fec0::1` 已出界，见下面的放行组）
    # ★ AWS 的 **IPv6** 元数据地址（与 100.100.100.200 同一个形状）
    "fd00:ec2::254",
]


@pytest.mark.parametrize("host", BLOCKED_HOSTS)
def test_equivalent_metadata_spellings_are_blocked(host: str) -> None:
    """★ 这一组是发现 1 的正面：修复前它们**全部放行**。"""
    assert hostguard.is_blocked_host(host), (
        f"{host!r} 是元数据地址的等价写法，必须被拦下 —— "
        "字符串比对拦不住它，这正是发现 1"
    )


def test_the_four_bypasses_are_really_equivalent() -> None:
    """★ 把"它们确实是同一个地址"这件事本身也钉住。

    这条不是多余的：上面那些断言在**归一化写坏**（比如只认十六进制
    不认十进制）时仍然可能全绿 —— 而这一条会直接指出"归一化结果不对"。
    """
    expected = "169.254.169.254"
    for spelling in ("2852039166", "0xA9FEA9FE", "0251.0376.0251.0376", "169.254.169.254."):
        addr = hostguard.normalize_host(spelling)
        assert addr is not None, f"{spelling!r} 应当能被归一化成 IP"
        assert str(addr) == expected, f"{spelling!r} 归一化成了 {addr}，应当是 {expected}"


def test_ipv4_mapped_ipv6_is_mapped_back_before_judging() -> None:
    """`::ffff:169.254.169.254` 要先映射回 IPv4 再判。

    不映射的话它既不在 IPv4 网段里、也不在 IPv6 网段里 —— 又是一个绕过。
    """
    assert hostguard.is_blocked_host("::ffff:169.254.169.254")
    # 对照：mapped 之后的**内网**地址仍然放行（映射不能变成"一律拒绝"）
    assert not hostguard.is_blocked_host("::ffff:192.168.1.10")


# ══════════════════════════════════════════════════════════
# ★★ IPv6 link-local（`fe80::/10`）—— 2026-10-16 补的那条
# ══════════════════════════════════════════════════════════


def test_ipv6_link_local_is_blocked() -> None:
    """★★ IPv6 的 link-local（`fe80::/10`）必须与 IPv4 的那段**同等对待**。

    ## 这条补的是什么

    补之前 `BLOCKED_NETS` **只有 IPv4** 的 `169.254.0.0/16`，
    而 IPv6 的 link-local 也是同一类地址 —— 实测 `fe80::1` **被放行**。

    ⚠️ 影响面是**两层**：`is_blocked_host` 同时被
    ① 判据层（`platforms.py` / `llm.py`）与
    ② 连接层（`netguard`）取用 —— 所以漏一处等于两层都漏。
    而且 `netguard._normalize_address` 专门剥了 scope id
    （`fe80::1%eth0`），说明那条路已经预期会见到 IPv6 link-local 形态。
    """
    for host in ("fe80::1", "fe80::", "febf::1", "fe80::1%eth0", "[fe80::1]"):
        assert hostguard.is_blocked_host(host), (
            f"{host!r} 是 IPv6 link-local，必须被拦下 —— "
            "它与 IPv4 的 169.254.0.0/16 是同类地址"
        )


def test_the_fe80_net_boundary_is_exact() -> None:
    """★★ 判据的边界必须**恰好**是 `fe80::/10`，不能宽也不能窄。

    把网段写宽（比如 `/9`）会连坐拦掉 `fec0::1`（已废弃的 site-local）；
    写窄（`/11`）会漏掉 `febf::1`（`fe80::/10` 的上边界）。
    两个方向都实测钉住 —— 只测"拦得住 fe80::1"是分辨不出来的。
    """
    import ipaddress

    net = ipaddress.ip_network("fe80::/10")
    # 上边界之内
    assert ipaddress.ip_address("febf:ffff::1") in net
    # 刚好出界
    assert ipaddress.ip_address("fec0::1") not in net

    assert hostguard.is_blocked_host("febf:ffff::1"), "上边界之内应当被拦"
    assert not hostguard.is_blocked_host("fec0::1"), (
        "`fec0::1` 在 fe80::/10 **之外**，不该被拦 —— 网段写宽了"
    )


def test_the_scope_id_does_not_create_a_bypass() -> None:
    """★ 带 scope id（接口名）的写法不能绕过判据。

    `fe80::1%eth0` 是**真实可达**的写法（`getaddrinfo` 认，实测它返回的
    sockaddr 带 scope id）。而它是"字符串比对认不出、`ipaddress` 也未必
    认"的形态 —— 正是绕过最喜欢的样子。

    实测结论：`ipaddress` 对带 scope 的地址**在网段判断上仍然正确**
    （`IPv6Address('fe80::1%eth0') in fe80::/10` 为 True），所以这一条
    不需要额外处理 —— 但**必须钉住**，因为它是实现细节、换 Python 版本
    或换写法都可能变。
    """
    assert hostguard.is_blocked_host("fe80::1%eth0")
    assert hostguard.is_blocked_host("fe80::1%lo")
    # 对照：带 scope 的**内网**地址仍放行（scope 不能变成"一律拒绝"）
    assert not hostguard.is_blocked_host("fd00::1%eth0")


def test_aws_ipv6_metadata_address_is_blocked() -> None:
    """★ AWS 的 IPv6 元数据地址（`fd00:ec2::254`）要拦。

    它与 `100.100.100.200` 是同一个形状：**一个具体的元数据地址**，
    而不是一整段内网。所以判据用 `/128` —— 不能顺手把 `fd00::/8`
    （ULA 整段）拦掉，那会误伤内网部署。
    """
    assert hostguard.is_blocked_host("fd00:ec2::254")
    # ★ 关键对照：ULA 里的**普通**地址必须放行
    assert not hostguard.is_blocked_host("fd00::1")
    assert not hostguard.is_blocked_host("fd12:3456::1")


def test_ipv4_and_ipv6_nets_do_not_interfere() -> None:
    """★★ 加了 IPv6 判据之后，IPv4 的判定**不受影响**（双向对照）。

    `is_blocked_host` 里按 `addr.version == net.version` 过滤，
    所以两个版本互不干扰。但这是"实现正确"才成立的 —— 这里用**同一批
    地址**在加判据前后应当给出的结果钉住它：

    * IPv4 的元数据地址仍被拦（不能因为加了 IPv6 判据就漏了）；
    * IPv4 的内网/回环仍放行（不能因为加了 IPv6 判据就误拦）。
    """
    for blocked in ("169.254.169.254", "169.254.0.1", "100.100.100.200"):
        assert hostguard.is_blocked_host(blocked), f"{blocked} 是 IPv4 元数据地址"
    for allowed in ("127.0.0.1", "192.168.1.10", "10.0.0.5", "172.18.0.1", "1.2.3.4"):
        assert not hostguard.is_blocked_host(allowed), (
            f"{allowed!r} 被误拦了 —— 加 IPv6 判据不该影响 IPv4 的放行组"
        )


def test_ipv4_mapped_still_works_after_adding_ipv6_nets() -> None:
    """★★ IPv4-mapped 的映射逻辑与**新增的 IPv6 判据**不打架。

    这是主管点名要核的一条：`::ffff:169.254.169.254` 会被映射回 IPv4、
    再判 IPv4 网段。加了 `fe80::/10` 之后要确认：

    * 映射**仍然发生**（它不能因为 IPv6 网段变多而改走 IPv6 分支）；
    * 映射后的结果仍然正确（元数据拦、内网放行）；
    * 而 `::ffff:` 的形态**不会**被误当成 IPv6 link-local
      （实测 `::ffff:169.254.169.254 in fe80::/10` 为 False）。
    """
    # 元数据：拦（走的是映射回 IPv4 那条路）
    assert hostguard.is_blocked_host("::ffff:169.254.169.254")
    # 内网：放行（映射之后是 192.168.x，不在任何被拦网段里）
    assert not hostguard.is_blocked_host("::ffff:192.168.1.10")
    assert not hostguard.is_blocked_host("::ffff:127.0.0.1")
    # IPv4-mapped 形态不属于 IPv6 link-local（不能靠 fe80::/10 误拦）
    import ipaddress

    mapped = ipaddress.ip_address("::ffff:169.254.169.254")
    assert mapped not in ipaddress.ip_network("fe80::/10")
    # ★ 而它**也不**因为"是 IPv6"而被 fe80::/10 放跑 —— 它由 IPv4 那条拦
    assert hostguard.normalize_host("::ffff:169.254.169.254") is not None


# ══════════════════════════════════════════════════════════
# 放行：内网必须继续可用（这是"修法不能做成禁止内网"的闸）
# ══════════════════════════════════════════════════════════


ALLOWED_HOSTS = [
    # ★ 比赛平台很可能就部署在内网 —— 拒绝它们等于这个功能白做
    "192.168.1.10",
    "10.0.0.5",
    "172.18.0.1",  # docker 网关
    "127.0.0.1",  # 本地部署
    # BSD 简写与十进制形式的**回环**地址
    "127.1",
    "2130706433",
    # 正常公网地址
    "1.2.3.4",
    "203.0.113.9",
    # 域名（含官方平台域名）
    "ctf2.dasctf.com",
    "example.com",
    # IPv6 回环
    "::1",
    # ★★ IPv6 的**内网**段必须继续放行（2026-10-16 补）。
    #    加 `fe80::/10` 时最容易连坐的就是这几类 —— 它们是内网/本机，
    #    按本项目口径（见模块 docstring"不做内网限制是刻意的"）该放行。
    "fc00::1",  # ULA（唯一本地地址，内网）
    "fd00::1",  # ULA 的另一半
    "fd12:3456::1",  # ULA 里的普通地址（★ 不是 AWS 元数据那个）
    "::ffff:127.0.0.1",  # IPv4-mapped 的回环
    "::ffff:192.168.1.10",  # IPv4-mapped 的内网
    "2001:db8::1",  # 文档用地址（公网性质）
    # ⚠️ `fec0::1` 是**已废弃**的 site-local（`fe80::/10` 之外）。
    #    加判据时若把网段写宽成 `/9` 之类，它会连坐被拦 —— 这里钉住
    #    "判据的边界就是 fe80::/10"，不多不少。
    "fec0::1",
]


@pytest.mark.parametrize("host", ALLOWED_HOSTS)
def test_internal_targets_are_still_allowed(host: str) -> None:
    """★★ 修法**绝不能**退化成"禁止内网"。

    `platforms.py` 的注释写明这是刻意的：比赛平台很可能就部署在内网，
    而"拒绝内网"会把最可能的使用场景直接挡在门外。

    ⚠️ 这条与上面那组是**一对**：只有两条一起，才能证明修的是
       "等价写法"而不是"把范围扩大成内网全禁"。
    """
    assert not hostguard.is_blocked_host(host), (
        f"{host!r} 不该被拦 —— 本项目刻意不做内网限制（见 platforms.py 的说明）"
    )


def test_empty_and_none_are_not_blocked() -> None:
    """空值不拦（它由"缺少主机名"那条校验负责，不是这一层的活）。"""
    assert not hostguard.is_blocked_host("")
    assert not hostguard.is_blocked_host("   ")


def test_non_ip_looking_hosts_return_none() -> None:
    """归一化对域名返回 `None` —— 这是"两种判据并存"的前提。"""
    assert hostguard.normalize_host("metadata.google.internal") is None
    assert hostguard.normalize_host("ctf2.dasctf.com") is None
    # 而域名形态的拦截**仍然生效**（靠字符串那条）
    assert hostguard.is_blocked_host("metadata.google.internal")


# ══════════════════════════════════════════════════════════
# 入口：assert_safe_base_url 真的接上了这一层
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://2852039166/latest/meta-data/",
        "http://0xA9FEA9FE/latest/meta-data/",
        "http://[::ffff:169.254.169.254]/latest/meta-data/",
        "http://169.254.169.254:80/latest/meta-data/",
        "http://100.100.100.200/latest/meta-data/",
    ],
)
def test_assert_safe_base_url_rejects_the_bypasses(url: str) -> None:
    """★ 端到端：绕过写法进了 `assert_safe_base_url` 也要被拒。

    上面测的是 `hostguard` 这个**零件**；这一条测的是它**装上了**。
    只测零件的话，"忘了改调用点"这个错误完全测不出来。
    """
    from app.core.errors import ApiError
    from app.ctf2.platforms import assert_safe_base_url

    with pytest.raises(ApiError) as excinfo:
        assert_safe_base_url(url)

    # `ApiError.validation` 是 **400**（不是 422）—— 见 `core/errors.py`。
    assert excinfo.value.status_code == 400
    assert "元数据" in excinfo.value.message


@pytest.mark.parametrize(
    "url",
    [
        "http://xn--.invalid/x",
        "http://xn--0.invalid/x",
        "http://xn--a.invalid/x",
    ],
)
def test_assert_safe_base_url_rejects_malformed_idna_labels(url: str) -> None:
    """★★ R-4 回归：畸形 A-label 必须以 400 拒绝，不能逃逸成 500。

    这一条守的是 `.host` 的**访问位置**：`httpx.URL(...)` 的构造是惰性的、
    不抛，畸形 A-label 要到第一次访问 `.host` 时才抛 `idna.IDNAError`。
    本函数里 `parsed.host` 出现多次，**只要有一处落在 `try` 之外**，
    异常就会穿过本函数 → `create_building` → `POST /ctf2/platforms/draft`
    → 全局 `@app.exception_handler(Exception)` → 500。

    反向验证（把 `host = parsed.host` 从 try 里挪出去）必须让这一条变红。

    ⚠️ 为什么不用 `hostguard` 那批绕过写法代替：那批是**字符串比对**漏的
    （`2852039166` 这类等价写法），与本条是**完全不同的机制**，
    两边互不覆盖 —— 那批全绿时本条照样能红。
    """
    from app.core.errors import ApiError
    from app.ctf2.platforms import assert_safe_base_url

    with pytest.raises(ApiError) as excinfo:
        assert_safe_base_url(url)

    assert excinfo.value.status_code == 400
    assert "不是一个合法的 URL" in excinfo.value.message


def test_assert_safe_base_url_keeps_allowing_internal_platforms() -> None:
    """★ 端到端：内网平台地址照旧通过（不误伤最可能的用法）。"""
    from app.ctf2.platforms import assert_safe_base_url

    for url in (
        "http://192.168.1.10:8080/api",
        "http://10.0.0.5/api",
        "http://172.18.0.1:8000/api",
        "http://127.0.0.1:5173/api",
    ):
        assert assert_safe_base_url(url) == url
