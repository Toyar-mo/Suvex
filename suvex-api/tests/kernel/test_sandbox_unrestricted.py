"""「无限制档」沙箱的参数与网络准备。

## 这个文件钉的是什么

对话里的通用 agent 需要一档**能真的做事**的容器：`apt-get install` 得能装、
`pip download` 得能下、`git clone` 得能拉。解题档那套
（`--network none --read-only --cap-drop ALL --tmpfs /work`）一条都给不了 ——
实测（Kali，生产参数起容器）逐条撞墙：

| 限制 | 撞成什么样 |
| --- | --- |
| `--network none` | `pip install` / `apt-get update` 全是 `000` |
| `--read-only` | `apt-get install` 报 `Read-only file system`；`pip` 无缓存目录 |
| `--cap-drop ALL` | `apt-get` 装包时 postinst 的 `chown`/`setcap` 静默失败 |
| `--tmpfs /work` 512m | `git clone` 一个中等仓库就 OOM（tmpfs 页算 cgroup 内存） |

所以加了一档 `unrestricted=True`。这个文件钉四件事：

1. **两档互不污染** —— 解题档的 argv **逐字节**不变（这是最要紧的一条：
   那一档有 20+ 处真实事故换来的参数，动一个字节都可能是在拆安全边界）；
2. **无限制档真的放开了**那四条；
3. **无限制档没有放开** `--privileged` / docker.sock / `--network host`
   —— 放开的是"能装能下"，不是"能摸到宿主"；
4. **网络准备跟着分叉走** —— 参数说走 `AGENT_NETWORK`，准备就必须建
   `AGENT_NETWORK`。这两处各写一份判断就会漂开，而漂开的症状是
   `docker run` 报一句 `network suvex-agent not found`。

## 为什么第 1 条要"逐字节"

不是洁癖。解题档的 `--tmpfs /work:rw,exec,size=512m,uid=1000,gid=1000`
里每个字段都有出处（`exec` 是因为 `pwn asm` 要执行临时文件；`uid/gid`
是因为镜像里 solver 是 1000，不设就写不进去）。所以 `_restricted_argv`
用整串字面量比对，而不是"包含几个关键参数" —— 后者漏掉的那一个
往往就是当初踩出来的那个。

## 打桩打在边界上

换掉 `runner._run`（与 `test_ctf2_network.py` 同一条理由）：命令**怎么拼**
仍然真的跑一遍，只有"真的执行 docker"被替换掉。
"""

from __future__ import annotations

import pytest

from app.kernel import runner

# `_verify_running()` 要的是 `--format` 拼出来的那一行：
# `{{.State.Running}}|{{.State.Status}}|{{.State.ExitCode}}|{{.State.OOMKilled}}|{{.State.Error}}`
#
# ⚠️ 不能只回 "running" —— 那样 `parts` 切出来第一段是 "running"，
#    而判据是 `running.lower() == "true"`，于是**每一次** start() 都
#    会抛"容器起来了但没在跑"。这个坑第一次写的时候踩了：
#    三条用例一起红，报的还是"容器秒退"，看起来像实现坏了。
_RUNNING = "true|running|0|false|"

# 解题档 `_restricted_argv()` 的**完整**字面量。
#
# ⚠️ 这不是"期望的一部分"，是全部。改这里之前先回答：你改的那个参数
#    当初是为哪次事故加的？答不上来就别改。
_RESTRICTED_EXACT = [
    "--network",
    "none",
    "--read-only",
    "--tmpfs",
    "/work:rw,exec,size=512m,uid=1000,gid=1000",
    "--tmpfs",
    "/tmp:rw,exec,size=256m,uid=1000,gid=1000",
    "--cap-drop",
    "ALL",
    "--security-opt",
    "no-new-privileges",
    "--pids-limit",
    "512",
]


def _sandbox(**kwargs) -> runner.SandboxContainer:  # noqa: ANN003
    return runner.SandboxContainer(
        name="ctf2-test-abc", image="suvex/suvex-sandbox:test", **kwargs
    )


