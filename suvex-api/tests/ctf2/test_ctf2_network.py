"""`runner.ensure_ctf2_network` —— 沙箱网络的存在**与配置**（发现 3）。

## 这条测试钉的是一个真实的越界

2026-10-16 在真实的 `suvex-ctf2` 上实测：起两个容器当 A、B 两道题，
A **直接读到了 B 的响应体**（`GOT:SECRET-FROM-B`）。原因是用户自定义
bridge 的 `com.docker.network.bridge.enable_icc` **默认为 true** ——
「换个自定义网络」本身不隔离任何东西，还额外多了按名字的 DNS。

修法是在网络创建时传 `-o …enable_icc=false`（实测那一档会在
`iptables` 里落成一条 `-i br-X -o br-X -j DROP`）。

## 为什么测试的重点是"**配置**"而不是"**存在**"

这个函数原来是先 `docker network ls --filter name=^…$` 查存在性、
不在就 `docker network create`。那条实现在"网络早就建好了、
但 `enable_icc` 还是默认的 true"这个**最常见**的情况下会返回 True ——
看起来一切正常，而题目之间是通的。

所以下面每组用例都同时断言**命令序列**（有没有 rm/create、
create 有没有带那个 `-o`），而不只是返回值。返回值在这里
几乎不携带信息量：三种截然不同的状态下它都可能是 True。

## 打桩打在边界上

换掉 `runner._run`，于是命令**怎么拼**仍然真的跑一遍
（与 `test_sandbox_sweep.py` 同一条理由）。
"""

from __future__ import annotations

import logging

import pytest

from app.ctf2 import runner

ICC = runner.NETWORK_ICC_OPTION


class _FakeDocker:
    """一个最小的 Docker 网络状态机。

    `icc` 的取值就是真实 `docker network inspect` 会渲染出来的东西：
      · `None`  → 网络**不存在**（inspect 退出码 1）
      · `""`    → 网络在，但**没有**这个选项（模板渲染空串）＝ Docker 默认 true
      · `"false"` → 网络在，且已关掉互通
    """

    def __init__(
        self,
        *,
        icc: str | None = None,
        create_ok: bool = True,
        rm_ok: bool = True,
        race_to: str | None = None,
    ):
        self.icc = icc
        self.create_ok = create_ok
        self.rm_ok = rm_ok
        # ★ 模拟竞态：我们这次 `create` 失败，但**另一个进程同时建好了**
        #   网络 —— 它建出来的配置是什么，由 `race_to` 给出。
        #   `"false"` = 对方也带了对的选项；`""` = 对方没带（默认互通）。
        self.race_to = race_to
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], *, timeout: float):
        self.calls.append(list(argv))
        if argv[:3] == ["docker", "network", "inspect"]:
            if self.icc is None:
                return runner.CommandResult(
                    1, "", f"Error: No such network: {runner.CTF2_NETWORK}"
                )
            return runner.CommandResult(0, self.icc + "\n", "")
        if argv[:3] == ["docker", "network", "rm"]:
            if not self.rm_ok:
                return runner.CommandResult(
                    1, "", "error while removing network: network has active endpoints"
                )
            self.icc = None
            return runner.CommandResult(0, runner.CTF2_NETWORK + "\n", "")
        if argv[:3] == ["docker", "network", "create"]:
            if self.race_to is not None:
                # 竞态：我们的 create 输了，网络被别人建成了 `race_to` 的样子。
                self.icc = self.race_to
                return runner.CommandResult(
                    1, "", f"Error response from daemon: network with name "
                    f"{runner.CTF2_NETWORK} already exists"
                )
            if not self.create_ok:
                return runner.CommandResult(
                    1, "", "Error response from daemon: all predefined address pools "
                    "have been fully subnetted"
                )
            self.icc = "false"
            return runner.CommandResult(0, "sha256:deadbeef\n", "")
        raise AssertionError(f"没有预料到的命令：{argv}")

    # ── 断言辅助 ─────────────────────────────────────────
    def creates(self) -> list[list[str]]:
        return [c for c in self.calls if c[:3] == ["docker", "network", "create"]]

    def removes(self) -> list[list[str]]:
        return [c for c in self.calls if c[:3] == ["docker", "network", "rm"]]


@pytest.fixture
def docker(monkeypatch):
    def _install(**kwargs) -> _FakeDocker:
        fake = _FakeDocker(**kwargs)
        monkeypatch.setattr(runner, "_run", fake)
        return fake

    return _install


# ══════════════════════════════════════════════════════════
# 一、建的时候必须带上 ICC=false
# ══════════════════════════════════════════════════════════


