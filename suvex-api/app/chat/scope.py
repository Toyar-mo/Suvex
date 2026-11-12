"""对话沙箱的出站目标守卫（v1.89）。

## 它挡的是什么

`run_shell` / `browser` 跑在**无限制档**沙箱里（可出网、root、可写）。
在此之前，模型想访问哪个网络目标都没有任何拦阻 —— 而同一平台里的
`create_scan` 是有的（`scans.service.check_authorization` 要逐字确认
授权声明）。于是"顺手扫一下这个网段"只需要一句自然语言，绕开了
整个授权流程。

这个模块把同一道闸拦在**工具调用之前**：命令里出现的目标必须落在
"已授权集合"里，否则不执行，并把可行动的理由交回模型。

## 已授权集合从哪来（按优先级）

1. **平台上已建过扫描的目标**（`scans.target` 且 `authorized_by` 非空）——
   建扫描那次请求已经逐字确认过授权声明，这是最有据可查的来源；
2. 运维在 `SUPEX_CHAT_SHELL_ALLOWED_HOSTS` 里列的主机/CIDR（逗号分隔）；
3. 内置的基础设施白名单（包管理镜像、代码托管、公共 DNS）——
   装不了包、下载不了的沙箱等于废的；
4. 环回地址（容器内的 127.0.0.0/8，含 Docker 内置 DNS `127.0.0.11`）。

## ⚠️ 它挡不住什么（如实写在这里，不要读成"出站已经封死"）

* 把目标藏进变量、base64、循环生成 —— 本模块的文本提取看不见它们；
  但守卫开启时 `egress.py` 会按容器源 IP 在 `DOCKER-USER` 上只放行
  本模块算出的授权集合（域名按下发时的解析结果），其余出站流量 DROP；
* 域名解析出来的内网地址：文本层不做 DNS 解析；网络层在下发规则时
  由宿主解析授权域名，因此它放行的是**当前解析结果**，域名换 IP 后
  要等下一次工具调用重新同步；
* `browser` 除了 `open` 之外的子命令（点击跳转、`eval` 里的 fetch）——
  它们不经过这里；
* `verify_http` 仍只做 SSRF 守卫（`GuardedHTTPTransport`）。它没有 target
  绑定，要补得先回答"验证对象算不算授权" —— 那是产品语义，不是这里能猜的。
* 规则下发前已经建立的连接不会被追溯切断；它挡的是新的出站连接。

所以它的定位是：**文本层挡误操作，网络层挡藏起来的地址；两者一起才构成边界**。
整体关掉：`SUPEX_CHAT_SHELL_SCOPE_GUARD=0`。
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from typing import Any, NamedTuple

from sqlalchemy import select
from sqlalchemy.orm import Session

#: 内置基础设施白名单：不装包、不下载、不查资料的沙箱等于废的。
#:
#: ★ 这份清单**故意短**：每多一个域名，就多一条"可以拿它当中转"的路。
#:   运维要加，用 `SUPEX_CHAT_SHELL_ALLOWED_HOSTS` —— 那是显式动作，
#:   与这里的默认值分开，出问题时能一眼看出"是我们给的还是你加的"。
DEFAULT_ALLOWED_HOSTS: frozenset[str] = frozenset(
    {
        # 包管理（Kali / Debian / Ubuntu）
        "http.kali.org",
        "archive.kali.org",
        "security.kali.org",
        "deb.debian.org",
        "security.debian.org",
        "archive.ubuntu.com",
        "security.ubuntu.com",
        # Python / Node / Go / Rust
        "pypi.org",
        "pypi.python.org",
        "files.pythonhosted.org",
        "registry.npmjs.org",
        "proxy.golang.org",
        "sum.golang.org",
        "crates.io",
        "static.crates.io",
        "index.crates.io",
        # 代码托管与原始内容
        "github.com",
        "api.github.com",
        "codeload.github.com",
        "raw.githubusercontent.com",
        "objects.githubusercontent.com",
        "gitlab.com",
        # 公共 DNS（容器默认走 127.0.0.11，这里管 `dig @x` 那种写法）
        "dns.google",
        "one.one.one.one",
    }
)

#: 内置放行的地址段：只放环回。
#:
#: ⚠️ 不要顺手加 `10.0.0.0/8` / `192.168.0.0/16` —— 那正好是这套守卫
#:   存在的全部理由。私网目标要按"已建扫描"逐条授权。
DEFAULT_ALLOWED_CIDRS: frozenset[str] = frozenset({"127.0.0.0/8", "::1/128"})

#: 当作域名时排除的"最后一段"：这些是文件扩展名，不是 TLD。
#:
#: ## 为什么用扩展名黑名单，而不是 TLD 白名单
#:
#: 靶场与内网里的域名千奇百怪（`.local`、`.corp`、`.lab`、`.test`、
#: 甚至 `.internal`），白名单一定会漏；而漏一个的表现是**守卫形同虚设**。
#: 反过来，命令里出现"看起来像域名"的东西，绝大多数是文件名
#: （`exploit.py`、`archive.tar.gz`），把它们排除掉就够用了。
_FILE_EXTS: frozenset[str] = frozenset(
    {
        "py", "js", "ts", "tsx", "jsx", "json", "sh", "bash", "zsh", "txt", "md",
        "png", "jpg", "jpeg", "gif", "svg", "webp", "pdf", "zip", "tar", "gz",
        "bz2", "xz", "7z", "rar", "log", "html", "htm", "css", "yml", "yaml",
        "toml", "ini", "conf", "cfg", "csv", "xml", "bin", "so", "dll", "exe",
        "pyc", "whl", "deb", "rpm", "apk", "php", "rb", "pl", "c", "h", "cpp",
        "hpp", "go", "rs", "java", "class", "jar", "lock", "sql", "db", "sqlite",
        "pcap", "pcapng", "cap", "key", "pem", "crt", "cer", "pub", "bak", "tmp",
    }
)

#: URL 里的 host 段（含端口）：`https://host:8080/path` → `host:8080`。
_URL_HOST_RE = re.compile(r"(?i)\bhttps?://([^/?#\s'\"`<>$;|&(){}\\,]+)")

#: 裸 IPv4（可带 /mask 或 :port）。
_IPV4_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?:/(\d{1,2}))?(?::(\d{1,5}))?(?![\d.])")

#: 认得出的 TLD。**白名单而不是"看起来像域名"**：
#:
#: 代码里到处是点号标识符（`urllib.request.urlopen`、`resp.status`、
#: `e.code`），只按"最后一段是 2–24 个字母"判域名，会把整段 Python
#: 当成一串目标 —— 实测一次命令能列出八个"目标"并整条拒绝执行。
#: 白名单会漏掉生僻 TLD，但漏掉的后果是"少拦一次"，
#: 误判的后果是"正常命令直接不能跑"，后者严重得多。
_TLDS: frozenset[str] = frozenset(
    {
        "com", "net", "org", "edu", "gov", "mil", "int", "info", "biz", "name",
        "io", "co", "dev", "app", "ai", "cloud", "tech", "site", "online", "shop",
        "xyz", "top", "me", "cc", "tv", "pro", "mobi", "asia", "club", "store",
        "local", "internal", "intranet", "corp", "home", "lan", "lab", "test", "arpa",
        "cn", "hk", "tw", "jp", "kr", "sg", "my", "th", "vn", "id", "ph", "in",
        "ru", "su", "de", "fr", "uk", "nl", "se", "no", "fi", "pl", "it", "es",
        "pt", "gr", "tr", "il", "ch", "at", "be", "cz", "dk", "ie", "ro", "hu",
        "us", "ca", "mx", "br", "ar", "cl", "pe", "au", "nz", "za", "eu",
    }
)

#: 裸域名（可带 :port）。`@` 在 lookbehind 里**不排除** —— `ssh user@host`
#: 是最常见的写法之一，排除它等于把那类命令整条漏掉。
_HOST_RE = re.compile(
    r"(?i)(?<![\w./-])((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+([a-z]{2,24}))"
    r"(?::(\d{1,5}))?(?![\w-])"
)


class Decision(NamedTuple):
    """一次守卫判定。`considered` 是提取到的目标，用于日志与测试。"""

    allowed: bool
    offending: tuple[str, ...]
    considered: tuple[str, ...]


def _strip_port(host: str) -> str:
    """去掉 `:port`（只处理 `host:port`；冒号多的是 IPv6，直接原样返回）。"""
    if host.count(":") == 1:
        head, _, tail = host.partition(":")
        if tail.isdigit():
            return head
    return host


def normalize_entry(raw: str) -> tuple[str | None, str | None]:
    """把一条"授权目标"拆成 `(host, cidr)`。

    同一列里可能是 `http://10.0.0.5:8080`、`10.0.0.5:8080`、
    `10.0.0.0/24`、`github.com/Toyar-mo/Suvex`、`/root/suvex` —— 扫描的
    `target` 列本来就允许多种形状，所以这里只做"能拆就拆"，拆不出来
    就返回 `(None, None)`：**宁可少授权，不要错授权**。
    """
    text = (raw or "").strip()
    if not text:
        return None, None

    if "://" in text:
        from urllib.parse import urlsplit

        try:
            host = urlsplit(text).hostname or ""
        except ValueError:
            return None, None
        text = host
        if not text:
            return None, None

    head = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].strip()
    if not head:
        return None, None

    # CIDR：`10.0.0.0/24`、`10.0.0.5/32`
    if "/" in text:
        try:
            return None, str(ipaddress.ip_network(text, strict=False))
        except ValueError:
            pass

    # IPv6 目标（`[::1]:8080` 或裸地址）：只保留环回那一个，其余不猜。
    if head.startswith("["):
        bare = head[1 : head.find("]")] if "]" in head else ""
        return ("::1", None) if bare == "::1" else (None, None)
    if head.count(":") > 1:
        return ("::1", None) if head == "::1" else (None, None)

    return _strip_port(head).lower().strip("."), None


def _split_allow(setting_value: str) -> tuple[set[str], set[str]]:
    hosts: set[str] = set()
    cidrs: set[str] = set()
    for item in (setting_value or "").replace(";", ",").split(","):
        host, cidr = normalize_entry(item)
        if host:
            hosts.add(host)
        if cidr:
            cidrs.add(cidr)
    return hosts, cidrs


def settings_allowlist() -> tuple[set[str], set[str]]:
    """运维显式放行的主机/CIDR（`SUPEX_CHAT_SHELL_ALLOWED_HOSTS`）。"""
    from app.core.config import get_settings

    return _split_allow(get_settings().chat_shell_allowed_hosts)


def authorized_targets(db: Session | None) -> tuple[set[str], set[str]]:
    """平台上**已授权**的扫描目标 → `(hosts, cidrs)`。

    ## 为什么判据是 `authorized_by` 而不是"有没有扫描记录"

    扫描行可以是 CTF2 建壳、定时计划重跑产生的（`scans.models` 里专门
    记过这条：`origin` 才是区分来源的字段，`authorized_by` 单看会混）。
    但这里要问的是"有没有人对此目标做过授权动作"，而 CTF2 建壳与
    定时重跑**都**继承了计划/建壳时的授权声明 —— 所以 `authorized_by`
    非空就足够回答这个问题。真正的"谁授权的"由 `authorized_by` 的值回答。

    ## `db is None`

    测试替身（`tests/chat/test_sandbox.py` 的 `FakeCtx`）没有数据库。
    这时返回空集：**没有记录 = 没有授权**，守卫只会更严，不会更松。
    绝不要反过来写成"拿不到就放行" —— 那是把守卫变成装饰。
    """
    from app.scans.models import Scan

    if db is None:
        return set(), set()

    rows = db.execute(
        select(Scan.target).where(Scan.authorized_by.is_not(None)).distinct()
    ).scalars()
    hosts: set[str] = set()
    cidrs: set[str] = set()
    for target in rows:
        host, cidr = normalize_entry(target)
        if host:
            hosts.add(host)
        if cidr:
            cidrs.add(cidr)
    return hosts, cidrs


def extract_targets(text: str) -> tuple[set[str], set[str]]:
    """从一段命令/URL 里提取 `(hosts, cidrs)`。

    ## 提取规则（顺序有意义）

    1. `scheme://host...` —— 先取 authority，避免被后面的裸域名规则重复算；
    2. 裸 IPv4，可带 `/mask` 或 `:port`；
    3. 裸域名，可带 `:port`，最后一段是文件扩展名的排除掉。

    ## 已知的漏

    IPv6（除环回）、`http://[::1]:8080` 之外的方括号地址、以及任何
    拼出来的字符串都不在这里。见模块 docstring「它挡不住什么」。
    """
    hosts: set[str] = set()
    cidrs: set[str] = set()
    if not text:
        return hosts, cidrs

    for match in _URL_HOST_RE.finditer(text):
        authority = match.group(1)
        # `scheme://user@host/` 或带口令的写法 —— 凭据不是目标的一部分，
        # 先去掉 `@` 之前那段再判。
        if "@" in authority:
            authority = authority.rsplit("@", 1)[1]
        if authority.count(":") > 1 and not authority.startswith("["):
            continue  # 裸 IPv6，不猜
        host = _strip_port(authority).lower().strip(".")
        if host:
            hosts.add(host)

    for match in _IPV4_RE.finditer(text):
        octets, mask = match.group(1), match.group(2)
        try:
            ipaddress.IPv4Address(octets)
        except ValueError:
            continue
        if mask is not None:
            try:
                cidrs.add(str(ipaddress.ip_network(f"{octets}/{mask}", strict=False)))
            except ValueError:
                hosts.add(octets)
            continue
        hosts.add(octets)

    for match in _HOST_RE.finditer(text):
        host, tld = match.group(1).lower(), match.group(2).lower()
        # 文件扩展名与"不是 TLD"的点号标识符都不算目标（见 `_TLDS` 的说明）。
        if tld in _FILE_EXTS or tld not in _TLDS:
            continue
        hosts.add(host)

    return hosts, cidrs


def _prepare(
    hosts: Iterable[str], cidrs: Iterable[str]
) -> tuple[set[str], list[ipaddress._BaseNetwork]]:
    """把授权集合预算成可反复比对的两份：主机名集合、网段列表。"""
    allowed_hosts = {h.lower().lstrip("*.").strip(".") for h in hosts}
    nets: list[ipaddress._BaseNetwork] = []
    for cidr in cidrs:
        try:
            nets.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            continue
    return allowed_hosts, nets


def _match(bare: str, allowed_hosts: set[str], nets: list[ipaddress._BaseNetwork]) -> bool:
    """一个已归一的目标是否命中授权集合。"""
    try:
        ip = ipaddress.ip_address(bare)
    except ValueError:
        ip = None

    if ip is not None:
        if str(ip) in allowed_hosts:
            return True
        return any(ip.version == net.version and ip in net for net in nets)

    if bare in allowed_hosts:
        return True
    # `*.example.com` 在 `normalize_entry` 后存成 `example.com`，
    # 这里按后缀匹配 —— 通配只能由**显式配置**产生，扫描目标不会带 `*`。
    return any(bare.endswith("." + a) for a in allowed_hosts)


def is_allowed(
    host: str,
    *,
    hosts: Iterable[str],
    cidrs: Iterable[str],
) -> bool:
    """单个目标是否在授权集合内（host 精确 / CIDR 包含 / `*.x` 通配）。"""
    allowed_hosts, nets = _prepare(hosts, cidrs)
    return _match(_strip_port(host).lower().strip("."), allowed_hosts, nets)


def evaluate(
    text: str,
    *,
    hosts: Iterable[str],
    cidrs: Iterable[str],
) -> Decision:
    """判定一段文本里出现的所有目标是否都在授权集合内。"""
    seen_hosts, seen_cidrs = extract_targets(text)
    allowed_hosts, nets = _prepare(hosts, cidrs)

    offending: list[str] = [
        host for host in sorted(seen_hosts) if not _match(host, allowed_hosts, nets)
    ]

    for cidr in sorted(seen_cidrs):
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        # 命令里写了网段：要求整个网段都被授权（部分覆盖等于没覆盖）。
        if not any(net.version == outer.version and net.subnet_of(outer) for outer in nets):
            offending.append(f"{cidr}（网段）")

    return Decision(
        allowed=not offending,
        offending=tuple(offending),
        considered=tuple(sorted(seen_hosts)) + tuple(sorted(seen_cidrs)),
    )


def allowlist(ctx: Any) -> tuple[set[str], set[str]]:
    """本次调用允许的主机/CIDR（扫描授权 ∪ 运维放行 ∪ 内置设施）。

    文本守卫与网络层守卫共用这一份，避免两处各拼一遍后悄悄分叉。
    """
    hosts, cidrs = authorized_targets(getattr(ctx, "db", None))
    extra_hosts, extra_cidrs = settings_allowlist()
    return (
        hosts | extra_hosts | set(DEFAULT_ALLOWED_HOSTS),
        cidrs | extra_cidrs | set(DEFAULT_ALLOWED_CIDRS),
    )


def reject_reason(ctx: Any, *, text: str, tool: str) -> str | None:
    """守卫的对外入口：`None` = 放行，否则返回给模型的失败结果。

    ## 为什么失败结果是"告诉模型该怎么做"，而不是一句"拒绝"

    模型的下一步完全由这句话决定。只写"拒绝"它会换个写法重试 ——
    而"换个写法"正是这套守卫最不希望发生的事（绕过去等于没守卫）。
    所以三件事必须写清楚：哪个目标、为什么不在范围内、正确路径是什么。
    """
    from app.core.config import get_settings

    if not get_settings().chat_shell_scope_guard:
        return None

    allowed_hosts, allowed_cidrs = allowlist(ctx)
    decision = evaluate(text, hosts=allowed_hosts, cidrs=allowed_cidrs)
    if decision.allowed:
        return None

    targets = "、".join(decision.offending)
    return (
        f"失败：`{tool}` 要访问的目标不在授权范围内：{targets}。\n"
        "平台的授权只认「已建过扫描的目标」（建扫描要逐字确认授权声明）"
        "或运维在 `SUPEX_CHAT_SHELL_ALLOWED_HOSTS` 里显式放行的主机。\n"
        "正确做法：先对这个目标建扫描（`create_scan`），或让用户确认授权后再继续。\n"
        "不要换一种写法重试 —— 检查没通过就是没有授权。"
    )


__all__ = [
    "DEFAULT_ALLOWED_CIDRS",
    "DEFAULT_ALLOWED_HOSTS",
    "Decision",
    "allowlist",
    "authorized_targets",
    "evaluate",
    "extract_targets",
    "is_allowed",
    "normalize_entry",
    "reject_reason",
    "settings_allowlist",
]