def _from_name_to_image(argv: list[str], image: str) -> list[str]:
    """从完整 argv 里切出 `--name <名字>` 之后、镜像之前那一段。

    切法是**锚定两端**而不是用下标：有人往前面加参数时下标会静默切错
    窗口 —— 而切错窗口的断言看起来照样"通过"，只是它断言的根本不是
    想测的那段。

    终点取"镜像名"而不是"最后一个参数"：镜像之后是容器入口
    （`tail -f /dev/null`），那不属于"沙箱参数"，混进来只会让期望值里
    多出两个与本题无关的元素。
    """
    start = argv.index("--name") + 2
    end = argv.index(image, start)
    return argv[start:end]


# ══════════════════════════════════════════════════════════
# 1. 解题档：逐字节不变
# ══════════════════════════════════════════════════════════


def test_the_restricted_argv_is_byte_for_byte_what_it_always_was() -> None:
    """★ 解题档一个字节都不许变。

    这是整个改动最容易被破坏的地方：两档分叉后有人"顺手统一一下"，
    就会把 `--cap-drop ALL` 或 `--read-only` 从解题档拿掉，
    而那时**没有任何测试会红** —— 因为无限制档那条用例照样通过。
    """
    assert _from_name_to_image(_sandbox()._argv(), "suvex/suvex-sandbox:test") == _RESTRICTED_EXACT


def test_the_restricted_argv_still_has_no_network() -> None:
    """反向锚：解题档的 `--network none` 不许被无限制档的默认值带跑。

    `_network_arg()` 里无限制档排在**第 0 条**（先于 `network_isolated`），
    所以这里必须显式确认"没传 unrestricted 时仍然是 none"。
    """
    argv = _sandbox()._argv()
    assert argv[argv.index("--network") + 1] == "none"
    assert "suvex-agent" not in argv
    assert runner.AGENT_NETWORK not in argv


def test_network_isolated_still_wins_inside_the_restricted_tier() -> None:
    """解题档内部的三态语义没被动过：隔离 > 指定网桥 > 靶站网络。"""
    assert _sandbox(network_isolated=True)._network_arg() == "none"
    assert (
        _sandbox(network_isolated=False, network_name="suvex-pentest")._network_arg()
        == "suvex-pentest"
    )
    assert (
        _sandbox(network_isolated=False)._network_arg() == runner.CTF2_NETWORK
    )


# ══════════════════════════════════════════════════════════
# 2. 无限制档：真的放开了那四条
# ══════════════════════════════════════════════════════════


def test_unrestricted_has_a_network_because_installing_packages_needs_one() -> None:
    """★ `--network none` 是 `pip install` / `apt-get update` 全 `000` 的原因。"""
    argv = _sandbox(unrestricted=True)._argv()
    assert argv[argv.index("--network") + 1] == runner.AGENT_NETWORK
    assert "--network" in argv
    assert "none" not in argv


def test_unrestricted_is_writable_because_apt_needs_a_writable_root_fs() -> None:
    """★ `--read-only` 会让 `apt-get install` 报 `Read-only file system`。"""
    argv = _sandbox(unrestricted=True)._argv()
    assert "--read-only" not in argv
    assert "--tmpfs" not in argv


def test_unrestricted_keeps_its_capabilities_because_postinst_silently_fails() -> None:
    """★ `--cap-drop ALL` 让装包时的 `chown` / `setcap` **静默**失败。

    静默是关键：`apt-get install` 仍然返回 0，装出来的东西却不完整。
    那比直接报错难查得多。
    """
    argv = _sandbox(unrestricted=True)._argv()
    assert "--cap-drop" not in argv


def test_unrestricted_runs_as_root_because_postinst_scripts_need_it() -> None:
    argv = _sandbox(unrestricted=True)._argv()
    assert argv[argv.index("--user") + 1] == "0:0"


def test_unrestricted_raises_the_pid_ceiling_for_real_builds() -> None:
    """`npm install` / `make -j` 用 512 会 fork 失败。

    ⚠️ 这一条**保留**了限制（不是取消），因为它是宿主存活限制而不是
       能力限制：`make -j` 该过，`:(){ :|:& };:` 该被挡。
    """
    argv = _sandbox(unrestricted=True)._argv()
    assert argv[argv.index("--pids-limit") + 1] == "4096"