def test_creates_the_network_with_inter_container_communication_off(docker) -> None:
    """★ 网络不存在时，`create` **必须**带 `-o …enable_icc=false`。

    ## 这条断言防的是什么

    防的是"改回一句朴素的 `docker network create suvex-ctf2`"。
    那种改法**不会有任何症状**：网络建得出来、沙箱起得来、
    所有既有测试照过 —— 只有同时跑两道题时会越界，而那不是
    任何一条自动化检查会去看的地方。

    ⚠️ 断言的是**选项常量**而不是字面量：创建处的字符串与
    `NETWORK_ICC_OPTION` 的文档、以及将来可能的第二处引用
    必须指同一个选项名。
    """
    fake = docker(icc=None)

    assert runner.ensure_ctf2_network() is True

    assert fake.creates() == [
        ["docker", "network", "create", "-o", f"{ICC}=false", runner.CTF2_NETWORK]
    ]
    assert fake.removes() == [], "网络本来就不存在，不该有 rm"


def test_the_option_name_is_the_docker_one() -> None:
    """选项名本身是 Docker 的接口，写错就等于没关。

    ⚠️ 写错的失败方式很安静：`docker network create -o` 对**未知选项**
    是接受的（它只是被记进 `.Options` 而已），所以一个拼错的选项名
    会让网络"建成功"、`inspect` 也看得见那个键 —— 而 `enable_icc`
    仍然是默认的 true。
    """
    assert ICC == "com.docker.network.bridge.enable_icc"


# ══════════════════════════════════════════════════════════
# 二、已经建好但没关互通的网络：就地迁移
# ══════════════════════════════════════════════════════════


def test_migrates_an_existing_network_that_still_allows_icc(docker) -> None:
    """★★ 网络在、但 `enable_icc` 是默认的 true → **删了重建**。

    这是发现 3 在现网上的**实际状态**：`suvex-ctf2` 早就被建出来了
    （2026-10-01 那次修的），只是从来没人给它带过选项。
    只查存在性的老实现会在这条路径上直接返回 True —— 于是修法
    对**唯一真正需要修的那个网络**完全不生效。

    ⚠️ 顺序：先 rm 再 create。反过来会 `already exists`。
    """
    fake = docker(icc="")  # 网络在，选项缺席 ＝ 默认 true

    assert runner.ensure_ctf2_network() is True

    assert fake.removes() == [["docker", "network", "rm", runner.CTF2_NETWORK]]
    assert fake.creates() == [
        ["docker", "network", "create", "-o", f"{ICC}=false", runner.CTF2_NETWORK]
    ]
    assert fake.icc == "false", "迁移之后该网络的配置必须真的变了"


def test_an_already_correct_network_is_left_alone(docker) -> None:
    """已经是 ICC=false → 什么都不做（一次 inspect 就结束）。

    这是**常态**：服务每次重启都会调一遍这个函数。要是它对一个
    配置正确的网络也 rm+create，那每次重启都会把网络删掉重建，
    而"重启时正好有别的东西在那个网络上"时会失败 —— 白引入一类故障。
    """
    fake = docker(icc="false")

    assert runner.ensure_ctf2_network() is True

    assert fake.calls == [
        [
            "docker",
            "network",
            "inspect",
            runner.CTF2_NETWORK,
            "--format",
            f'{{{{index .Options "{ICC}"}}}}',
        ]
    ], "配置已经正确时只该有一次 inspect"


def test_never_uses_network_ls_to_judge_readiness(docker) -> None:
    """★ 判据**不能**退回"网络在不在"。

    这条是给未来的人看的：`docker network ls` 查得到名字，
    只证明网络**存在**，不证明它**隔不隔离**。老实现就是栽在这里
    （它查存在性，于是现网那个默认 ICC 的网络被判为"已就绪"）。
    """
    fake = docker(icc="")

    runner.ensure_ctf2_network()

    assert not [c for c in fake.calls if c[:3] == ["docker", "network", "ls"]], (
        "用 `docker network ls` 判断就绪状态时，'网络在但 ICC 是默认 true' "
        "会被判为就绪 —— 那正是发现 3 的成因。判据必须是 inspect 出来的配置。"
    )


# ══════════════════════════════════════════════════════════
# 三、迁移失败时不许硬删
# ══════════════════════════════════════════════════════════


def test_does_not_migrate_when_the_network_still_has_containers(docker, caplog) -> None:
    """★★ `rm` 失败（网络上有容器）→ **不重建、返回 True、记 warning**。

    ## 为什么不"想办法删掉它"

    Docker 的 `enable_icc` 没有"改现有网络"的 API，所以迁移只能是
    删了重建。而删一个**还有容器**的网络要么失败、要么（`--force` 之类）
    把别人的容器网络拔掉。

    ⚠️ 这里刻意返回 **True** 而不是 False：网络确实可用、沙箱起得来。
    这轮的问题是"隔离没生效"，那是另一回事；返回 False 会让调用方
    以为"网络不可用"，而那是假的。

    ⚠️ 但**必须留下 warning**。否则这就是"静默地不隔离" ——
    本轮题与题之间是通的，而日志里一个字的痕迹都没有。
    """
    fake = docker(icc="", rm_ok=False)

    with caplog.at_level(logging.WARNING, logger="suvex.ctf2"):
        assert runner.ensure_ctf2_network() is True

    assert fake.creates() == [], (
        "删不掉就不能接着 create —— create 只会 `already exists` 失败，"
        "而且会掩盖真正的状态"
    )
    assert "互通" in caplog.text, (
        "迁移没做成时必须留下 warning —— 否则'本轮题目之间是通的'"
        "这件事在日志里完全不可见"
    )


