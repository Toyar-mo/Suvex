"""CTF2 持久工作区（v0.29）：跨容器保留现场 + 题目删除时清理。

## 这组用例守的两件事

**① 现场真的能跨容器存活**（这是"续解"有意义的前提）

`/work` 是 tmpfs、容器 `finally` 必销毁，所以解题产物本来活不过一次
运行。实测（2026-10-12，`[FlareOn3]GUI`）：模型跑满 78 轮、写了 12 个
C# harness、patch 过 CLR 元数据 —— 全部随容器消失，续解时得重做。

**② 加了持久化就必须有清理**（否则复制"只增不减"那条缺口）

而清理的判据是"**题目记录还在不在**"，不是时间 —— 与
`sweep_orphan_artifacts` 选"孤儿"而不是"时间"是同一条理由：
解出后放着不动的题，产物正是最该留的。

所以下面"保住"类的用例比"删掉"类的更重要。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from app.scans import runner as scans_runner
from tests.conftest import SCAN_WORK_ROOT
from tests.factories import make_scan


def _workspace_root() -> Path:
    return Path(SCAN_WORK_ROOT) / "_ctf2_workspaces"


def _make_workspace(challenge_id: str, *, content: str = "产物") -> Path:
    """造一个持久工作区（带一个文件，模拟真实产物）。"""
    path = _workspace_root() / challenge_id
    path.mkdir(parents=True, exist_ok=True)
    (path / "layer1.il").write_text(content, encoding="utf-8")
    return path


# ══════════════════════════════════════════════════════════
# 一、路径生成与安全
# ══════════════════════════════════════════════════════════


def test_workspace_path_is_under_the_workspaces_root() -> None:
    """★ 工作区落在 `_ctf2_workspaces/<challenge_id>/` 下。

    ⚠️ 必须 `create=True`：`create=False` 且目录不存在时返回 `None`
    （那是刻意的 —— 见下一条用例），所以这里要真的建出来才看得到路径。
    """
    from app.kernel import runner as ctf2_runner

    path = ctf2_runner._workspace_dir("ctf2chal_TESTWORKSPACEPATH", create=True)
    assert path is not None
    assert path.name == "ctf2chal_TESTWORKSPACEPATH"
    assert path.parent.name == "_ctf2_workspaces"


def test_workspace_is_not_created_unless_asked() -> None:
    """★★ `create=False` 时**不建目录**。

    这条挡的是"读路径顺手建目录"：那会让"这道题从没跑过"与
    "跑过但没留下东西"变成同一个状态，而两者该给用户的信息完全不同。
    """
    from app.kernel import runner as ctf2_runner

    cid = "ctf2chal_NEVERCREATEDWORKSPACE"
    path = _workspace_root() / cid
    assert not path.exists()

    result = ctf2_runner._workspace_dir(cid)  # 默认 create=False
    assert not path.exists(), "★ 读路径不该建目录"
    assert result is None, "目录不存在时应返回 None（调用方据此决定不挂载）"


def test_workspace_is_created_when_asked() -> None:
    """★ `create=True` 时才建（那是真正要起容器时）。"""
    from app.kernel import runner as ctf2_runner

    cid = "ctf2chal_DOCREATEWORKSPACE1"
    result = ctf2_runner._workspace_dir(cid, create=True)
    assert result is not None and result.is_dir()


def test_workspace_is_chowned_to_the_solver_uid(monkeypatch) -> None:  # noqa: ANN001
    """★★★ 建工作区时必须把属主改成容器里的 `solver`（uid 1000）。

    ⚠️⚠️ 这条守的是一个**实测发现的真 bug**（2026-10-13）：容器以
    `USER solver`（uid 1000）运行，而目录由服务进程（root）`mkdir`
    出来 → `root:root 0755` → **uid 1000 只读**。

    于是 v0.29 的"持久工作区"**从未真正生效过**。实测里模型在第 23 轮
    写了 `/work/persist/harness.cs`，第 26 轮自己发现并放弃：

        /work/persist is not writable. Let me use /work.

    后果正是持久区要解决的那个问题本身（产物退回 tmpfs、容器一停全没），
    外加白费 3 轮。

    ⚠️ 测试在 Windows 上没有 `os.chown`，所以断言的是"**调用意图**"
    （用 monkeypatch 抓调用参数），而不是真实属主 —— 真实属主由
    Kali 上的实测覆盖。
    """
    from app.kernel import runner as ctf2_runner

    calls: list[tuple[str, int, int]] = []

    def _fake_chown(path, uid, gid):  # noqa: ANN001, ANN202
        calls.append((str(path), uid, gid))

    monkeypatch.setattr(ctf2_runner.os, "chown", _fake_chown, raising=False)

    cid = "ctf2chal_CHOWNEDWORKSPACE1"
    result = ctf2_runner._workspace_dir(cid, create=True)

    assert result is not None
    assert calls, "★★★ 建完工作区没有 chown —— 容器里将不可写（持久区失效）"
    path, uid, gid = calls[-1]
    assert path == str(result)
    assert (uid, gid) == (ctf2_runner.SOLVER_UID, ctf2_runner.SOLVER_GID)
    assert (uid, gid) == (1000, 1000), "必须与镜像里 useradd --uid 1000 一致"


def test_chown_failure_does_not_break_the_workspace(monkeypatch) -> None:  # noqa: ANN001
    """★★ chown 失败**不该**让解题跑不了。

    服务可能不以 root 跑（开发机），那时 `os.chown` 抛 `PermissionError`。
    最坏结果是"持久区不可写"（退回修复前的行为），而不该是"整道题解不了"。

    ⚠️ 但要记 warning —— 静默退化成修复前的行为，正是这个 bug
    藏了这么久的原因。
    """
    from app.kernel import runner as ctf2_runner

    def _boom(path, uid, gid):  # noqa: ANN001, ANN202
        raise PermissionError("not root")

    monkeypatch.setattr(ctf2_runner.os, "chown", _boom, raising=False)

    result = ctf2_runner._workspace_dir("ctf2chal_CHOWNFAILWORKSPACE", create=True)
    assert result is not None and result.is_dir(), "chown 失败不该让工作区不可用"


def test_solver_uid_matches_the_image() -> None:
    """★★ `SOLVER_UID` 必须与镜像 Dockerfile 里的 `useradd --uid` 一致。

    ⚠️ 两处各写一份会漂移，而漂移的症状**与那个 bug 一模一样**
    （持久区又变只读）—— 很难再查一遍。所以这里直接读 Dockerfile 断言。
    """
    import re
    from pathlib import Path

    from app.kernel import runner as ctf2_runner

    dockerfile = (
        Path(__file__).resolve().parents[2] / "docker" / "suvex-sandbox" / "Dockerfile"
    )
    if not dockerfile.is_file():
        import pytest

        pytest.skip(f"找不到 {dockerfile}")

    text = dockerfile.read_text(encoding="utf-8")
    m = re.search(r"useradd[^\n]*--uid\s+(\d+)", text)
    assert m, "★ Dockerfile 里找不到 useradd --uid"
    assert int(m.group(1)) == ctf2_runner.SOLVER_UID, (
        f"★ 镜像里 solver 是 uid {m.group(1)}，而 runner 里写的是 "
        f"{ctf2_runner.SOLVER_UID} —— 持久区会再次变成只读"
    )
    # gid 同理（`--gid` 或 `--user-group` 都行，这里只查显式的）。
    m2 = re.search(r"useradd[^\n]*--gid\s+(\d+)", text)
    if m2:
        assert int(m2.group(1)) == ctf2_runner.SOLVER_GID


def test_suspicious_challenge_id_is_refused() -> None:
    """★★ 路径穿越必须被拒 —— 这个目录是**读写挂载**进容器的。

    容器里跑的是题目给的不可信内容，而路径拼接一旦能被影响，
    就是"任意宿主目录暴露给不可信内容"。
    """
    from app.kernel import runner as ctf2_runner

    for bad in ("../../etc", "a/b", "a\\b", "..", ""):
        assert ctf2_runner._workspace_dir(bad, create=True) is None, (
            f"{bad!r} 不该被接受"
        )


# ══════════════════════════════════════════════════════════
# 二、挂载参数（现场能否真的跨容器存活）
# ══════════════════════════════════════════════════════════


def test_argv_mounts_the_workspace_read_write(tmp_path: Path) -> None:
    """★★ 工作区以 **rw** 挂到 `/work/persist`。

    两条都要断言：
      · 挂载点是 `/work/persist`（**不是** `/work` —— 直接替换 /work
        会让 attachments 子挂载失效）；
      · 是 `rw` 而不是 `ro`（只读的话模型写不进去，等于没做）。
    """
    from app.kernel import runner as ctf2_runner

    ws = tmp_path / "ws"
    ws.mkdir()
    container = ctf2_runner.SandboxContainer(
        name="ctf2-test-ws",
        image=ctf2_runner.IMAGE_BASE,
        workspace_dir=ws,
    )
    argv = container._argv()

    expected = f"{ws}:{ctf2_runner.PERSIST_DIR}:rw"
    assert expected in argv, f"工作区挂载参数缺失或不是 rw：期望 {expected}"
    assert ctf2_runner.PERSIST_DIR == "/work/persist", (
        "挂载点必须是 /work 的子目录，不能是 /work 本身"
    )


def test_argv_omits_workspace_when_absent() -> None:
    """★ 没给工作区时**不挂**（挂不存在的路径会得到空目录，与"没挂"难区分）。"""
    from app.kernel import runner as ctf2_runner

    container = ctf2_runner.SandboxContainer(
        name="ctf2-test-nows",
        image=ctf2_runner.IMAGE_BASE,
        workspace_dir=None,
    )
    argv = container._argv()
    assert not any(ctf2_runner.PERSIST_DIR in str(a) for a in argv)


def test_argv_does_not_replace_work_tmpfs(tmp_path: Path) -> None:
    """★★ 工作区挂载**不能**把 `/work` 这个 tmpfs 换掉。

    如果实现成 `-v <host>:/work`，attachments 的子挂载会失效 ——
    而症状是"模型看不到附件"，看起来像下载失败。
    """
    from app.kernel import runner as ctf2_runner

    ws = tmp_path / "ws"
    ws.mkdir()
    container = ctf2_runner.SandboxContainer(
        name="ctf2-test-tmpfs",
        image=ctf2_runner.IMAGE_BASE,
        workspace_dir=ws,
    )
    argv = container._argv()

    assert f"{ws}:/work" not in argv, "★ 工作区挂载覆盖了 /work"
    # tmpfs 声明必须还在
    assert any("size=512m" in str(a) for a in argv), "★ /work 的 tmpfs 声明丢了"


# ══════════════════════════════════════════════════════════
# 三、清理：题目还在就保住，题目没了才删
# ══════════════════════════════════════════════════════════


def test_sweep_keeps_workspace_of_a_live_challenge(
    db_session: Session, ctf2_challenge
) -> None:
    """★★ 题目**还在库里** → 工作区必须留着。

    这是最重要的一条：解出后放着不动的题，产物正是最该留的
    （复盘、写 WP 都要它）。按时间删会把它们误伤。

    ⚠️ 用现成的 `ctf2_challenge` 夹具，不手造题目行 —— 手造要自己补
    account / competition 两层外键，而那是夹具已经做好的事
    （实测撞过 `ForeignKeyViolation: ctf2_competitions_account_id`）。
    """
    path = _make_workspace(ctf2_challenge.id)
    scans_runner.sweep_orphan_ctf2_workspaces(db_session)
    assert path.is_dir(), "★ 题目还在，工作区被误删了"


def test_sweep_removes_workspace_of_a_deleted_challenge(db_session: Session) -> None:
    """★ 题目**已从库里删掉** → 工作区是孤儿，删掉。"""
    orphan = _make_workspace("ctf2chal_ORPHANNOCHALLENGEROW")
    removed = scans_runner.sweep_orphan_ctf2_workspaces(db_session)
    assert removed >= 1
    assert not orphan.exists()


def test_sweep_never_touches_attachments_dir(db_session: Session) -> None:
    """★★ 清理工作区时**绝不能**碰 `_ctf2_attachments/`。

    附件跟**题目**走，工作区跟**多次尝试**走 —— 两个生命周期不同。
    顺手一起删会让"题目还在但附件没了"，而附件是只读原件，
    删了要重新下载。
    """
    att = Path(SCAN_WORK_ROOT) / "_ctf2_attachments" / "ctf2chal_SHOULD_SURVIVE"
    att.mkdir(parents=True, exist_ok=True)
    (att / "chall.zip").write_bytes(b"PK\x03\x04")

    scans_runner.sweep_orphan_ctf2_workspaces(db_session)

    assert att.is_dir(), "★ 附件目录被工作区清理误删了"
    assert (att / "chall.zip").is_file()


def test_sweep_never_touches_scan_artifacts(db_session: Session) -> None:
    """★ 也不该碰扫描产物（那是另一个清理函数的职责）。"""
    scan = make_scan(db_session, status="completed")
    art = Path(SCAN_WORK_ROOT) / scan.id
    art.mkdir(parents=True, exist_ok=True)

    scans_runner.sweep_orphan_ctf2_workspaces(db_session)

    assert art.is_dir(), "★ 扫描产物被工作区清理碰了"


def test_sweep_is_idempotent(db_session: Session) -> None:
    """清第二遍什么都不做。"""
    scans_runner.sweep_orphan_ctf2_workspaces(db_session)
    assert scans_runner.sweep_orphan_ctf2_workspaces(db_session) == 0


def test_sweep_returns_zero_when_root_missing(db_session: Session) -> None:
    """工作区根目录不存在时返回 0（不抛异常）。"""
    import shutil

    root = _workspace_root()
    shutil.rmtree(root, ignore_errors=True)
    assert scans_runner.sweep_orphan_ctf2_workspaces(db_session) == 0


# ══════════════════════════════════════════════════════════
# 四、接进 sweep_retention
# ══════════════════════════════════════════════════════════


def test_sweep_retention_reports_workspace_count(db_session: Session) -> None:
    """★ `sweep_retention` 的返回值要带上工作区清理的计数。

    没有这个字段，运维在启动日志里看不到它 —— 而"清理有没有生效"
    正是这类后台任务最该能回答的问题。
    """
    from app.scans import service as scans_service

    _make_workspace("ctf2chal_REPORTSWEEPWORKSPACE")

    result = scans_service.sweep_retention(db_session)

    assert "ctf2WorkspacesSwept" in result
    assert result["ctf2WorkspacesSwept"] >= 1