# ══════════════════════════════════════════════════════════
# 3. 无限制档**没有**放开的东西
# ══════════════════════════════════════════════════════════
#
# 这一组比上一组重要。「无限制」这个词很容易被读成"什么都能干"，
# 而实际上它只放开四件与"装包/下载/构建"直接相关的事。
# 下面每一条一旦被放开，容器就等于宿主 root：
#
#   --privileged        → 能 `cat /dev/mem`、能重新挂载、能改内核参数
#   /var/run/docker.sock → 直接 `docker run -v /:/host` 拿到整个宿主
#   --network host      → 不再有网段隔离，且能直连 127.0.0.1:8000 打平台自己
#
# 所以它们是**反向断言**：不是"应该有"，是"绝不许有"。


@pytest.mark.parametrize(
    ("flag", "why"),
    [
        ("--privileged", "能读 /dev/mem、能重挂载、能改内核参数"),
        ("--network", None),  # 单独在下面测（它必须有，但值不许是 host）
        ("--pid", None),
        ("--ipc", None),
        ("--userns", None),
    ],
)
def test_unrestricted_never_gets_the_escape_hatches(flag: str, why: str | None) -> None:
    argv = _sandbox(unrestricted=True)._argv()
    if flag == "--network":
        assert argv[argv.index("--network") + 1] != "host"
        return
    assert flag not in argv, f"{flag} 会让容器摸到宿主：{why}"


def test_unrestricted_never_mounts_the_docker_socket() -> None:
    """/var/run/docker.sock = 宿主 root（`docker run -v /:/host` 一条命令）。"""
    argv = _sandbox(unrestricted=True)._argv()
    joined = " ".join(argv)
    assert "docker.sock" not in joined


def test_unrestricted_still_cannot_reach_the_platform_itself_via_the_bridge() -> None:
    """无限制档走的是**专用**网桥，不是默认 bridge。

    这条钉的是"放开出网"与"放开看见平台自己"是两件事：`AGENT_NETWORK`
    与平台库 / CTF 靶站 / 扫描网桥都隔离。真正封"容器 → 宿主 LAN IP"
    的是 `deploy/sandbox-egress-guard.sh`（INPUT 链），不是网桥本身 ——
    所以这里只能断言网桥选对了，不能断言端到端封死。
    """
    argv = _sandbox(unrestricted=True)._argv()
    assert argv[argv.index("--network") + 1] == runner.AGENT_NETWORK
    assert runner.AGENT_NETWORK != "bridge"
    assert runner.AGENT_NETWORK != runner.CTF2_NETWORK


# ══════════════════════════════════════════════════════════
# 4. 网络准备跟着分叉走
# ══════════════════════════════════════════════════════════


def test_unrestricted_start_prepares_the_agent_bridge(monkeypatch) -> None:
    """★ `start()` 必须建的是 `AGENT_NETWORK`，不是 `CTF2_NETWORK`。

    ## 这条钉的是一个真实会发生的缺陷

    对话 agent 创建容器时用构造函数默认值 `network_isolated=True`
    （它不关心解题档那三态语义），而原来 `start()` 的准备条件是
    `if not self.network_isolated:` —— 于是**网络一次都不会被准备**，
    同时 `_network_arg()` 已经把参数改成 `AGENT_NETWORK`。
    结果：`docker run` 报 `network suvex-agent not found`。

    断言方式是看**命令序列**而不是返回值（返回值在错配时同样是 True，
    见 `test_ctf2_network.py` 开头那段"为什么测配置不测存在"）。
    """
    seen: list[list[str]] = []

    def fake_run(argv, *, timeout):  # noqa: ANN001, ANN202
        seen.append(list(argv))
        if argv[:3] == ["docker", "network", "inspect"]:
            return runner.CommandResult(0, "[]", "")
        if argv[:3] == ["docker", "network", "create"]:
            return runner.CommandResult(0, "abc", "")
        if argv[:2] == ["docker", "version"]:
            return runner.CommandResult(0, "28.0.0", "")
        if argv[:2] == ["docker", "inspect"]:
            return runner.CommandResult(0, _RUNNING, "")
        # docker run
        return runner.CommandResult(0, "a" * 64, "")

    monkeypatch.setattr(runner, "_run", fake_run)
    _sandbox(unrestricted=True).start()

    inspected = [a for a in seen if a[:3] == ["docker", "network", "inspect"]]
    assert any(runner.AGENT_NETWORK in a for a in inspected), (
        "start() 没有为无限制档查 AGENT_NETWORK 是否存在"
    )
    # 反向：不许去查/建 CTF 靶站网络（那是解题档的事）。
    assert not any(runner.CTF2_NETWORK in a for a in inspected), (
        "无限制档不该碰 CTF 靶站网络"
    )


