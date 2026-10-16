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
