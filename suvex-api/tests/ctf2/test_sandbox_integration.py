"""**真起容器**验证持久区（v0.35，P1）—— 默认跳过，需显式开启。

## 为什么必须有这一条（这是它存在的全部理由）

v0.29 声称加了 `/work/persist` 持久区，代码、挂载参数、文档都齐了 ——
**但容器里根本写不进去**，因为它由服务进程（root）`mkdir` 出来、
属主 `root:root 0755`，而容器以 `USER solver`（uid 1000）运行。

这个 bug 藏了很久，根因只有一个：**没有任何一个测试真的起过容器**。
`test_workspace_persistence.py` 里那些用例是**纯函数**级的 ——
它们断言"`_argv()` 里拼了 `-v ...:/work/persist:rw`"，
但证明不了"docker 收到这个参数之后，容器里的非 root 用户**真的能写**"。
而挂载参数对不对，只有真起容器才知道。

## 为什么默认跳过

真起容器要 Docker、要一个已构建的镜像（`ctf2-sandbox` 2.8 GB）、
要 Linux（容器里的 uid/chown 语义 Windows 上验不了）。这些都不是
每次跑测试都该付出的代价 —— 尤其 CI 的 runner 里没有那个镜像。

所以它由环境变量 `SUPEX_CTF2_INTEGRATION=1` 显式开启，否则
**跳过并说清原因**（不是静默 skip —— 静默跳过等于没有这个测试）。

## 怎么在 Kali 上跑

```bash
cd /root/suvex/suvex-api
SUPEX_CTF2_INTEGRATION=1 .venv/bin/python -m pytest \
    tests/ctf2/test_sandbox_integration.py -v -p no:cacheprovider
```

（镜像名默认 `ctf2-sandbox:latest`，可用 `SUPEX_CTF2_TEST_IMAGE` 覆盖。）
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from app.ctf2 import runner

#: 显式开启这个开关才会真起容器。
_ENABLE_ENV = "SUPEX_CTF2_INTEGRATION"
#: 用哪个镜像。默认是 base profile。
_IMAGE_ENV = "SUPEX_CTF2_TEST_IMAGE"
_DEFAULT_IMAGE = "ctf2-sandbox:latest"


def _integration_enabled() -> bool:
    return os.environ.get(_ENABLE_ENV, "").strip() in {"1", "true", "yes"}


def _require_linux_and_docker() -> str:
    """不满足前提就 skip（带原因）。返回镜像名。"""
    if os.name == "nt":
        pytest.skip(
            "真起容器的持久区验证只在 Linux 上做 —— Windows 上容器里的 "
            "uid/chown 语义与生产不同，验了也不代表生产"
        )
    if not _integration_enabled():
        pytest.skip(
            f"默认跳过真起容器的集成测试（要 Docker + 已构建的镜像）。"
            f"要跑就设 {_ENABLE_ENV}=1"
        )
    # docker 在不在。
    probe = subprocess.run(
        ["docker", "version", "--format", "{{.Server.Version}}"],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        pytest.skip(f"Docker 不可用：{probe.stderr.strip()[:120]}")

    image = os.environ.get(_IMAGE_ENV, _DEFAULT_IMAGE)
    listed = subprocess.run(
        ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
        capture_output=True,
        text=True,
    )
    if image not in (listed.stdout or "").splitlines():
        pytest.skip(f"镜像 {image} 不存在（先构建：见 docker/ctf2-sandbox/Dockerfile）")
    return image


def test_persist_dir_is_writable_by_the_solver_and_survives(tmp_path: Path) -> None:
    """★★ 真起容器：以容器内的非 root 用户写 `/work/persist`，文件必须留在宿主。

    这一条覆盖的正是 `_workspace_dir` 的 `chown` 修复（v0.34）——
    少了它，"挂载参数拼对了"与"容器里真的能写"之间的差距没人验。
    """
    image = _require_linux_and_docker()

    # 用真实的 `_workspace_dir(create=True)`，这样 chown 也一起被验到。
    challenge_id = "ctf2chal_INTEGRATIONPERSIST"
    workspace = runner._workspace_dir(challenge_id, create=True)
    assert workspace is not None, "create=True 必须返回路径"

    container = runner.SandboxContainer(
        name=f"{runner.SANDBOX_NAME_PREFIX}integration-{os.urandom(3).hex()}",
        image=image,
        # 不联网 —— 这条测试只验文件系统，与网络无关。
        network_isolated=True,
        workspace_dir=workspace,
    )
    try:
        container.start()
        # 以容器内当前用户（Dockerfile 里的 `USER solver`，uid 1000）写。
        result = container.shell(
            f"id -u && echo ok > {runner.PERSIST_DIR}/from-container.txt && cat {runner.PERSIST_DIR}/from-container.txt"
        )
        assert result.exit_code == 0, (
            f"★ 容器内写 {runner.PERSIST_DIR} 失败 —— 持久区又变成只读了：\n{result.combined()}"
        )
        uid = result.stdout.strip().splitlines()[0]
        assert uid == str(runner.SOLVER_UID), (
            f"容器内用户不是 {runner.SOLVER_UID}（实际 {uid}）—— "
            "Dockerfile 的 useradd --uid 与 runner.SOLVER_UID 漂移了"
        )
    finally:
        container.destroy()

    # ★ 关键断言：容器**已销毁**，文件却还在宿主上 —— 这才叫"持久"。
    persisted = workspace / "from-container.txt"
    assert persisted.is_file(), (
        "★ 容器里写的文件没有留在宿主 —— 要么挂载没生效，要么写到了 /work（tmpfs）"
    )
    assert persisted.read_text(encoding="utf-8").strip() == "ok"


def test_attachments_are_visible_but_read_only(tmp_path: Path) -> None:
    """★ 附件只读挂载：容器里能**读**到，但**写不进去**。

    附件是题目原件，被容器里的不可信内容改掉会让"原件"失去意义。
    """
    image = _require_linux_and_docker()

    att = tmp_path / "attachments"
    att.mkdir()
    (att / "sample.txt").write_text("原件内容", encoding="utf-8")

    container = runner.SandboxContainer(
        name=f"{runner.SANDBOX_NAME_PREFIX}integration-{os.urandom(3).hex()}",
        image=image,
        network_isolated=True,
        attachments_dir=att,
    )
    try:
        container.start()
        read = container.shell(f"cat {runner.ATTACHMENT_DIR}/sample.txt")
        assert read.exit_code == 0 and "原件内容" in read.stdout, (
            f"容器里读不到附件：{read.combined()}"
        )
        # 写应当失败（:ro 挂载）。
        write = container.shell(f"echo tampered > {runner.ATTACHMENT_DIR}/sample.txt")
        assert write.exit_code != 0, "★ 附件目录居然可写 —— :ro 挂载没生效"
    finally:
        container.destroy()

    # 宿主上的原件没被动过。
    assert (att / "sample.txt").read_text(encoding="utf-8") == "原件内容"


def test_network_is_really_isolated(tmp_path: Path) -> None:
    """★ `--network none` 真的断网（不是"看起来配了"）。

    这是本模块最重要的一个安全参数（见 `_argv` 的参数表），
    而它同样只有真起容器才验得了。
    """
    image = _require_linux_and_docker()

    container = runner.SandboxContainer(
        name=f"{runner.SANDBOX_NAME_PREFIX}integration-{os.urandom(3).hex()}",
        image=image,
        network_isolated=True,
    )
    try:
        container.start()
        # 试着连一个公网地址；断网时必须失败。
        result = container.shell(
            "timeout 5 bash -c 'cat < /dev/null > /dev/tcp/1.1.1.1/443' && echo CONNECTED || echo BLOCKED"
        )
        assert "BLOCKED" in result.stdout, (
            f"★ 隔离容器竟然连上了外网 —— --network none 没生效：{result.combined()}"
        )
    finally:
        container.destroy()