def test_unrestricted_start_fails_loudly_when_the_bridge_is_missing(
    monkeypatch,
) -> None:
    """建网桥失败 → `start()` 抛错并说清缺什么。

    不拦的话，`docker run` 会自己报一句 `network suvex-agent not found`，
    而那句话里没有任何线索指向"需要 root 建 docker 网络"。
    """

    def fake_run(argv, *, timeout):  # noqa: ANN001, ANN202
        if argv[:2] == ["docker", "version"]:
            return runner.CommandResult(0, "28.0.0", "")
        if argv[:3] == ["docker", "network", "inspect"]:
            return runner.CommandResult(1, "", "Error: network not found")
        if argv[:3] == ["docker", "network", "create"]:
            return runner.CommandResult(1, "", "permission denied")
        return runner.CommandResult(0, "a" * 64, "")

    monkeypatch.setattr(runner, "_run", fake_run)
    with pytest.raises(runner.KernelSandboxError) as exc:
        _sandbox(unrestricted=True).start()

    message = str(exc.value)
    assert runner.AGENT_NETWORK in message
    assert "install.sh" in message


def test_the_restricted_start_still_prepares_the_ctf2_bridge(monkeypatch) -> None:
    """反向锚：解题档的网络准备没被这一支抢走。

    上一条测试写了"无限制档不许碰 CTF2 网络"，这一条防的是改过头
    —— 把那个 `elif` 写成 `if` 的并列分支，就会两档都去建 CTF2。
    """
    seen: list[list[str]] = []

    def fake_run(argv, *, timeout):  # noqa: ANN001, ANN202
        seen.append(list(argv))
        if argv[:2] == ["docker", "version"]:
            return runner.CommandResult(0, "28.0.0", "")
        if argv[:3] == ["docker", "network", "inspect"]:
            return runner.CommandResult(0, "[]", "")
        if argv[:2] == ["docker", "inspect"]:
            return runner.CommandResult(0, _RUNNING, "")
        return runner.CommandResult(0, "a" * 64, "")

    monkeypatch.setattr(runner, "_run", fake_run)
    _sandbox(network_isolated=False).start()

    inspected = [a for a in seen if a[:3] == ["docker", "network", "inspect"]]
    assert any(runner.CTF2_NETWORK in a for a in inspected)
    assert not any(runner.AGENT_NETWORK in a for a in inspected)


def test_network_isolated_never_prepares_any_network(monkeypatch) -> None:
    """`--network none` 与任何网桥无关，不该为它付查询。

    这条防的是"为了省事把 prepare 提到最前面无条件执行" —— 那会让
    本地 repo/path 目标的隔离题也去查一遍 docker 网络，
    而隔离题本来不该依赖 docker 网络状态。
    """
    seen: list[list[str]] = []

    def fake_run(argv, *, timeout):  # noqa: ANN001, ANN202
        seen.append(list(argv))
        if argv[:2] == ["docker", "version"]:
            return runner.CommandResult(0, "28.0.0", "")
        if argv[:2] == ["docker", "inspect"]:
            return runner.CommandResult(0, _RUNNING, "")
        return runner.CommandResult(0, "a" * 64, "")

    monkeypatch.setattr(runner, "_run", fake_run)
    _sandbox(network_isolated=True).start()

    assert not [a for a in seen if a[:3] == ["docker", "network", "inspect"]]


