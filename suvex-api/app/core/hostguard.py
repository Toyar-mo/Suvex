"""主机判据：**一处定义"哪些地址不能打"，所有出站校验都从这里取**。

## 为什么单独一个模块

因为**依赖方向**。两个调用方在依赖链的两头：

* `app/kernel/platforms.py` 在最上游（它 import `adapters.builder`，
  后者 import `adapters.generic`，再 import `adapters.transport`）——
  如果 `transport.py` / `probe.py` 反过来 import `platforms`，就成环了，
  而环在 import 期炸，症状是"某个不相关的模块 import 失败"。
* `app/providers/llm.py` 与 `app/kernel/` **互相不认识**，让 provider 那条
  去 import `app.kernel` 会把两个领域绑在一起。

所以判据落在 `app/core/`（**叶子层**：它只 import 标准库，谁都能 import 它）。

## ★ 为什么是"一份清单"而不是各写各的

这个模块的前身是 `app/kernel/hostguard.py`，而 `app/providers/llm.py` 里有
**自己一份** `BLOCKED_HOSTS`。两份清单已经漂移了：provider 那份有
`169.254.170.2`（AWS ECS 任务元数据），ctf2 那份没有 —— 也就是**同一类
地址，在一条路径上被拦、在另一条上放行**。清单一旦分叉就一定会继续分叉，
所以现在只有这一份，两边都从这里取。

## ★ 为什么"等价写法"必须归一化（这是本模块存在的主要原因）

判据如果写成 `host in {"169.254.169.254"}` 这样的**字符串比对**，
那么 `2852039166`、`0xA9FEA9FE`、`[::ffff:169.254.169.254]` 这些
**`getaddrinfo` 全都认**的等价写法会一个都不命中 —— 全部放行。
所以先把主机名归一化成一个 `ipaddress` 对象，再判**网段**。

## 判据的形状：**白名单式**，不是往黑名单里加条目

这里只回答一个问题：**"这个主机是不是我们明确拒绝的？"**

刻意**不**做"禁止内网"：比赛平台很可能就部署在内网
（`192.168.x.x` / `10.x.x.x` / docker 网关 `172.18.0.1` / 本机 `127.0.0.1`），
拒绝内网会把最可能的使用场景直接挡在门外 —— 那等于这个功能白做。
见 `platforms.py` 里同一段说明。

## ★ 为什么要做"等价写法"归一化

判据如果写成 `host in {"169.254.169.254"}` 这样的**字符串比对**，
那么 `2852039166`、`0xA9FEA9FE`、`[::ffff:169.254.169.254]` 这些
**`getaddrinfo` 全都认**的等价写法会一个都不命中 —— 全部放行。
所以先把主机名归一化成一个 `ipaddress` 对象，再判**网段**。

三条归一化分支（`ipaddress` 只认第一种，另两种得自己兜）：

1. 标准点分 / IPv6 → `ipaddress.ip_address(h)`
2. 纯整数 / 十六进制（`2852039166`、`0xA9FEA9FE`）→ `ipaddress.ip_address(int(h, 0))`
3. BSD 简写 / 八进制点分（`127.1`、`0177.0.0.1`）→ `socket.inet_aton(h)`

⚠️ **归一化不出来不等于安全**，只是"这不是一个字面量 IP"（域名走
`BLOCKED_HOSTS` 那条分支）。两者**并存**，不是替换。
"""

from __future__ import annotations

import ipaddress
import socket

#: 明确拒绝的**主机名**（域名形态）。
#:
#: 这一条**不能删**：`metadata.google.internal` / `metadata.goog` 是**域名**，
#: 下面的 IP 归一化分支对它们返回 `None`（它们不是字面量 IP），
#: 删掉就等于把它们放过去。
BLOCKED_HOSTS: frozenset[str] = frozenset(
    {
        "169.254.169.254",  # AWS / GCP / Azure / 阿里云共用的元数据地址
        "169.254.170.2",  # AWS ECS 任务元数据（原先只在 provider 那份清单里）
        "metadata.google.internal",
        "metadata.goog",
        "100.100.100.200",  # 阿里云元数据
    }
)

