"""扫描产物清理（2026-10-11 补）：`remove_scan_artifacts` 与 `sweep_orphan_artifacts`。

## 为什么这些测试值得单独一个文件

它们测的是**会删磁盘文件**的逻辑。这类代码的失效模式与别的不同：

* 别的逻辑错了 → 功能不对，看得见；
* 这里错了 → **数据没了，回不来**（用户的题目附件、上传的原件）。

所以断言的重点不是"该删的删了"，而是**"不该删的一个都没动"**。
下面的用例里，"保住附件"那几条比"删掉产物"那几条更重要。

## 一个真实背景

在这之前 `purge_scan` 只删数据库行、**不删磁盘产物** ——
所以"永久删除"之后磁盘上永远留着 5–7 MB 且再无引用。
这些测试同时也是那个缺口的回归闸。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from app.core.vocab import ScanStatus
from app.scans import runner as runner_mod
from app.scans import service as scans_service
from app.scans.models import Scan
from tests.conftest import SCAN_WORK_ROOT
from tests.factories import make_scan

# ══════════════════════════════════════════════════════════
# 辅助
# ══════════════════════════════════════════════════════════


def _artifact_dir(scan_id: str) -> Path:
    """造一个扫描产物目录（带一个文件，模拟真实产物）。"""
    path = Path(SCAN_WORK_ROOT) / scan_id
    path.mkdir(parents=True, exist_ok=True)
    (path / "scan.log").write_text("假的扫描日志", encoding="utf-8")
    return path


def _uploads_dir(scan_id: str) -> Path:
    """造一个附件目录 —— 它**不该**被产物清理碰到。"""
    path = Path(SCAN_WORK_ROOT) / "_uploads" / scan_id
    path.mkdir(parents=True, exist_ok=True)
    (path / "chall.zip").write_bytes(b"PK\x03\x04 fake attachment")
    return path


# ══════════════════════════════════════════════════════════
# 一、remove_scan_artifacts：基本行为
# ══════════════════════════════════════════════════════════


def test_removes_the_artifact_directory() -> None:
    """★ 该删的删掉，且返回 True。"""
    scan_id = "scan_01TESTARTIFACTREMOVALAA"
    path = _artifact_dir(scan_id)
    assert path.is_dir()

    assert runner_mod.remove_scan_artifacts(scan_id) is True
    assert not path.exists()


def test_missing_directory_is_not_an_error() -> None:
    """目录本来就不在（手工清过）→ 返回 False，**不抛异常**。

    调用方是 `purge_scan` 的后半段：数据库那边已经删干净了，
    对用户来说这次操作就是成功的。这里抛异常会让用户以为"没删掉"，
    然后重复操作。
    """
    assert runner_mod.remove_scan_artifacts("scan_01TESTNEVEREXISTEDAA") is False


def test_never_touches_sibling_directories() -> None:
    """★★ 最重要的一条：产物清理**绝不能**碰附件目录。

    `scan_root/` 下住着五类东西，只有 `<scan_id>/` 归这个函数管：
      · `_uploads/<scan_id>/`     —— 附件原件，重跑还要用
      · `_uploads/_staging/`      —— 暂存，有专门的 sweep
      · `_ctf2_attachments/`      —— CTF2 附件，跟题目走
      · 转录目录                   —— CTF2 对话记录

    附件**删了就回不来**（用户得重新上传 / 重新下题），所以这条
    断言比"产物被删掉了"重要得多。
    """
    scan_id = "scan_01TESTKEEPSIBLINGSAAAAA"
    artifact = _artifact_dir(scan_id)
    uploads = _uploads_dir(scan_id)
    ctf2 = Path(SCAN_WORK_ROOT) / "_ctf2_attachments" / "chall_x"
    ctf2.mkdir(parents=True, exist_ok=True)
    (ctf2 / "题目附件.zip").write_bytes(b"PK\x03\x04")

    runner_mod.remove_scan_artifacts(scan_id)

    assert not artifact.exists(), "产物目录该被删掉"
    assert uploads.is_dir(), "★ 附件目录被误删了"
    assert (uploads / "chall.zip").is_file(), "★ 附件文件被误删了"
    assert ctf2.is_dir(), "★ CTF2 附件目录被误删了"


def test_underscore_prefixed_names_are_refused() -> None:
    """★ 形状校验：只接受 `scan_` 开头的 id。

    这是防"将来有人把 URL 参数传进来"的护栏 —— `_uploads` 这种
    下划线开头的目录名必须被拒掉。
    """
    for bad in ("_uploads", "_ctf2_attachments", "_staging", "", "scan", "scans_x"):
        assert runner_mod.remove_scan_artifacts(bad) is False, f"{bad!r} 不该被接受"


def test_path_traversal_is_refused() -> None:
    """★★ `../` 不能穿越出去删别的目录（第一层：形状校验）。

    `scan_id` 现在来自数据库主键（`scan_<ULID>`），但形状校验必须
    挡住穿越 —— 否则一旦有人把请求参数接到这里，就能删任意目录。
    """
    outside = Path(SCAN_WORK_ROOT).parent / "SHOULD_NOT_BE_DELETED"
    outside.mkdir(parents=True, exist_ok=True)
    (outside / "important.txt").write_text("别删我", encoding="utf-8")
    try:
        for bad in ("scan_../../SHOULD_NOT_BE_DELETED", "scan_..", "scan_/etc"):
            assert runner_mod.remove_scan_artifacts(bad) is False
        assert outside.is_dir(), "★ 穿越成功了，删到了 scan_root 之外"
        assert (outside / "important.txt").is_file()
    finally:
        import shutil

        shutil.rmtree(outside, ignore_errors=True)


def test_symlink_pointing_outside_is_refused() -> None:
    """★★ 第二层防线：`resolve()` 之后的 `is_relative_to` 校验。

    ## 为什么需要这条单独的测试（2026-10-11 变异验证发现）

    上面那条用例走的全是 `../` 形式的 id，而它们**先被形状校验
    （`isalnum()`）挡住了** —— 于是 `is_relative_to` 那层永远走不到。
    变异验证证实了这一点：把 `is_relative_to` 拆掉，全部用例仍然绿，
    说明它当时**没有任何测试覆盖**。

    要真正打到那一层，得构造一个**通过形状校验、但解析后跑到
    `scan_root` 之外**的路径 —— 符号链接正好如此：
    `scan_<合法字符>` 这个名字本身没问题，但它是**指向外面的软链**。

    ⚠️ 第二层不是多余的：第一层管的是"名字长什么样"，第二层管的是
    "解析之后到底落在哪"。将来若有人放宽第一层（比如允许 `-`），
    第二层仍然挡得住。
    """
    import os
    import shutil

    outside = Path(SCAN_WORK_ROOT).parent / "SYMLINK_TARGET_MUST_SURVIVE"
    outside.mkdir(parents=True, exist_ok=True)
    (outside / "important.txt").write_text("别删我", encoding="utf-8")

    link_id = "scan_01TESTSYMLINKOUTSIDEAAA"  # 形状完全合法
    link = Path(SCAN_WORK_ROOT) / link_id
    try:
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            import pytest

            pytest.skip("本平台不支持创建符号链接（Windows 需开发者模式或管理员）")

        assert runner_mod.remove_scan_artifacts(link_id) is False, (
            "★ 符号链接穿越成功了 —— 删到了 scan_root 之外"
        )
        assert outside.is_dir(), "★ 软链目标被删了"
        assert (outside / "important.txt").is_file()
    finally:
        if link.is_symlink():
            link.unlink()
        shutil.rmtree(outside, ignore_errors=True)


# ══════════════════════════════════════════════════════════
# 二、sweep_orphan_artifacts：只删"数据库里已无记录"的
# ══════════════════════════════════════════════════════════


def test_sweeps_directory_with_no_database_row(db_session: Session) -> None:
    """★ 数据库里查不到的产物目录 = 孤儿 → 删掉。"""
    orphan_id = "scan_01TESTORPHANNOENTITYAA"
    path = _artifact_dir(orphan_id)

    removed = runner_mod.sweep_orphan_artifacts(db_session)

    assert removed >= 1
    assert not path.exists(), "孤儿产物目录没被清掉"


def test_keeps_directory_whose_scan_still_exists(db_session: Session) -> None:
    """★★ 数据库里还有记录的扫描，产物**必须留着**。

    哪怕它是归档状态 —— 归档是"收起来"，用户随时可能要看产物。
    这条同时钉住了"不按时间删"这个设计决定。
    """
    scan = make_scan(db_session, status=ScanStatus.COMPLETED.value)
    path = _artifact_dir(scan.id)

    runner_mod.sweep_orphan_artifacts(db_session)

    assert path.is_dir(), "★ 还在数据库里的扫描，产物被误删了"


def test_keeps_archived_scan_artifacts(db_session: Session) -> None:
    """★ 归档的扫描产物也要留着（归档 ≠ 删除）。"""
    from app.core.timeutil import now_utc

    scan = make_scan(db_session, status=ScanStatus.COMPLETED.value)
    scan.archived_at = now_utc()
    db_session.commit()
    path = _artifact_dir(scan.id)

    runner_mod.sweep_orphan_artifacts(db_session)

    assert path.is_dir(), "★ 归档扫描的产物被误删了 —— 归档不是删除"


def test_never_sweeps_uploads_or_ctf2_dirs(db_session: Session) -> None:
    """★★ 孤儿清理**绝不能**扫到下划线开头的兄弟目录。

    它们的生命周期不跟扫描走：附件跟任务、题目附件跟题目。
    即便数据库里没有任何扫描记录，它们也必须原地不动。
    """
    uploads = Path(SCAN_WORK_ROOT) / "_uploads" / "scan_01TESTORPHANUPLOADAA"
    uploads.mkdir(parents=True, exist_ok=True)
    (uploads / "原件.zip").write_bytes(b"PK\x03\x04")
    staging = Path(SCAN_WORK_ROOT) / "_uploads" / "_staging"
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "half-uploaded.bin").write_bytes(b"xx")
    ctf2 = Path(SCAN_WORK_ROOT) / "_ctf2_attachments" / "chall_y"
    ctf2.mkdir(parents=True, exist_ok=True)
    (ctf2 / "附件.zip").write_bytes(b"PK\x03\x04")

    runner_mod.sweep_orphan_artifacts(db_session)

    assert uploads.is_dir() and (uploads / "原件.zip").is_file(), "★ 附件被误删"
    assert ctf2.is_dir() and (ctf2 / "附件.zip").is_file(), "★ CTF2 附件被误删"
    # 暂存区归 `sweep_staging` 管（按 TTL），不归这里 —— 这里不该动它
    assert staging.is_dir(), "★ 暂存目录被产物清理碰了（它归 sweep_staging）"


def test_sweep_is_idempotent(db_session: Session) -> None:
    """清第二遍什么都不做（没有孤儿了）。"""
    runner_mod.sweep_orphan_artifacts(db_session)
    assert runner_mod.sweep_orphan_artifacts(db_session) == 0


# ══════════════════════════════════════════════════════════
# 三、接进 purge_scan 与 sweep_retention
# ══════════════════════════════════════════════════════════


def test_purge_scan_removes_artifacts(db_session: Session) -> None:
    """★★ 回归闸：`purge_scan` 必须**连磁盘产物一起删**。

    这正是 2026-10-11 之前缺的那一步 —— 那时它只删数据库行，
    磁盘上的产物永远留着，而且再无任何引用。
    """
    from app.core.timeutil import now_utc

    scan = make_scan(db_session, status=ScanStatus.FAILED.value)
    scan.deleted_at = now_utc()  # purge 要求先进回收站
    db_session.commit()
    path = _artifact_dir(scan.id)
    assert path.is_dir()

    scans_service.purge_scan(db_session, scan.id)

    assert not path.exists(), "★ purge 没删磁盘产物（这就是本次修的缺口）"
    assert db_session.get(Scan, scan.id) is None


def test_purge_keeps_the_uploads_of_that_scan(db_session: Session) -> None:
    """★★ purge 删产物，但**保住同 id 的附件**。

    两者生命周期不同：产物是派生物（报告已在库里），附件是用户交上来的
    原件（同一道题重跑还要用）。`purge_scan` 的语义是"删这次扫描的结果"，
    不是"删用户的素材"。
    """
    from app.core.timeutil import now_utc

    scan = make_scan(db_session, status=ScanStatus.FAILED.value)
    scan.deleted_at = now_utc()
    db_session.commit()
    artifact = _artifact_dir(scan.id)
    uploads = _uploads_dir(scan.id)

    scans_service.purge_scan(db_session, scan.id)

    assert not artifact.exists(), "产物该删"
    assert uploads.is_dir(), "★ purge 把用户的附件原件也删了"
    assert (uploads / "chall.zip").is_file()


def test_purge_survives_a_missing_artifact_directory(db_session: Session) -> None:
    """产物目录已被手工清掉时，purge **仍然成功**。

    数据库那边删干净了，对用户来说就是成功。因为磁盘清理失败而抛异常，
    会让用户以为"没删掉"，然后重复操作。
    """
    from app.core.timeutil import now_utc

    scan = make_scan(db_session, status=ScanStatus.FAILED.value)
    scan.deleted_at = now_utc()
    db_session.commit()
    # 故意不建产物目录

    scans_service.purge_scan(db_session, scan.id)  # 不抛异常即通过

    assert db_session.get(Scan, scan.id) is None


def test_sweep_retention_reports_orphan_count(db_session: Session) -> None:
    """`sweep_retention` 的返回值里要有孤儿清理的计数。

    没有这个字段，运维在启动日志里看不到"磁盘被清了多少" ——
    而"清理到底有没有生效"是这类后台任务最该能回答的问题。
    """
    _artifact_dir("scan_01TESTSWEEPREPORTSAAAAA")

    result = scans_service.sweep_retention(db_session)

    assert "orphanArtifactsSwept" in result
    assert result["orphanArtifactsSwept"] >= 1


def test_sweep_retention_keeps_live_scans_artifacts(db_session: Session) -> None:
    """★ 走完整条清理路径时，活着的扫描产物也不能被碰。"""
    scan = make_scan(db_session, status=ScanStatus.RUNNING.value)
    path = _artifact_dir(scan.id)

    scans_service.sweep_retention(db_session)

    assert path.is_dir(), "★ 正在跑的扫描产物被清理删了"
