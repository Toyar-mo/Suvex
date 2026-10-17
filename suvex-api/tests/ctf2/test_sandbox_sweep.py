"""`runner.sweep_orphan_sandboxes` —— 重启服务留下的沙箱容器。

## 这条测试钉的是一个真实发生过、且没人会注意到的泄漏

实测（2026-10-01）：一轮部署里重启了几次服务，`docker ps` 就留下了
两个 `ctf2-*` 容器仍在跑，而数据库里那两道题已经 `solved`、环境记录
是 `stopped` —— **两边说法不一致，且没有任何界面能看出来**。

根因写不出补丁（Python 没有安全的杀线程手段，见 `ctf2/scheduler.py`），
所以修法是**在下次启动时清掉**：那一刻"带 `ctf2-` 前缀的容器全是遗留"
这个判据才成立。

## 打桩打在边界上

换掉 `runner._run`，于是命令构造（`docker ps -aq --filter name=…`）
仍然真的跑一遍，只有子进程是假的 —— 与 `test_runner.py` 里沙箱那几条
同一条理由。
"""

from __future__ import annotations

from app.ctf2 import runner


class _FakeRun:
    """记录被调用的命令，并按脚本返回结果。"""

    def __init__(self, *, ps_stdout: str = "", ps_ok: bool = True, rm_ok: bool = True):
        self.calls: list[list[str]] = []
        self.ps_stdout = ps_stdout
        self.ps_ok = ps_ok
        self.rm_ok = rm_ok

    def __call__(self, argv: list[str], *, timeout: float):
        self.calls.append(list(argv))
        if argv[:2] == ["docker", "ps"]:
            return runner.CommandResult(
                0 if self.ps_ok else 1, self.ps_stdout, "" if self.ps_ok else "docker 坏了"
            )
        return runner.CommandResult(
            0 if self.rm_ok else 1, "", "" if self.rm_ok else "删不掉"
        )


def test_sweep_removes_every_container_with_the_prefix(monkeypatch) -> None:
    """★ 列出带前缀的容器，逐个 `rm -f`。"""
    fake = _FakeRun(ps_stdout="abc123\ndef456\n")
    monkeypatch.setattr(runner, "_run", fake)

    assert runner.sweep_orphan_sandboxes() == 2

    assert fake.calls[0][:4] == ["docker", "ps", "-aq", "--filter"]
    # ⚠️ 过滤条件必须用**常量**而不是手写的字面量：容器名的前缀同时
    #    被"创建"和"清理"两处依赖，写死两遍迟早会漂移 ——
    #    而漂移的症状是"容器建出来了、却永远清不掉"。
    #
    # ⚠️ 常量之外**还必须带锚点 `^`**：Docker 的 `name` 过滤器是
    #    子串/正则匹配而非前缀匹配，不锚定的话一个叫 `my-ctf2-helper`
    #    的无关容器会被 `rm -f` 一起删掉。这两条要求是**并存**的 ——
    #    常量防"两处漂移"，锚点防"误删无关容器"。
    #    （同一条断言另见 `tests/test_ctf2_resource_guards.py`。）
    assert fake.calls[0][4] == f"name=^{runner.SANDBOX_NAME_PREFIX}"
    assert fake.calls[1] == ["docker", "rm", "-f", "abc123", "def456"]


def test_sweep_is_a_noop_when_nothing_is_left(monkeypatch) -> None:
    """没有遗留就**不调 `rm`**。

    空列表调 `docker rm -f` 会报错（"requires at least 1 argument"），
    那会让每次正常启动都打一行无意义的 warning。
    """
    fake = _FakeRun(ps_stdout="")
    monkeypatch.setattr(runner, "_run", fake)

    assert runner.sweep_orphan_sandboxes() == 0
    assert len(fake.calls) == 1, "只该有一次 ps，不该有 rm"
    assert fake.calls[0][:2] == ["docker", "ps"]


def test_sweep_never_raises_when_docker_is_unavailable(monkeypatch) -> None:
    """★★ Docker 不可用时**返回 0，不抛异常**。

    它在 `main.lifespan` 的启动路径上。抛出去会让"清理失败"升级成
    **服务起不来** —— 那个代价远大于"这次没清成"。
    """
    fake = _FakeRun(ps_ok=False)
    monkeypatch.setattr(runner, "_run", fake)

    assert runner.sweep_orphan_sandboxes() == 0


def test_sweep_reports_nothing_when_removal_fails(monkeypatch) -> None:
    """删不掉时返回 0（而不是报"清了 N 个"）。

    谎报会让日志说"清理了 2 个"而容器其实还在 —— 那比不清理更坏，
    因为它会让人以为问题解决了。
    """
    fake = _FakeRun(ps_stdout="abc123\n", rm_ok=False)
    monkeypatch.setattr(runner, "_run", fake)

    assert runner.sweep_orphan_sandboxes() == 0


def test_container_name_uses_the_prefix_constant() -> None:
    """★ 建容器与清容器**必须用同一个前缀**。

    这条守的是那对依赖关系：`sweep_orphan_sandboxes` 只认前缀，
    而 `_run_worker` 建容器时必须带上它。两者一旦漂移，泄漏的容器
    就**永远清不掉**了 —— 且没有任何症状，只是内存慢慢变少。

    ⚠️ 用源码断言而不是拉起整条链路：`_run_worker` 会起环境、调平台、
    跑 LLM，为了验一个字符串常量付那个代价不值得。而这条断言的
    意图很窄、也很明确 —— "建容器时用的是那个常量"。
    """
    import inspect

    source = inspect.getsource(runner._run_worker)
    assert "SANDBOX_NAME_PREFIX" in source, (
        "建容器的地方必须用 SANDBOX_NAME_PREFIX 拼名字 —— 否则清理函数认不出它"
    )
    # 反向确认：没有残留的手写 `ctf2-` 字面量（那就是漂移的开始）。
    assert 'f"ctf2-' not in source, (
        "建容器时别手写 'ctf2-' 前缀 —— 用常量，否则和清理函数会对不上"
    )


def test_the_test_client_never_sweeps_real_containers(client) -> None:  # noqa: ANN001
    """★★ `client` fixture 必须把 `sweep_orphan_sandboxes` 换成替身。

    这是 2026-10-17 那场真事故的守卫（见 `tests/conftest.py` 里 `client`
    的说明）。`main.py:194` 在 lifespan 里调这个函数，它做的是
    `docker rm -f` 掉**所有** `ctf2-` 前缀的容器 —— 在生产进程里成立，
    但在测试进程里，那台机器上可能正有别的进程在解题。

    事故形态：三道题在 40 秒内接连失败，每题起容器后 1–2 秒沙箱消失，
    `run_shell` 报 `No such container: ctf2-…`，而 `docker run` 返回码是 0。
    用户看到"轮数 2、耗时 8 秒、未解出"。

    这条用例守的是"替身还装着"：把 conftest 里那行 `monkeypatch.setattr`
    删掉、或把它挪到 `with TestClient(app)` 内部（lifespan 已经跑完，
    等于没 patch），这里就会红。
    """
    assert getattr(runner.sweep_orphan_sandboxes, "_is_test_stub", False) is True, (
        "client fixture 没有把 sweep_orphan_sandboxes 换成替身 —— "
        "跑一次 pytest 就会 docker rm -f 掉生产环境正在解题的沙箱"
    )
    # 替身不碰 Docker，也不假装扫到了东西。
    assert runner.sweep_orphan_sandboxes() == 0