#: 明确拒绝的**网段**（字面量 IP 归一化之后判这里）。
#:
#: 只拦三类，**不是**"禁止内网"（理由见模块 docstring）：
#:
#: * `169.254.0.0/16` —— **IPv4** link-local。云厂商的元数据服务都落在
#:   这一段（AWS/GCP/Azure/阿里云 = `169.254.169.254`，AWS ECS =
#:   `169.254.170.2`），而"让服务端替你打一个 URL"正是这个功能的形状，
#:   一次 GET 就能换到云主机的临时凭据。所以这一段必须堵死。
#: * `fe80::/10` —— **IPv6** link-local（2026-10-16 补）。
#:   与上面那一段**同类**：IPv6 里 link-local 也是 `fe80::/10`，
#:   而"域名解析到 `fe80::1`"或直接填 `[fe80::1]` 都是可达的写法。
#:   ★ 补它之前这里**只有 IPv4** —— 而 `netguard` 的连接层判据
#:   也取自本模块，所以那是**两层同时漏**的一条（实测 `fe80::1` 被放行）。
#: * `100.100.100.200/32` —— 阿里云元数据的另一个地址。
#: * `fd00:ec2::254/128` —— **AWS 的 IPv6 元数据地址**（2026-10-16 补）。
#:   ⚠️ 注意它与"ULA 该放行"**不冲突**：`fd00::/8` 整段是 ULA（内网，
#:   按本项目口径该放行），而这里只钉 **AWS 元数据那一个具体地址**
#:   （`/128`），与 `100.100.100.200/32` 是同一个形状 ——
#:   不是"把整段 ULA 拦掉"。
BLOCKED_NETS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("100.100.100.200/32"),
    ipaddress.ip_network("fd00:ec2::254/128"),
)


def normalize_host(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """把主机名归一化成一个 IP 对象；**不是字面量 IP 就返回 `None`**。

    `None` 的含义是"这看起来是个域名"（或无法解析的东西），
    调用方应回退到 `BLOCKED_HOSTS` 的字符串比对，而不是当成安全。

    规整顺序（每一条都对应一种实测能绕过的写法）：
    `[]` 剥离（IPv6 字面量在 URL 里的写法）、尾点剥离（`169.254.169.254.`
    在 DNS 里等价于不带点）、IPv4-mapped IPv6 映射回 IPv4
    （`::ffff:169.254.169.254`）。
    """
    raw = (host or "").strip()
    if not raw:
        return None
    # IPv6 字面量在 URL 里是 `[::1]` 形态；`httpx.URL.host` 已经剥过一层，
    # 但调用方可能传原始字符串，所以这里再兜一次。
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    # 尾点：`example.com.` 与 `example.com` 在 DNS 里等价。
    raw = raw.rstrip(".")
    if not raw:
        return None

    # 分支 1：标准点分 / IPv6。
    try:
        return ipaddress.ip_address(raw)
    except ValueError:
        pass

    # 分支 2：纯整数 / 十六进制。
    #
    # ⚠️ 只在**整体看起来像一个整数**时才走（`int(h, 0)` 对 `"12abc"` 这类
    #    会抛，但对 `"0"` 这类合法整数会成功 —— 那是 `0.0.0.0`，判它无妨）。
    try:
        return ipaddress.ip_address(int(raw, 0))
    except (ValueError, TypeError):
        pass

    # 分支 3：BSD 简写 / 八进制点分（`127.1`、`0251.0376.0251.0376`）。
    try:
        return ipaddress.ip_address(socket.inet_aton(raw))
    except OSError:
        pass

    return None


def is_blocked_host(host: str) -> bool:
    """这个主机是不是明确拒绝的地址。**两种判据并存**。

    1. 字符串比对（域名形态，`BLOCKED_HOSTS`）；
    2. 归一化成 IP 之后判网段（等价写法，`BLOCKED_NETS`）。

    ⚠️ 顺序上先做 2 再做 1 也行，但两条都要在 —— 只留一条就会漏掉
    另一半（域名漏在 IP 分支、等价写法漏在字符串分支）。
    """
    raw = (host or "").strip().lower()
    if not raw:
        return False

    # ① 域名/字面量字符串比对。尾点也要剥（`169.254.169.254.` 与不带点等价）。
    if raw.rstrip(".") in BLOCKED_HOSTS:
        return True

    # ② 归一化成 IP，再判网段。
    addr = normalize_host(raw)
    if addr is None:
        return False
    # IPv4-mapped IPv6（`::ffff:169.254.169.254`）先映射回 IPv4，
    # 否则它既不在 IPv4 网段里、也不在 IPv6 网段里 —— 又是一个绕过。
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return any(addr in net for net in BLOCKED_NETS if addr.version == net.version)


__all__ = ["BLOCKED_HOSTS", "BLOCKED_NETS", "is_blocked_host", "normalize_host"]
