"""题目附件必须真的进沙箱（2026-10-27 真跑踩出来的回归）。

## 这次要防的是什么

一道 97 MB 的 Android/UE 逆向题（附件是 zip，`uploads.claim()` 解出来的
`千千的咲.apk` 105,774,224 字节）。宿主磁盘上文件在，
但**容器里 `/work/attachments` 不存在**：扫描路径构造 `SandboxContainer`
时没传 `attachments_dir`，`SolveSpec.attachments` 还被写死成 `()`。

模型的反应（`scan.log` 原文）：
    [reasoning] The environment is empty. No attachments, no challenge dir access.
    [reasoning] The platform itself confirms **"可用附件：（无附件）"**
然后拿 348 轮去 `grep -rI /` 盲搜整个文件系统、还去 `ctf2.dasctf.com`
找题目文件 —— 它不是不会解，是**没题可解**。

## 为什么分三条测

1. 挂载：容器 argv 里必须出现 `-v <challenge_dir>:/work/attachments:ro`；
2. 清单：`SolveSpec.attachments` 必须列出那些文件（否则 `read_attachment`
   按名字找不到、初始提示词的「## 附件」整段不出现）；
3. 反面对照：目录不存在时两条都必须是**空的**，不能因为修 bug 就凭空
   造出一个不存在的挂载点（那会让"这题没附件"变成"容器起不来"）。
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def challenge_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """造一个「某次扫描的题目素材目录」，并让 `challenge_dir()` 指过去。

    直接 monkeypatch `get_settings().scan_root` 而不是 `challenge_dir` 本身 ——
    那样测到的才是**真实**的拼路径逻辑（`<scan_root>/<scan_id>/challenge`），
    而 monkeypatch `challenge_dir` 会让被测代码和被断言的对象变成同一个假值。
    """
    from app.scans import runner as scans_runner

    scan_root = tmp_path / "_scan_runs"
    scan_root.mkdir()

    class _Settings:
        # 真实 `Settings.scan_root` 也是个 property（`config.py`），
        # 这里照抄那个形状 —— 换成普通属性会让被测代码走另一条取值路径。
        @property
        def scan_root(self) -> Path:
            return scan_root

    monkeypatch.setattr(scans_runner, "get_settings", _Settings)

    folder = scan_root / "scan_demo" / scans_runner.CHALLENGE_DIRNAME
    folder.mkdir(parents=True)
    (folder / "challenge.apk").write_bytes(b"PK\x03\x04not-really-a-zip")
    (folder / ".hidden").write_bytes(b"x")  # 隐藏文件不该进清单
    (folder / "subdir").mkdir()
    return folder


def test_challenge_dir_is_what_claim_writes_to(challenge_folder: Path) -> None:
    """前提：`challenge_dir()` 指向 `claim()` 铺素材的那个目录。

    这条是另外两条的地基 —— 如果这个路径口径变了（比如哪天改成
    `_uploads/<id>`），下面的挂载测试会跟着指错地方而**仍然通过**。
    """
    from app.scans import runner as scans_runner

    assert scans_runner.challenge_dir("scan_demo") == challenge_folder


def test_sandbox_mounts_the_challenge_folder_read_only(
    challenge_folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★★ 核心回归：附件目录必须以 `:ro` 挂到容器的 `/work/attachments`。

    修之前 `_run_engine` 只传 `workspace_dir`，这条 `-v` 根本不存在，
    所以模型看到的是一个空 `/work`。
    """
    from app.kernel import runner as kernel_runner
    from app.scans import agent_runner

    ws = challenge_folder.parent / "agent_run" / "workspace"
    ws.mkdir(parents=True)

    box = kernel_runner.SandboxContainer(
        name="suvex-scan-test",
        image="suvex-sandbox-ir",
        network_isolated=True,
        # ★ 这一行就是被测的那一处接线（`_run_engine` 里同样一行）。
        attachments_dir=agent_runner.challenge_dir("scan_demo"),
        workspace_dir=ws,
    )
    argv = box._argv()
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    wanted = f"{challenge_folder}:{kernel_runner.ATTACHMENT_DIR}:ro"
    assert wanted in mounts, (
        "题目附件目录没有挂进容器 —— 模型会以为这题没有附件，"
        f"然后拿几百轮去盲搜文件系统。实际挂载：{mounts}"
    )


