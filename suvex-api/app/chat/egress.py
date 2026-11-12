"""对话沙箱的**网络层**出站边界（与文本守卫配套）。

文本守卫（`scope.py`）只看命令字符串，挡不住 base64、变量拼接、脚本里
再解析出来的地址。真正的边界必须在网络层：按容器源 IP 在
`DOCKER-USER` 上挂一条链，只放行「已授权主机/CIDR」解析出的 IPv4 地址，
其余转发流量一律 DROP。

## 生效条件

只在 `SUPEX_CHAT_SHELL_SCOPE_GUARD=1` 时启用 —— 它和文本守卫是同一道
策略的两种执行方式，不能一个开一个关（否则界面说"已开启"，实际只挡了
命令字面量）。

## 域名 → IP

规则按 IP 下发，所以授权域名在**下发时**用宿主解析器解析一次；容器内
DNS 仍可用（Docker 内嵌 DNS 走 NAT，不经过 FORWARD）。CDN 换 IP 后需要
下一次同步；每次工具调用会比对授权集合与容器 IP，变了才重刷。
这是如实记录的残余差异：它挡的是"连到未授权地址"，不是"永远解析到同一个 IP"。

## 原子性

新规则先写进一条新链（`SXEG-<容器哈希>-<代次>`），再插入跳转，最后删
旧跳转与旧链。避免"先 flush 再写"那段链为空、默认放行的窗口。

## 非 root / 无 iptables 环境

抛 `EgressError`。调用方在守卫开启时**失败关闭**：拒绝执行并说明原因，
而不是悄悄退回文本守卫 —— 那会让人以为网络边界已经生效。
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import secrets
import shlex
import socket
import subprocess
import threading
from collections.abc import Iterable

_log = logging.getLogger(__name__)

COMMENT_PREFIX = "sxeg:"
CHAIN_PREFIX = "SXEG-"
_LOCK = threading.Lock()
#: 容器名 → (容器 IP, 授权主机集合, 授权 CIDR 集合, 链名)。
_APPLIED: dict[str, tuple[str, frozenset[str], frozenset[str], str]] = {}


class EgressError(RuntimeError):
    """网络层规则下发失败。"""


def _run(argv: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise EgressError(f"{' '.join(argv)} 执行失败：{exc}") from exc


def _iptables(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = _run(["iptables", *args])
    if check and result.returncode != 0:
        raise EgressError((result.stderr or result.stdout or "iptables 失败").strip())
    return result


def _container_ip(name: str) -> str | None:
    result = _run(
        [
            "docker",
            "inspect",
            "-f",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}",
            name,
        ]
    )
    if result.returncode != 0:
        raise EgressError((result.stderr or "容器不存在").strip())
    for part in result.stdout.split():
        try:
            ip = ipaddress.ip_address(part)
        except ValueError:
            continue
        if ip.version == 4:
            return str(ip)
    return None


def _resolve(hosts: Iterable[str], cidrs: Iterable[str]) -> list[str]:
    """主机/CIDR → 去重后的 IPv4 网段（`/32` 也是网段）。"""
    nets: list[ipaddress.IPv4Network] = []
    for raw in cidrs:
        try:
            net = ipaddress.ip_network(raw, strict=False)
        except ValueError:
            continue
        if net.version == 4:
            nets.append(net)
    for raw in hosts:
        host = raw.strip()
        if not host:
            continue
        if "/" in host:
            try:
                net = ipaddress.ip_network(host, strict=False)
            except ValueError:
                continue
            if net.version == 4:
                nets.append(net)
            continue
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            try:
                infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
            except OSError:
                # 解析不了就不放行：网络层守卫宁可少放，也不能把域名写错后
                # 变成一个永远匹配不上的空规则却看起来"已允许"。
                _log.info("出站白名单域名无法解析，跳过：%s", host)
                continue
            for info in infos:
                nets.append(ipaddress.ip_network(f"{info[4][0]}/32", strict=False))
            continue
        if addr.version == 4:
            nets.append(ipaddress.ip_network(f"{addr}/32", strict=False))
    return sorted({str(net) for net in nets})


def _chain_for(name: str) -> str:
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:12]
    return f"{CHAIN_PREFIX}{digest}"


def _rules_with_comment(name: str) -> list[list[str]]:
    """`DOCKER-USER` 里属于这个容器的跳转规则（已拆成参数）。"""
    result = _iptables("-S", "DOCKER-USER", check=False)
    if result.returncode != 0:
        raise EgressError((result.stderr or "读不到 DOCKER-USER").strip())
    comment = f"{COMMENT_PREFIX}{_chain_for(name)}"
    out: list[list[str]] = []
    for line in result.stdout.splitlines():
        if "--comment" not in line or comment not in line:
            continue
        text = line[len("-A DOCKER-USER ") :] if line.startswith("-A DOCKER-USER ") else ""
        if text:
            out.append(shlex.split(text))
    return out


def _delete_chain(chain: str) -> None:
    _iptables("-F", chain, check=False)
    _iptables("-X", chain, check=False)


def _chains_for(name: str) -> list[str]:
    """本容器残留的所有代次链（含进程崩溃前的）。"""
    prefix = f"{_chain_for(name)}-"
    result = _iptables("-S", check=False)
    if result.returncode != 0:
        return []
    return [
        line[3:]
        for line in result.stdout.splitlines()
        if line.startswith("-N ") and line[3:].startswith(prefix)
    ]


def apply(name: str, hosts: Iterable[str], cidrs: Iterable[str]) -> None:
    """给容器 `name` 安装/刷新出站白名单。授权集合没变时直接返回。"""
    ip = _container_ip(name)
    if ip is None:
        raise EgressError("容器没有 IPv4 地址，无法安装出站规则")

    host_set = frozenset(hosts)
    cidr_set = frozenset(cidrs)
    with _LOCK:
        cached = _APPLIED.get(name)
        # 规则没变。仍然确认跳转还在（容器重启可能被 Docker 清了链）。
        if (
            cached
            and cached[0] == ip
            and cached[1] == host_set
            and cached[2] == cidr_set
            and _iptables("-C", "DOCKER-USER", "-s", ip, "-j", cached[3], check=False).returncode == 0
        ):
            return

        nets = _resolve(host_set, cidr_set)
        old_jumps = _rules_with_comment(name)
        chain = f"{_chain_for(name)}-{secrets.token_hex(3)}"
        try:
            _iptables("-N", chain)
        except EgressError:
            _delete_chain(chain)
            _iptables("-N", chain)
        try:
            _iptables("-A", chain, "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT")
            for net in nets:
                _iptables("-A", chain, "-d", net, "-j", "ACCEPT")
            _iptables("-A", chain, "-j", "DROP")
            _iptables(
                "-I", "DOCKER-USER", "1",
                "-s", ip, "-j", chain,
                "-m", "comment", "--comment", f"{COMMENT_PREFIX}{_chain_for(name)}",
            )
        except EgressError:
            _delete_chain(chain)
            raise

        # 新的跳转已经生效，再清掉这个容器的旧跳转与旧链。
        for args in old_jumps:
            _iptables("-D", "DOCKER-USER", *args, check=False)
        for old in _chains_for(name):
            if old != chain:
                _delete_chain(old)
        _APPLIED[name] = (ip, host_set, cidr_set, chain)
        _log.info("对话沙箱出站白名单已生效：%s ip=%s 网段=%d", name, ip, len(nets))


def clear(name: str) -> None:
    """容器销毁时摘掉它的规则。幂等，失败只记日志。"""
    with _LOCK:
        _APPLIED.pop(name, None)
        try:
            for args in _rules_with_comment(name):
                _iptables("-D", "DOCKER-USER", *args, check=False)
            for chain in _chains_for(name):
                _delete_chain(chain)
        except EgressError as exc:
            _log.warning("清理对话沙箱出站规则失败：%s (%s)", name, exc)


def sweep() -> None:
    """清掉本模块遗留的全部规则（启动时调用，容器已由 orphan sweep 清掉）。

    进程被 `systemctl restart` 杀掉时 `destroy()` 来不及跑，规则会留下。
    `DOCKER-USER` 的跳转按注释全删，再删所有 `SXEG-` 链 —— 只碰
    本模块自己的对象。
    """
    with _LOCK:
        _APPLIED.clear()
        try:
            result = _iptables("-S", "DOCKER-USER", check=False)
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if COMMENT_PREFIX not in line or not line.startswith("-A DOCKER-USER "):
                        continue
                    _iptables("-D", "DOCKER-USER", *shlex.split(line[len("-A DOCKER-USER ") :]), check=False)
            result = _iptables("-S", check=False)
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if line.startswith("-N " + CHAIN_PREFIX):
                        _delete_chain(line[3:])
        except EgressError as exc:
            _log.warning("清理遗留出站规则失败：%s", exc)


__all__ = ["EgressError", "apply", "clear", "sweep"]
