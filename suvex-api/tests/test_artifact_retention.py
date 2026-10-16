"""归档产物的保留期清理（README 缺口清单「扫描产物目录只增不减」那一条的后半段）。

## 这组用例守的是什么

"扫描产物目录只增不减"有**两个**来源，2026-10-11 那次只修了第一个：

| 来源 | 谁负责 | 状态 |
| --- | --- | --- |
| 数据库里**已无记录**的孤儿目录 | `sweep_orphan_artifacts` | ✅ 已修 |
| 记录还在、但**早已归档**的目录 | ★ `sweep_retired_artifacts` | 本文件 |

第二个才是主因：归档是**自动**的（`sweep_retention` 到点就打
`archived_at`），而产物在那之后永远留着。

## ★★ 这类测试为什么必须用**真实目录树**

它们测的是**会删磁盘文件**的逻辑，而失效模式与别的代码不同：

* 别的逻辑错了 → 功能不对，看得见；
* 这里错了 → **数据没了，回不来**（用户的附件原件、题目素材）。

所以断言的重点不是"该删的删了"，而是**"不该删的一个都没动"**。
mock 掉文件系统的话，"删错路径"这个最严重的失效模式**根本测不出来**
—— 因为 mock 只会按你写的规则回应你。

下面 `_tree()` 造的是**真的**目录与文件，`_assert_intact()` 逐个核对
它们仍然存在（含文件内容）。
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.core.timeutil import now_utc
from app.core.vocab import ScanStatus
from app.scans import runner as runner_mod
from app.scans import service as scans_service
from app.settings.models import DEFAULT_ARTIFACT_RETENTION_DAYS
from tests.conftest import SCAN_WORK_ROOT
from tests.factories import make_scan

# ══════════════════════════════════════════════════════════
# 辅助：造真实的目录树
# ══════════════════════════════════════════════════════════


def _artifact_dir(scan_id: str) -> Path:
    """一次扫描的产物目录（真的目录 + 真的文件）。"""
    path = Path(SCAN_WORK_ROOT) / scan_id
    path.mkdir(parents=True, exist_ok=True)
    (path / "scan.log").write_text("假的扫描日志", encoding="utf-8")
    (path / "nuclei.jsonl").write_text('{"template":"x"}\n', encoding="utf-8")
    return path


def _challenge_dir(scan_id: str) -> Path:
    """`artifact` 模式的派生目录 —— 它在**产物目录里面**。

    ★ 它跟着产物一起删是**对的**（派生物，从 `_uploads/` 的原件还能再生成），
    所以本文件不断言它必须留下 —— 但也不断言它必须没，
    见 `test_challenge_dir_is_inside_the_artifact_dir`。
    """
    path = Path(SCAN_WORK_ROOT) / scan_id / "challenge"
    path.mkdir(parents=True, exist_ok=True)
    (path / "chall.bin").write_bytes(b"\x7fELF fake")
    return path


def _uploads_dir(scan_id: str) -> Path:
    """附件原件目录 —— **绝不该**被产物清理碰到。"""
    path = Path(SCAN_WORK_ROOT) / "_uploads" / scan_id
    path.mkdir(parents=True, exist_ok=True)
    (path / "原件.zip").write_bytes(b"PK\x03\x04 fake attachment")
    return path


def _staging_dir() -> Path:
    """附件暂存区 —— 归 `sweep_staging` 管，不归这里。"""
    path = Path(SCAN_WORK_ROOT) / "_uploads" / "_staging" / "batch_x"
    path.mkdir(parents=True, exist_ok=True)
    (path / "half.bin").write_bytes(b"xx")
    return path


def _ctf2_attachment_dir() -> Path:
    """CTF2 题目附件 —— 跟题目走，不跟扫描走。"""
    path = Path(SCAN_WORK_ROOT) / "_ctf2_attachments" / "chall_z"
    path.mkdir(parents=True, exist_ok=True)
    (path / "附件.zip").write_bytes(b"PK\x03\x04")
    return path


def _archive(db: Session, *, days_ago: int, status: str = ScanStatus.COMPLETED.value):
    """造一条**已归档** `days_ago` 天的扫描。"""
    scan = make_scan(db, status=status)
    scan.archived_at = now_utc() - timedelta(days=days_ago)
    db.commit()
    return scan


def _assert_intact(*paths: Path) -> None:
    """这些路径**必须**原样还在（含里面的文件）。"""
    for path in paths:
        assert path.exists(), f"★ {path} 被误删了 —— 它不该归产物清理管"
    assert (Path(SCAN_WORK_ROOT) / "_uploads").is_dir(), "★ _uploads 整棵树被动了"
    assert (Path(SCAN_WORK_ROOT) / "_uploads" / "_staging").is_dir(), (
        "★ 暂存区被产物清理碰了（它归 sweep_staging）"
    )


# ══════════════════════════════════════════════════════════
# 一、该清的：已归档 + 超期
# ══════════════════════════════════════════════════════════


def test_retired_artifacts_are_removed(db_session: Session) -> None:
    """★ 已归档且超过保留期 → 产物目录被清掉。"""
    scan = _archive(db_session, days_ago=100)
    path = _artifact_dir(scan.id)

    removed = runner_mod.sweep_retired_artifacts(db_session, retention_days=90)

    assert removed >= 1
    assert not path.exists(), "已归档超期的产物没被清掉"


def test_the_challenge_dir_goes_with_the_artifacts(db_session: Session) -> None:
    """★ `challenge/` 在产物目录**里面**，所以跟着一起清 —— 这是对的。

    它是**派生物**（从 `_uploads/` 的原件还能再生成），不是用户交上来的
    原件。这条断言把那个语义钉住：将来有人把 `challenge/` 挪到
    `_uploads/` 下面（那才是错的），这条会变红。
    """
    scan = _archive(db_session, days_ago=100)
    artifact = _artifact_dir(scan.id)
    challenge = _challenge_dir(scan.id)
    assert challenge.is_relative_to(artifact), "前置：challenge 应当在产物目录里"

    runner_mod.sweep_retired_artifacts(db_session, retention_days=90)

    assert not challenge.exists(), "派生物应当跟着产物一起清"


def test_zero_means_disabled(db_session: Session) -> None:
    """★ `0` = **关闭**这条规则（与那套配置的约定一致）。

    ⚠️ 绝不能解释成"立刻清理"：一个想把自动清理关掉的人，
    填 0 之后反而把所有归档产物一次删光，那是数据事故。
    """
    scan = _archive(db_session, days_ago=9999)
    path = _artifact_dir(scan.id)

    assert runner_mod.sweep_retired_artifacts(db_session, retention_days=0) == 0
    assert path.is_dir(), "★ 填 0（关闭）时把产物删了 —— 0 不是'立刻清'"


def test_a_negative_retention_is_also_disabled(db_session: Session) -> None:
    """负数同样按"关闭"处理，而不是"删得更狠"。"""
    scan = _archive(db_session, days_ago=9999)
    path = _artifact_dir(scan.id)

    assert runner_mod.sweep_retired_artifacts(db_session, retention_days=-5) == 0
    assert path.is_dir()


# ══════════════════════════════════════════════════════════
# 二、不该清的（这一类最重要）
# ══════════════════════════════════════════════════════════


def test_live_scans_are_never_touched(db_session: Session) -> None:
    """★★ **正在跑的扫描**产物绝不能被清 —— 那会边跑边丢产物。

    这是"只按时间删"那种写法最危险的失效模式，所以单独钉一条。
    """
    running = make_scan(db_session, status=ScanStatus.RUNNING.value)
    queued = make_scan(db_session, status=ScanStatus.QUEUED.value)
    run_path = _artifact_dir(running.id)
    queue_path = _artifact_dir(queued.id)

    runner_mod.sweep_retired_artifacts(db_session, retention_days=1)

    assert run_path.is_dir(), "★ 正在跑的扫描产物被清了"
    assert queue_path.is_dir(), "★ 排队中的扫描产物被清了"


def test_unarchived_finished_scans_are_kept(db_session: Session) -> None:
    """★★ **没归档**的扫描产物要留着 —— 哪怕它很旧。

    用户没归档 = 还在他的列表里正常显示，产物随时可能要看。
    按时间盲删会把这类一起带走。
    """
    scan = make_scan(db_session, status=ScanStatus.COMPLETED.value)
    # 把结束时间推到很久以前，但**不打 archived_at**
    scan.finished_at = now_utc() - timedelta(days=9999)
    db_session.commit()
    path = _artifact_dir(scan.id)

    runner_mod.sweep_retired_artifacts(db_session, retention_days=90)

    assert path.is_dir(), "★ 没归档的扫描产物被清了（判据必须是「已归档」）"


def test_recently_archived_artifacts_are_kept(db_session: Session) -> None:
    """★ 刚归档、还没到保留期的要留着。"""
    scan = _archive(db_session, days_ago=10)
    path = _artifact_dir(scan.id)

    runner_mod.sweep_retired_artifacts(db_session, retention_days=90)

    assert path.is_dir(), "★ 归档才 10 天的产物被清了（保留期没生效）"


def test_uses_archived_at_not_finished_at(db_session: Session) -> None:
    """★★ 保留期从**归档时刻**算，不是从扫描结束时刻算。

    ## 为什么这条单独钉

    一条"跑完很久、今天刚被手工归档"的扫描：用 `finished_at` 算的话
    它**立刻**超期、产物马上被删 —— 而用户刚点了"归档"，
    期待的是"收起来"，不是"删掉"。用 `archived_at` 才会给它完整的保留期。
    """
    scan = make_scan(db_session, status=ScanStatus.COMPLETED.value)
    scan.finished_at = now_utc() - timedelta(days=9999)  # 结束很久了
    scan.archived_at = now_utc()  # 但刚刚才归档
    db_session.commit()
    path = _artifact_dir(scan.id)

    runner_mod.sweep_retired_artifacts(db_session, retention_days=90)

    assert path.is_dir(), (
        "★ 刚归档的扫描产物被删了 —— 说明保留期是按 `finished_at` 算的，"
        "而应当按 `archived_at`（用户刚点归档，不该立刻删）"
    )


def test_scans_in_the_trash_are_left_to_the_trash_rule(db_session: Session) -> None:
    """★★ 回收站里的扫描**不归本函数管** —— 由回收站保留期那条路闭环。

    两条路都管的话，"两个保留期谁说了算"就变成一个没人能回答的问题。
    而且 `purge_scan` 删记录时会顺带删产物，所以回收站那条路已经闭环了。
    """
    scan = _archive(db_session, days_ago=9999)
    scan.deleted_at = now_utc()  # 进了回收站
    db_session.commit()
    path = _artifact_dir(scan.id)

    runner_mod.sweep_retired_artifacts(db_session, retention_days=90)

    assert path.is_dir(), (
        "★ 回收站里的产物被本函数清了 —— 它应当由回收站保留期那条路负责"
    )


def test_uploads_staging_and_ctf2_dirs_are_never_touched(db_session: Session) -> None:
    """★★★ **附件 / 暂存 / CTF2 附件一个都不能碰**（最重要的一条）。

    它们的生命周期与扫描产物**不同**，而附件**删了就回不来**
    （用户得重新上传、重新下题）。这里用真实目录树验：
    即便有一条"已归档超期"的扫描，`_uploads/<同一个 id>/` 也必须原样留着。
    """
    scan = _archive(db_session, days_ago=100)
    _artifact_dir(scan.id)  # 产物（该被清）
    uploads = _uploads_dir(scan.id)  # ★ 附件（不该被清）
    staging = _staging_dir()
    ctf2 = _ctf2_attachment_dir()

    runner_mod.sweep_retired_artifacts(db_session, retention_days=90)

    assert uploads.is_dir(), "★★ 同一 id 的附件被产物清理删了（删了就回不来）"
    assert (uploads / "原件.zip").is_file(), "★★ 附件文件本身没了"
    _assert_intact(staging, ctf2)


def test_the_whole_sweep_retention_path_keeps_attachments(db_session: Session) -> None:
    """★★ 走**完整**的 `sweep_retention`（真实入口）时附件也不能被碰。

    单独测 `sweep_retired_artifacts` 只证明那个零件对；
    这条证明它**装上了**、而且没有与同一条路径上的其它清理互相踩。
    """
    scan = _archive(db_session, days_ago=100)
    artifact = _artifact_dir(scan.id)
    uploads = _uploads_dir(scan.id)
    staging = _staging_dir()
    ctf2 = _ctf2_attachment_dir()

    result = scans_service.sweep_retention(db_session)

    assert result["retiredArtifactsSwept"] >= 1, "归档产物没有被清（或没进返回值）"
    assert not artifact.exists(), "归档超期的产物该被清掉"
    assert uploads.is_dir(), "★★ 走完整路径时附件被误删"
    assert (uploads / "原件.zip").is_file()
    _assert_intact(staging, ctf2)


def test_sweep_retention_keeps_live_and_unarchived(db_session: Session) -> None:
    """★ 走完整路径时，正在跑的与没归档的也都留着。"""
    running = make_scan(db_session, status=ScanStatus.RUNNING.value)
    unarchived = make_scan(db_session, status=ScanStatus.COMPLETED.value)
    run_path = _artifact_dir(running.id)
    unarch_path = _artifact_dir(unarchived.id)

    scans_service.sweep_retention(db_session)

    assert run_path.is_dir(), "★ 正在跑的产物被清了"
    assert unarch_path.is_dir(), "★ 没归档的产物被清了"


# ══════════════════════════════════════════════════════════
# 三、幂等、计数、默认值
# ══════════════════════════════════════════════════════════


def test_the_sweep_is_idempotent(db_session: Session) -> None:
    """清第二遍什么都不做（目录已经不在了）。"""
    _archive(db_session, days_ago=100)
    runner_mod.sweep_retired_artifacts(db_session, retention_days=90)
    assert runner_mod.sweep_retired_artifacts(db_session, retention_days=90) == 0


def test_a_missing_directory_is_not_counted(db_session: Session) -> None:
    """目录已被手工清过 → 不计入、也不抛异常。"""
    _archive(db_session, days_ago=100)  # 故意不建目录

    assert runner_mod.sweep_retired_artifacts(db_session, retention_days=90) == 0


def test_the_default_retention_is_on_and_generous() -> None:
    """★ 默认值必须**开启**（不是 0）且比归档期大。

    * 开启：这条修的就是"默认状态下磁盘无限涨"，默认关闭等于没修；
    * 比归档期大：产物清理是**唯一不可逆**的一步，该留更久。
    """
    from app.settings.models import (
        DEFAULT_ARCHIVE_AFTER_DAYS,
        DEFAULT_TRASH_RETENTION_DAYS,
    )

    assert DEFAULT_ARTIFACT_RETENTION_DAYS > 0, "默认关闭等于没修这条缺口"
    assert DEFAULT_ARTIFACT_RETENTION_DAYS > DEFAULT_ARCHIVE_AFTER_DAYS
    assert DEFAULT_ARTIFACT_RETENTION_DAYS > DEFAULT_TRASH_RETENTION_DAYS


def test_the_config_row_carries_the_new_field(db_session: Session) -> None:
    """★ 单例行读出来要带 `artifact_retention_days`（新加的那一列）。

    这条同时是**迁移接上了**的闸：列不存在时 `get_retention_config`
    会在读那一行时抛错。
    """
    config = scans_service.get_retention_config(db_session)
    assert config.artifact_retention_days == DEFAULT_ARTIFACT_RETENTION_DAYS


def test_the_api_exposes_and_accepts_the_new_field(ready_client) -> None:  # noqa: ANN001
    """★ 端到端：`GET` 要给出这个字段，`PUT` 要能改它。

    ⚠️ 这条不是多余的：后端 schema 加了字段而**前端忘了传**的话，
    `PUT` 会 422（那是刻意的 fail-closed，见 `ScanRetentionIn` 的说明）。
    这条把两个方向都钉住。
    """
    got = ready_client.get("/api/v1/settings/scan-retention")
    assert got.status_code == 200, got.text
    assert "artifactRetentionDays" in got.json()

    put = ready_client.put(
        "/api/v1/settings/scan-retention",
        json={
            "archiveAfterDays": 30,
            "trashRetentionDays": 30,
            "artifactRetentionDays": 45,
        },
    )
    assert put.status_code == 200, put.text
    assert put.json()["artifactRetentionDays"] == 45


def test_the_api_keeps_the_value_when_the_field_is_omitted(ready_client) -> None:  # noqa: ANN001
    """★★ 旧客户端不传这个字段 → **保持现值**，不是报错、也不是改回默认。

    ## 为什么这是刻意的（而不是"漏了必填校验"）

    实测过那个部署窗口**真实存在**：线上 `dist/`（2026-10-16 04:49 构建）
    里没有这个字段，而它是 bind mount 的构建产物 —— 后端先部署、
    前端后重建的那段时间里，用户一点"保存"就会打到这个端点。

    两种处理方式的风险对比：

    | 做法 | 后果 |
    | --- | --- |
    | 必填（报错） | 那个窗口里**保存直接失败**，用户看到一个莫名其妙的错误 |
    | **不传 = 保持原值** | ✅ 一切照常，旧前端不需要知道这个字段 |
    | 不传 = 用默认值 | 用户改过的产物保留期被**悄悄改回默认**（静默配置改写） |

    最后一种最坏：它没有任何症状。所以这里选中间那条 ——
    旧前端"没传"**不等于**"用户想改它"。
    """
    # 先设一个**非默认**的值
    first = ready_client.put(
        "/api/v1/settings/scan-retention",
        json={
            "archiveAfterDays": 30,
            "trashRetentionDays": 30,
            "artifactRetentionDays": 45,
        },
    )
    assert first.status_code == 200, first.text
    assert first.json()["artifactRetentionDays"] == 45

    # 再模拟"旧前端"：只发那两个字段
    second = ready_client.put(
        "/api/v1/settings/scan-retention",
        json={"archiveAfterDays": 10, "trashRetentionDays": 20},
    )
    assert second.status_code == 200, (
        "旧前端（不传 artifactRetentionDays）被拒了 —— 那会让部署窗口里"
        f"用户无法保存保留规则。响应：{second.text}"
    )
    body = second.json()
    assert body["artifactRetentionDays"] == 45, (
        "旧前端保存一次就把产物保留期改掉了 —— 用户没碰过这个值，"
        "它必须保持原样（这比报错更安全，也比改回默认更安全）"
    )
    # 另外两个字段照常被改（不能因为新字段而整条不生效）
    assert body["archiveAfterDays"] == 10
    assert body["trashRetentionDays"] == 20


def test_sweep_retention_reports_the_retired_count(db_session: Session) -> None:
    """`sweep_retention` 的返回值里要有这一项的计数。

    没有它，运维在启动日志里看不到"归档产物清了多少" ——
    而"清理到底有没有生效"是这类后台任务最该能回答的问题。

    ⚠️ 必须真的建出目录：`_archive()` 只建数据库记录，
    而"没有目录可删"时计数是 0（那是正确行为，见
    `test_a_missing_directory_is_not_counted`）。
    """
    scan = _archive(db_session, days_ago=100)
    _artifact_dir(scan.id)

    result = scans_service.sweep_retention(db_session)

    assert "retiredArtifactsSwept" in result
    assert result["retiredArtifactsSwept"] >= 1


def test_the_cleanup_can_be_disabled_from_the_config(db_session: Session) -> None:
    """★ 从配置把它关掉（0）之后，`sweep_retention` 不再清产物。"""
    scan = _archive(db_session, days_ago=100)
    path = _artifact_dir(scan.id)
    config = scans_service.get_retention_config(db_session)
    config.artifact_retention_days = 0
    db_session.commit()

    result = scans_service.sweep_retention(db_session)

    assert path.is_dir(), "★ 配置里关了（0）却仍然清了产物"
    assert result["retiredArtifactsSwept"] == 0


# ══════════════════════════════════════════════════════════
# 四、路径安全：只按 id 拼，不遍历
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "bad_id",
    [
        "../escape",
        "scan_../escape",
        "other_prefix",
        "",
        "scan_01ABC/../../etc",
    ],
)
def test_a_malformed_scan_id_is_refused(bad_id: str) -> None:
    """★ 形状不对的 id 一律拒绝（复用 `remove_scan_artifacts` 的校验）。"""
    assert runner_mod.remove_scan_artifacts(bad_id) is False


def test_it_does_not_walk_the_root(db_session: Session) -> None:
    """★★ 它按 id 拼路径，**不遍历** `scan_root`。

    这条钉的是一个"实现选择"而不是行为：一个改成"遍历 scan_root、
    按条件删目录"的实现，很容易在某次条件调整后连 `_uploads/` 一起删。
    按 id 拼路径从构造上就碰不到下划线开头的兄弟目录。

    ⚠️ 用一个**不存在的** id 验：如果实现是遍历式的，
    它会把 `_uploads` 那类目录也纳入候选 —— 而本函数根本不该看它们。
    """
    uploads = _uploads_dir("scan_01SHOULDNEVERBESEENAA")
    runner_mod.sweep_retired_artifacts(db_session, retention_days=1)
    assert uploads.is_dir(), "★ 遍历式的实现碰到了附件目录"