# ══════════════════════════════════════════════════════════
# 5. 出站封堵脚本必须覆盖每一个沙箱网桥
# ══════════════════════════════════════════════════════════


def test_egress_guard_script_covers_every_sandbox_network() -> None:
    """★ 封堵脚本的网段清单必须跟着 `runner.py` 的网络常量走。

    ## 这条钉的是实测发生过的漏洞（2026-10-27）

    脚本原先把子网**写死**成 `172.18.0.0/16 172.19.0.0/16`（ctf2 与
    pentest）。通用 agent 档用的是后来新增的 `suvex-agent`，docker 给它
    分了 `172.20.0.0/16` —— 不在清单里。实测后果：

        docker exec ctf2-chat-… sh -c \
            "curl -s -o /dev/null -w %{http_code} http://192.168.44.128:8080/"
        →  200

    即**一个特意放开的通用 agent 容器能直接打开平台自己的前端**。

    ## 为什么肉眼检查会漏掉它

    规则确实下发成功了，`iptables -S INPUT` 里看得见 15 条 DROP ——
    缺的那一条不会报错、不会缺席地显眼。只有拿「有哪些网桥」去对
    「清单里写了哪些」，才能发现少一个。

    ## 为什么断言"赋值行"而不是"文件里出现过这个名字"

    第一版就是断言 `net in text`，结果**变异验证没抓住**：把 `suvex-agent`
    从赋值里删掉，那条断言照样绿 —— 因为名字在我自己写的注释里也出现过
    （"通用 agent 档用的是新网络 `suvex-agent`"）。文件里出现过 = 什么都
    证明不了。改成只解析 `SANDBOX_NETWORKS=` 那一行，注释不参与。

    ## 为什么断言"名字在赋值里"而不是跑脚本

    跑脚本要 root + 会改真实防火墙，测试环境不该碰。这里断言的是一件
    更弱但仍有价值的事：**新增沙箱网络时，会有人被迫回来改这个文件**
    （常量加进 `runner.py` 而脚本没跟上 → 这条红）。
    """
    from pathlib import Path

    script = (
        Path(__file__).resolve().parents[2] / "deploy" / "sandbox-egress-guard.sh"
    )
    text = script.read_text(encoding="utf-8")

    # 只取赋值语句的内容，注释里的名字不算数（见上面那段）。
    assignments = [
        line for line in text.splitlines() if line.startswith("SANDBOX_NETWORKS=")
    ]
    assert len(assignments) == 1, f"SANDBOX_NETWORKS 应恰好赋值一次：{assignments}"
    listed = assignments[0].split("=", 1)[1].strip().strip('"').split()

    for net in (runner.CTF2_NETWORK, runner.PENTEST_NETWORK, runner.AGENT_NETWORK):
        assert net in listed, (
            f"出站封堵脚本没覆盖网桥 {net} —— 那个网桥里的容器能直接连平台端口。"
            f"把 {net} 加进 SANDBOX_NETWORKS。当前清单：{listed}"
        )

    # 反向：不许再写死具体网段。写死就必然在下次新增网桥时静默漏掉
    # （这正是本次漏洞的成因）。
    assert not any(item.startswith("172.") for item in listed), (
        f"网段不许写死：新增网桥时会静默漏掉。用 resolve_subnets() 从 docker 读。"
        f"当前清单：{listed}"
    )


def test_egress_guard_resolves_subnets_from_docker() -> None:
    """网段必须真的从 docker 读，不是换个地方继续写死。

    `resolve_subnets()` 存在还不够 —— 它可能被定义了却没人调用（本项目
    反复出现的"写了却不生效"）。所以同时钉住调用点。
    """
    from pathlib import Path

    text = (
        Path(__file__).resolve().parents[2] / "deploy" / "sandbox-egress-guard.sh"
    ).read_text(encoding="utf-8")
    assert "$(resolve_subnets)" in text, (
        "定义了 resolve_subnets() 却没有调用点 —— 网段会一直是空的"
    )
    assert "docker network inspect" in text, "网段应从 docker network inspect 读"