# ══════════════════════════════════════════════════════════
# 四、失败不阻断启动，且竞态复查要看**配置**
# ══════════════════════════════════════════════════════════


def test_create_failure_returns_false_when_the_network_is_really_absent(docker) -> None:
    """★★ `create` 失败且网络确实不在 → 返回 False，**不抛异常**。

    这个函数在 `main.lifespan` 的启动路径上，与 `sweep_orphan_sandboxes`
    同一类动作。抛出去会让"网络没建出来"升级成**服务起不来** ——
    而那个代价远大于"解题沙箱起不来"（后者是这道题失败，
    前者是整个平台失败）。
    """
    fake = docker(icc=None, create_ok=False)

    assert runner.ensure_ctf2_network() is False
    assert fake.icc is None


def test_a_lost_race_with_the_right_config_is_not_a_failure(docker) -> None:
    """★ 竞态：`create` 报 already exists，但网络**已被建成对的形状** → True。

    查与建之间不原子。没有这次复查的话，这里会为一件**已经成立**
    的事记一条"解题沙箱将起不来"的 warning —— 一条假的警报比没有警报更坏。
    """
    fake = docker(icc=None, race_to="false")

    assert runner.ensure_ctf2_network() is True
    assert fake.creates(), "确实试过 create（失败了），复查才把它救回来"


def test_a_lost_race_with_the_wrong_config_is_still_a_failure(docker) -> None:
    """★★ 复查必须是**看配置**，不是"看名字在不在"。

    竞态的另一半：`create` 失败，网络确实存在 —— 但它仍然允许互通。
    这时返回 True 等于把发现 3 原样复现（老实现在这里放行的正是
    "名字在"这个判据）。
    """
    fake = docker(icc=None, race_to="")

    assert runner.ensure_ctf2_network() is False, (
        "复查时只看'名字在不在'，会让一个 ICC 仍是默认值的网络被判为就绪 —— "
        "那正是发现 3。复查要看 inspect 出来的配置。"
    )
    assert fake.calls, "至少该查过一次"


# ══════════════════════════════════════════════════════════
# 五、调用时机：必须在起容器**之前**
# ══════════════════════════════════════════════════════════


def test_sandbox_start_ensures_the_network_before_running_the_container() -> None:
    """★★ `SandboxContainer.start()` 要在 `docker run` **之前**调它。

    ## 为什么顺序在这里是硬要求

    迁移会 `docker network rm` 那个网络，而它**要求网络上没有容器**。
    在 `start()` 里，这一刻本容器还没起 —— 那正是删得动的时机。
    一旦挪到 `_run()` 之后，本容器自己就挂在网络上了：
    迁移必然失败，于是**第一个起容器的进程会把网络永久锁在"互通"状态**，
    而且每次都是它。
    """
    import inspect

    source = inspect.getsource(runner.SandboxContainer.start)

    assert "ensure_ctf2_network()" in source, (
        "起容器前必须保证网络 —— 光靠服务启动时建一次挡不住"
        "「服务跑着的时候网络被删/被 prune」"
    )
    assert source.index("ensure_ctf2_network()") < source.index("self._argv()"), (
        "`ensure_ctf2_network()` 必须在 `self._argv()`（也就是 docker run）之前 —— "
        "迁移要删网络，而删网络要求它上面没有容器。"
    )


def test_lifespan_ensures_the_network_after_sweeping_orphans() -> None:
    """★★ `main.lifespan` 里"先清容器、再保证网络"，**顺序不能对调**。

    清容器那一步（`sweep_orphan_sandboxes`）的意义在发现 3 之后多了一层：
    它让下面的网络迁移**删得动**（删网络要求没有活动端点）。
    对调的话，上一次运行残留的 `ctf2-*` 容器会让迁移失败，
    于是重启一次不够、每次重启都不够 —— 网络永远停在互通状态。
    """
    import inspect

    from app import main

    source = inspect.getsource(main.lifespan)
    sweep = source.index("sweep_orphan_sandboxes()")
    ensure = source.index("ensure_ctf2_network()")

    assert sweep < ensure, (
        "顺序必须先是 sweep_orphan_sandboxes()、再是 ensure_ctf2_network()："
        "残留容器会让网络迁移（rm + create）失败，于是 ICC=false 永远装不上。"
    )