def test_no_attachment_dir_means_no_mount(tmp_path: Path) -> None:
    """反面对照：这题真没附件时**不**挂一个不存在的路径。

    `--read-only` 下多挂一条指向不存在目录的 `-v` 会让 docker 直接起不来，
    而报错读起来像"镜像坏了"。
    """
    from app.kernel import runner as kernel_runner

    ws = tmp_path / "workspace"
    ws.mkdir()
    box = kernel_runner.SandboxContainer(
        name="suvex-scan-test",
        image="suvex-sandbox-ir",
        network_isolated=True,
        workspace_dir=ws,
    )
    argv = box._argv()
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    assert not [m for m in mounts if kernel_runner.ATTACHMENT_DIR in m], (
        f"没有附件时不该挂 /work/attachments，实际：{mounts}"
    )


def test_solve_spec_attachments_lists_real_files(challenge_folder: Path) -> None:
    """★★ `_scan_attachments()` 必须列出素材文件（跳过目录与隐藏文件）。

    ## 为什么这条重要

    `SolveSpec.attachments` 原来写死成 `()`。空元组有两个后果：
      · `read_attachment` 在 `spec.attachments` 里按名字找 → 永远"没有这个附件"；
      · `_initial_prompt` 的 `if spec.attachments:` 不成立 → 「## 附件」整段不出现。

    实测里模型的结论正是 "可用附件：（无附件）"。
    """
    from app.scans import agent_runner

    got = agent_runner._scan_attachments("scan_demo")
    names = [p.name for p in got]
    assert names == ["challenge.apk"], (
        f"附件清单不对（应只含顶层普通文件）：{names}"
    )
    assert all(isinstance(p, Path) for p in got)


def test_scan_attachments_is_empty_when_folder_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """目录不存在 → 空元组（不是报错，也不是凭空造路径）。"""
    from app.scans import agent_runner, runner as scans_runner

    scan_root = tmp_path / "nope"
    scan_root.mkdir()

    class _Settings:
        @property
        def scan_root(self) -> Path:
            return scan_root

    monkeypatch.setattr(scans_runner, "get_settings", _Settings)
    assert agent_runner._scan_attachments("scan_missing") == ()


def test_engine_wires_the_mount_and_the_list() -> None:
    """静态守卫：`_run_engine` / `build_solve_spec_from_scan` 里那两处接线还在。

    ⚠️ 为什么用 `inspect.getsource` 而不是跑一遍 `_run_engine`：
    那函数要起真 docker 容器、要 LLM 网关，单测里跑不动。而这两处
    接线各自只有一行，被删掉时的症状是**静默的**（见模块 docstring），
    所以值得有一条"这行还在不在"的断言盯着。
    这一条也解释了 AGENTS.md 里"全量测试跑着时不要改源文件"那条纪律。

    两条断言分别对应 2026-10-27 修的两个洞：
      · `attachments_dir=challenge_dir(...)` —— 挂载；
      · `attachments=_scan_attachments(...)` —— 清单（原来是 `()`）。
    """
    import inspect

    from app.scans import agent_runner

    engine = inspect.getsource(agent_runner._run_engine)
    assert "attachments_dir=challenge_dir(spec.scan_id)" in engine, (
        "扫描路径起沙箱时没挂题目附件目录 —— 模型会以为这题没有附件。"
    )

    spec_src = inspect.getsource(agent_runner.build_solve_spec_from_scan)
    assert "attachments=_scan_attachments(spec.scan_id)" in spec_src, (
        "SolveSpec.attachments 又变回空的了 —— read_attachment 会找不到任何附件。"
    )
    assert "attachments=()," not in spec_src, (
        "SolveSpec.attachments 被写死成空元组（2026-10-27 修掉的那个洞）。"
    )
