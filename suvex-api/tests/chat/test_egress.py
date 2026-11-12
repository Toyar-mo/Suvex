"""网络层出站守卫：只验命令构造与幂等，不要求在 CI 里有 root/iptables。"""

from __future__ import annotations

import subprocess

from app.chat import egress


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def test_apply_builds_default_drop_chain(monkeypatch) -> None:  # noqa: ANN001
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(egress, "_container_ip", lambda name: "172.20.0.5")

    def fake_iptables(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return _ok()

    monkeypatch.setattr(egress, "_iptables", fake_iptables)
    monkeypatch.setattr(egress, "_APPLIED", {})

    egress.apply("ctf2-chat-test", ["10.0.0.0/8"], ["192.168.1.0/24"])

    chain = next(a[1] for a in calls if a[0] == "-N")
    assert chain.startswith("SXEG-")
    # 放行已建立连接、两个网段，最后默认丢弃。
    assert ("-A", chain, "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT") in calls
    assert ("-A", chain, "-d", "10.0.0.0/8", "-j", "ACCEPT") in calls
    assert ("-A", chain, "-d", "192.168.1.0/24", "-j", "ACCEPT") in calls
    assert ("-A", chain, "-j", "DROP") in calls
    # 跳转按源 IP，带本容器的注释。
    jump = next(a for a in calls if a[0] == "-I")
    assert jump[1:5] == ("DOCKER-USER", "1", "-s", "172.20.0.5")
    assert any("sxeg:" in item for item in jump)


def test_apply_is_idempotent_when_allowlist_unchanged(monkeypatch) -> None:  # noqa: ANN001
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(egress, "_container_ip", lambda name: "172.20.0.5")

    def fake_iptables(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return _ok()

    monkeypatch.setattr(egress, "_iptables", fake_iptables)
    monkeypatch.setattr(egress, "_APPLIED", {})

    egress.apply("ctf2-chat-test", ["10.0.0.0/8"], [])
    chain = next(a[1] for a in calls if a[0] == "-N")
    calls.clear()
    egress.apply("ctf2-chat-test", ["10.0.0.0/8"], [])

    # 只做一次 -C 确认跳转还在，不再重建链。
    assert calls == [("-C", "DOCKER-USER", "-s", "172.20.0.5", "-j", chain)]


def test_sweep_removes_our_jumps_and_chains(monkeypatch) -> None:  # noqa: ANN001
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(egress, "_APPLIED", {"old": ("172.20.0.2", frozenset(), frozenset(), "SXEG-x")})

    def fake_iptables(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args == ("-S", "DOCKER-USER"):
            return _ok(
                "-A DOCKER-USER -s 172.20.0.2 -j SXEG-abc-1 -m comment --comment sxeg:SXEG-abc\n"
                "-A DOCKER-USER -s 10.0.0.0/8 -j ACCEPT\n"
            )
        if args == ("-S",):
            return _ok("-N SXEG-abc-1\n-N OTHER-chain\n")
        return _ok()

    monkeypatch.setattr(egress, "_iptables", fake_iptables)
    egress.sweep()

    assert ("-D", "DOCKER-USER", "-s", "172.20.0.2", "-j", "SXEG-abc-1", "-m", "comment", "--comment", "sxeg:SXEG-abc") in calls
    assert ("-F", "SXEG-abc-1") in calls
    assert ("-X", "SXEG-abc-1") in calls
    assert all("OTHER-chain" not in a for a in calls)
    assert egress._APPLIED == {}
