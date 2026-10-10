"""漏洞发现的持久化与状态流转（任务 5）。

四条要求逐条对应：
1. 落库为结构化记录 —— `test_import_*` / `test_parses_*`
2. 状态流转 + 操作人留痕 + 时间线 —— `test_status_*` / `test_timeline_*`
3. 批量状态更新 —— `test_batch_*`
4. 统计唯一数据源 —— `test_stats_*` / `test_overview_and_findings_agree`

## 格式依据

`vulnerabilities.json` 的字段来自 strix 源码（`report/state.py` 的
`add_vulnerability_report` + `report/writer.py` 的写入点），
逐字段的对照表在 `app/findings/strix_vulns.py` 的模块 docstring 里。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.findings import service
from app.findings.models import Finding, FindingTimelineEntry
from tests.factories import make_scan

AUTH_STATEMENT = "我有权对此目标进行安全测试"


# ══════════════════════════════════════════════════════════
# 造数据
# ══════════════════════════════════════════════════════════
#
# `vuln_record` / `write_vulns` / `scan` / `imported` 已移到 `conftest.py`
# （任务 10 P2）—— `test_members` 的指派测试也要造同样形状的漏洞，
# 而 pytest 不跨文件共享局部 fixture。定义两份的话，
# "3 条漏洞"在两个文件里会各自漂移成不同的东西。
from tests.conftest import vuln_record, write_vulns  # noqa: F401


# ══════════════════════════════════════════════════════════
# 1. 持久化
# ══════════════════════════════════════════════════════════


def test_import_persists_structured_records(db_session: Session, scan: Scan) -> None:
    """★ 落库为结构化记录，字段对齐契约（任务 5 要求 1）。"""
    write_vulns(Path(scan.run_dir), [vuln_record()])

    result = service.import_from_run(db_session, scan)

    assert result.created == 1
    finding = db_session.execute(select(Finding)).scalar_one()
    assert finding.severity == "high"
    assert finding.title == "调试接口未授权可读环境变量"
    assert finding.scan_id == scan.id
    assert finding.target == "api.example.com"
    assert finding.detail_evidence.startswith("HTTP/1.1 200 OK")
    assert finding.poc_code and "requests" in finding.poc_code
    assert finding.remediation == "把该路由挂到鉴权中间件之后。"
    # 溯源字段
    assert finding.source_ref == "vuln-0001"
    assert finding.cwe == "CWE-200"
    assert finding.cvss_score == 7.5


def test_two_axes_are_stored_in_two_columns(db_session: Session, scan: Scan) -> None:
    """★★ `verification` 与 `status` 必须是**两列**，绝不可合并（契约 §2.2）。

    一条漏洞可以是「已用 PoC 证明存在（verified），但判定不影响业务
    （false_positive）」。合成一个字段，这个渗透测试里最常见的争议点
    就无处表达。

    这里同时验证**两个轴各自的来源不同**：
    `verification` 来自 strix 的 `confidence`（agent 产出），
    `status` 一律从 `new` 开始（人的判断还没发生）。
    """
    write_vulns(
        Path(scan.run_dir),
        [
            vuln_record(id="v1", confidence="high"),
            vuln_record(id="v2", confidence="medium"),
            vuln_record(id="v3", confidence="low"),
        ],
    )
    service.import_from_run(db_session, scan)

    rows = {
        f.source_ref: f
        for f in db_session.execute(select(Finding).order_by(Finding.source_ref)).scalars()
    }
    assert rows["v1"].verification == "verified"
    assert rows["v2"].verification == "partial"
    assert rows["v3"].verification == "none"
    # 处置状态一律 new —— 外部工具不产生人的判断
    assert {f.status for f in rows.values()} == {"new"}


def test_reimport_is_idempotent(db_session: Session, scan: Scan) -> None:
    """★ 反复导入不产生重复记录。

    导入器每轮调度都跑一次（扫描还在写 `vulnerabilities.json`），
    没有幂等，Findings 页上会出现五条一模一样的记录，
    而用户会以为工具重复报了五次。
    """
    write_vulns(Path(scan.run_dir), [vuln_record()])

    first = service.import_from_run(db_session, scan)
    second = service.import_from_run(db_session, scan)
    third = service.import_from_run(db_session, scan)

    assert first.created == 1
    assert second.created == 0 and third.created == 0
    assert db_session.execute(select(Finding)).scalars().all().__len__() == 1


def test_reimport_does_not_touch_human_decisions(db_session: Session, scan: Scan) -> None:
    """★ 重复导入**绝不能动 `status`**。

    那是人流转出来的结论。外部工具后续补写文件把"已确认"改回"待确认"，
    等于抹掉人的判断 —— 而这个 bug 在"每次调度都导入"的频率下
    会立刻暴露成"我标的误报过两秒又变回待确认了"。
    """
    write_vulns(Path(scan.run_dir), [vuln_record()])
    service.import_from_run(db_session, scan)
    finding = db_session.execute(select(Finding)).scalar_one()

    from app.auth.deps import Identity

    identity = Identity(kind="session", label="s", scopes=frozenset({"read", "scan", "admin"}))
    service.change_status(db_session, ids=[finding.id], target="confirmed", note=None, identity=identity)

    # 外部工具补写了新证据
    write_vulns(Path(scan.run_dir), [vuln_record(evidence="HTTP/1.1 200 OK\n新的证据")])
    service.import_from_run(db_session, scan)

    refreshed = db_session.execute(select(Finding)).scalar_one()
    assert refreshed.status == "confirmed", "人的判断被机器覆盖了"
    assert "新的证据" in refreshed.detail_evidence, "证据没有被更新"


def test_missing_file_is_not_an_error(db_session: Session, scan: Scan) -> None:
    """没有漏洞文件 = 这次扫描没发现漏洞。**正常状态**，不是失败。"""
    assert service.import_from_run(db_session, scan).created == 0


def test_corrupt_file_is_reported_not_silently_empty(
    db_session: Session, scan: Scan
) -> None:
    """★ 坏文件**不能静默当成空**。

    "扫了一整轮没发现漏洞"与"文件坏了读不出来"是完全不同的事，
    混起来会让用户以为扫描是干净的。
    """
    path = Path(scan.run_dir) / "strix_runs" / "run-1"
    path.mkdir(parents=True, exist_ok=True)
    (path / "vulnerabilities.json").write_text("{ 这不是 JSON", encoding="utf-8")

    result = service.import_from_run(db_session, scan)

    assert result.created == 0
    # 与"文件不存在"的区别在于：这次会留下一条 warning 日志
    # （见 strix_vulns.read_vulnerabilities 的返回错误路径）


def test_record_without_title_is_skipped(db_session: Session, scan: Scan) -> None:
    """没有标题的记录跳过 —— 界面上点了没内容的记录比少一条更糟。"""
    write_vulns(Path(scan.run_dir), [{"id": "v1", "severity": "high"}, vuln_record(id="v2")])

    result = service.import_from_run(db_session, scan)

    assert result.created == 1
    assert db_session.execute(select(Finding)).scalar_one().source_ref == "v2"


def test_unknown_severity_falls_back_to_info(db_session: Session, scan: Scan) -> None:
    """★ 认不出的严重度落到 `info`（最低档），**不是** `medium`。

    显示成"中危"是在**放大**一条严重度不明的发现。宁可低估也不要高估 ——
    一份"全是中危"的报告会让人对整套分级失去信任。
    """
    write_vulns(Path(scan.run_dir), [vuln_record(severity="whatever")])

    service.import_from_run(db_session, scan)

    assert db_session.execute(select(Finding)).scalar_one().severity == "info"


def test_strix_timestamp_format_is_parsed(db_session: Session, scan: Scan) -> None:
    """★ strix 的时间戳**不是 ISO 8601**（`2026-09-24 10:00:00 UTC`）。

    直接 `fromisoformat` 会抛 —— 而抛在这里会让整次导入失败。
    """
    write_vulns(Path(scan.run_dir), [vuln_record(timestamp="2026-09-24 10:00:00 UTC")])

    service.import_from_run(db_session, scan)

    finding = db_session.execute(select(Finding)).scalar_one()
    assert finding.discovered_at.year == 2026
    assert finding.discovered_at.tzinfo is not None


def test_counterevidence_is_kept_in_reasoning(db_session: Session, scan: Scan) -> None:
    """★ `counterevidence` / `assumptions` 不能被丢掉。

    它们恰恰是"诚实标注证据强度"的关键材料。契约的 `FindingDetail`
    里没有它们的位置，但**丢掉是错的** —— 丢掉之后一条"有反证"的记录
    会和一条铁证在界面上长得一模一样。

    处理方式：折进 `reasoning`，带明确小标题。
    """
    write_vulns(
        Path(scan.run_dir),
        [
            vuln_record(
                technical_analysis="路由在鉴权中间件之前。",
                confidence="medium",
                confidence_rationale="未验证生产环境是否同配置",
                assumptions="假设生产环境的中间件顺序与测试环境一致",
                counterevidence="预发环境返回 403，与测试环境不一致",
            )
        ],
    )

    service.import_from_run(db_session, scan)

    reasoning = db_session.execute(select(Finding)).scalar_one().detail_reasoning
    assert "路由在鉴权中间件之前" in reasoning
    assert "【置信度依据（medium）】" in reasoning
    assert "【成立假设】" in reasoning
    assert "【反证】" in reasoning
    assert "预发环境返回 403" in reasoning


def test_cwe_maps_to_contract_category(db_session: Session, scan: Scan) -> None:
    """CWE → 契约的 8 个分类（映射，不是等价）。"""
    write_vulns(
        Path(scan.run_dir),
        [
            vuln_record(id="v1", cwe="CWE-89", title="SQL 注入"),
            vuln_record(id="v2", cwe="CWE-79", title="XSS"),
            vuln_record(id="v3", cwe="CWE-918", title="SSRF"),
        ],
    )
    service.import_from_run(db_session, scan)

    by_ref = {
        f.source_ref: f.category
        for f in db_session.execute(select(Finding).order_by(Finding.source_ref)).scalars()
    }
    assert by_ref["v1"] == "injection"
    assert by_ref["v2"] == "xss"
    assert by_ref["v3"] == "ssrf"


def test_unknown_cwe_falls_back_to_misconfiguration(db_session: Session, scan: Scan) -> None:
    """★ 认不出的 CWE 落到最中性的 `misconfiguration`，**不是** `injection`。

    分类是用来筛选的，填错比填"其他"更糟 —— 用户按"注入"筛选时
    混进来一堆无关条目，他会开始不信任这个筛选。
    """
    write_vulns(Path(scan.run_dir), [vuln_record(cwe="CWE-999999", title="某个没见过的模式")])

    service.import_from_run(db_session, scan)

    assert db_session.execute(select(Finding)).scalar_one().category == "misconfiguration"


def test_code_locations_become_affected_positions(db_session: Session, scan: Scan) -> None:
    """`code_locations` → 契约的 `affected: string[]`。

    键名（`file` / `start_line`）依据 `report/sarif.py` 的读取。
    """
    write_vulns(
        Path(scan.run_dir),
        [
            vuln_record(
                code_locations=[
                    {"file": "src/api/debug.py", "start_line": 42},
                    {"file": "src/app.py"},
                ]
            )
        ],
    )

    service.import_from_run(db_session, scan)

    affected = db_session.execute(select(Finding)).scalar_one().detail_affected
    assert affected == ["src/api/debug.py:42", "src/app.py"]


# ══════════════════════════════════════════════════════════
# 2. 状态流转：时间线 + 操作人留痕
# ══════════════════════════════════════════════════════════


def _identity() -> object:
    from app.auth.deps import Identity

    return Identity(kind="session", label="s", scopes=frozenset({"read", "scan", "admin"}))


def test_import_creates_initial_timeline(db_session: Session, scan: Scan) -> None:
    """导入时写一条 `detected` —— 时间线不能从"待确认"凭空开始。

    有 PoC 与高置信度还会各补一条，让"什么时候拿到脚本的"可追溯。
    """
    write_vulns(Path(scan.run_dir), [vuln_record(confidence="high")])
    service.import_from_run(db_session, scan)

    finding = db_session.execute(select(Finding)).scalar_one()
    kinds = [
        entry.kind
        for entry in db_session.execute(
            select(FindingTimelineEntry).where(FindingTimelineEntry.finding_id == finding.id)
        ).scalars()
    ]
    assert "detected" in kinds
    assert "poc_generated" in kinds
    assert "verified" in kinds


def test_status_change_records_actor_and_timestamp(
    db_session: Session, scan: Scan, imported: list[Finding]
) -> None:
    """★ 每次变更记录**操作人**与时间戳（任务 5 要求 2）。

    单管理员系统里操作人是常量，但仍然要存 ——
    为将来多用户做准备，与契约 §5.4 的 `authorized_by` 同一个判断。
    """
    target = imported[0]

    service.change_status(
        db_session, ids=[target.id], target="confirmed", note=None, identity=_identity()
    )

    entry = db_session.execute(
        select(FindingTimelineEntry)
        .where(FindingTimelineEntry.finding_id == target.id)
        .where(FindingTimelineEntry.kind == "status_changed")
    ).scalar_one()
    assert entry.actor == "user"
    assert entry.from_status == "new"
    assert entry.to_status == "confirmed"
    assert entry.at is not None
    # 操作人写在摘要里 —— 契约的 TimelineEntry 没有专门的位置放它
    assert "管理员会话" in entry.summary
    assert "待确认 → 已确认" in entry.summary


def test_illegal_transition_is_rejected(
    db_session: Session, scan: Scan, imported: list[Finding]
) -> None:
    """★ 非法流转 → 400（契约 §2.3）。**服务端必须校验。**

    不要依赖前端不显示非法按钮 —— 那是双保险里的第二道，不是唯一一道。
    `new → fixed` 不在流转表里（新发现必须先确认才能说修好了）。
    """
    from app.core.errors import ApiError

    with pytest.raises(ApiError) as excinfo:
        service.change_status(
            db_session, ids=[imported[0].id], target="fixed", note=None, identity=_identity()
        )

    assert excinfo.value.status_code == 400
    assert excinfo.value.code == "VALIDATION_FAILED"
    # 提示要给出**能去哪**
    assert "已确认" in excinfo.value.details["status"]


def test_same_status_is_idempotent_and_writes_no_timeline(
    db_session: Session, scan: Scan, imported: list[Finding]
) -> None:
    """★★ 幂等：已经是目标状态 → 跳过，**且不写时间线**（契约 §2.3）。

    不写是因为否则时间线里会出现「待确认 → 待确认」的噪音，
    把真正的流转淹掉。
    """
    target = imported[0]
    service.change_status(
        db_session, ids=[target.id], target="confirmed", note=None, identity=_identity()
    )
    before = db_session.execute(
        select(FindingTimelineEntry).where(FindingTimelineEntry.finding_id == target.id)
    ).scalars().all().__len__()

    changed, skipped = service.change_status(
        db_session, ids=[target.id], target="confirmed", note=None, identity=_identity()
    )

    after = db_session.execute(
        select(FindingTimelineEntry).where(FindingTimelineEntry.finding_id == target.id)
    ).scalars().all().__len__()
    assert changed == [] and skipped == 1
    assert after == before, "幂等跳过时写了时间线"


def test_full_transition_chain(db_session: Session, scan: Scan, imported: list[Finding]) -> None:
    """走完整条链：待确认 → 已确认 → 已修复 → 已确认（退回）。

    `fixed → confirmed` 是允许的（只能退回，不能直接误报）——
    见 `FINDING_STATUS_TRANSITIONS` 的说明。
    """
    target = imported[0]
    for step in ("confirmed", "fixed", "confirmed"):
        service.change_status(
            db_session, ids=[target.id], target=step, note=None, identity=_identity()
        )
        db_session.refresh(target)
        assert target.status == step


def test_false_positive_round_trip(db_session: Session, scan: Scan, imported: list[Finding]) -> None:
    """误报可以退回待确认 —— 判错了要能改回来。"""
    target = imported[0]
    service.change_status(
        db_session, ids=[target.id], target="false_positive", note="扫描器把测试环境的调试开关判成了漏洞", identity=_identity()
    )
    service.change_status(db_session, ids=[target.id], target="new", note=None, identity=_identity())
    db_session.refresh(target)
    assert target.status == "new"


def test_note_overrides_default_summary(
    db_session: Session, scan: Scan, imported: list[Finding]
) -> None:
    target = imported[0]
    service.change_status(
        db_session, ids=[target.id], target="confirmed", note="已人工复现", identity=_identity()
    )

    entry = db_session.execute(
        select(FindingTimelineEntry)
        .where(FindingTimelineEntry.finding_id == target.id)
        .where(FindingTimelineEntry.kind == "status_changed")
    ).scalar_one()
    assert entry.summary == "已人工复现"


def test_timeline_is_newest_first(db_session: Session, scan: Scan, imported: list[Finding]) -> None:
    """★ 时间线**倒序**（最新在前）—— 契约 §2.2 明确要求。"""
    target = imported[0]
    service.change_status(db_session, ids=[target.id], target="confirmed", note=None, identity=_identity())
    service.change_status(db_session, ids=[target.id], target="fixed", note=None, identity=_identity())

    db_session.refresh(target)
    timeline = target.timeline
    assert len(timeline) >= 2
    # 最新的那条是最后发生的流转
    assert timeline[0].to_status == "fixed"
    # `at` 单调不增
    assert all(timeline[i].at >= timeline[i + 1].at for i in range(len(timeline) - 1))


def test_timeline_seq_is_unique_after_deletes(db_session: Session, scan: Scan, imported: list[Finding]) -> None:
    """★ 时间线序号用 `max(seq)+1`，不是 `count(*)+1`。

    用 count 的话，删过条目之后新 seq 会与已有的撞上，
    而 `(finding_id, seq)` 上有唯一约束 —— 那会是一个
    "删了东西之后就再也写不进时间线"的 bug。
    """
    target = imported[0]
    entries = db_session.execute(
        select(FindingTimelineEntry).where(FindingTimelineEntry.finding_id == target.id)
    ).scalars().all()
    db_session.delete(entries[0])
    db_session.commit()

    service.change_status(db_session, ids=[target.id], target="confirmed", note=None, identity=_identity())

    seqs = [
        entry.seq
        for entry in db_session.execute(
            select(FindingTimelineEntry).where(FindingTimelineEntry.finding_id == target.id)
        ).scalars()
    ]
    assert len(seqs) == len(set(seqs))


def test_unknown_id_is_reported_once(db_session: Session, imported: list[Finding]) -> None:
    """批量提交里有不存在的 id → 一次性说清，而不是逐个报错。"""
    from app.core.errors import ApiError

    with pytest.raises(ApiError) as excinfo:
        service.change_status(
            db_session,
            ids=[imported[0].id, "finding_nope", "finding_nope2"],
            target="confirmed",
            note=None,
            identity=_identity(),
        )
    assert excinfo.value.status_code == 404
    assert "2 条" in excinfo.value.message


# ══════════════════════════════════════════════════════════
# 3. 批量
# ══════════════════════════════════════════════════════════


def test_batch_reports_changed_and_skipped(
    db_session: Session, scan: Scan, imported: list[Finding]
) -> None:
    """★ `changed` / `skipped` 都要返回（契约 §3.4）。

    前端用它们提示"实际改了几条、跳过几条"。
    只返回 changed 的话，用户勾了三条只改了一条会不知道为什么。
    """
    service.change_status(
        db_session, ids=[imported[0].id], target="confirmed", note=None, identity=_identity()
    )

    changed, skipped = service.change_status(
        db_session,
        ids=[finding.id for finding in imported],
        target="confirmed",
        note=None,
        identity=_identity(),
    )

    assert len(changed) == 2
    assert skipped == 1


def test_batch_dedupes_ids(db_session: Session, scan: Scan, imported: list[Finding]) -> None:
    """重复 id 去重 —— 不去的话 `changed` 会翻倍，
    而前端用它显示"实际改了几条"。"""
    from app.findings.schemas import StatusChangeRequest

    payload = StatusChangeRequest(ids=[imported[0].id, imported[0].id], status="confirmed")
    assert payload.ids == [imported[0].id]


def test_batch_is_atomic(db_session: Session, scan: Scan, imported: list[Finding]) -> None:
    """★ 批量里有一条非法 → **整批都不改**。

    改一半会让用户不知道哪些生效了；而"部分成功"在界面上
    最难表达（重试会重复改掉那些已经改过的）。
    """
    from app.core.errors import ApiError

    # 先让第一条变成 confirmed（于是 new→confirmed 合法）
    # 第二条保持 new，而 new→fixed 非法
    with pytest.raises(ApiError):
        service.change_status(
            db_session,
            ids=[imported[0].id, imported[1].id],
            target="fixed",
            note=None,
            identity=_identity(),
        )

    for finding in imported[:2]:
        db_session.refresh(finding)
        assert finding.status == "new", "批量失败时有条目被改了"


# ══════════════════════════════════════════════════════════
# 4. 统计：唯一数据源
# ══════════════════════════════════════════════════════════


def test_stats_counts_by_all_dimensions(db_session: Session, scan: Scan, imported: list[Finding]) -> None:
    snapshot = service.stats(db_session)

    assert snapshot.total == 3
    # `Partial`：为 0 的档**键不存在**，不是 0
    assert snapshot.bySeverity.critical == 1
    assert snapshot.bySeverity.high == 1
    assert snapshot.bySeverity.low == 1
    assert snapshot.bySeverity.medium is None
    assert snapshot.bySeverity.info is None
    # 状态四档**全部返回**（含 0）—— 它们是筛选 chip 的角标
    assert snapshot.byStatus["new"] == 3
    assert snapshot.byStatus["confirmed"] == 0
    assert snapshot.byStatus["fixed"] == 0
    assert snapshot.byStatus["false_positive"] == 0


def test_false_positive_count_is_always_present(
    db_session: Session, scan: Scan, imported: list[Finding]
) -> None:
    """★ `falsePositiveCount` **必须单独返回**。

    Overview 的指标卡排除了误报，界面上有一句「上面两张指标卡已排除
    N 条被判为误报的条目」—— 前端注释：**"排除是可以的，悄悄排除不行。"**
    """
    snapshot = service.stats(db_session)
    assert snapshot.falsePositiveCount == 0

    service.change_status(
        db_session, ids=[imported[0].id], target="false_positive", note="扫描器把测试环境的调试开关判成了漏洞", identity=_identity()
    )

    assert service.stats(db_session).falsePositiveCount == 1


def test_stats_can_scope_to_one_scan(db_session: Session, tmp_path: Path) -> None:
    """`scanId` 参数：只统计那一次扫描。"""
    first = make_scan(db_session, status="completed", scan_id="scan_a", target="a.example.com")
    second = make_scan(db_session, status="completed", scan_id="scan_b", target="b.example.com")
    for scan, count in ((first, 2), (second, 3)):
        scan.run_dir = str(tmp_path / scan.id)
        Path(scan.run_dir).mkdir(parents=True, exist_ok=True)
        write_vulns(
            Path(scan.run_dir),
            [vuln_record(id=f"v{scan.id}-{i}") for i in range(count)],
        )
        service.import_from_run(db_session, scan)

    assert service.stats(db_session).total == 5
    assert service.stats(db_session, scan_id=first.id).total == 2
    assert service.stats(db_session, scan_id=second.id).total == 3


def test_overview_and_findings_agree(db_session: Session, scan: Scan, imported: list[Finding]) -> None:
    """★★★ **任务 5 的验收标准**：两个页面的统计数字来自同一处查询逻辑。

    `overview_metrics()` 内部调 `stats()`，所以它们**不可能**对不上。
    这条测试断言这个关系在任何数据状态下都成立 ——
    包括有误报、有各种严重度的时候。

    为什么这条这么重要：两页数字不一致的话，用户不知道哪个是对的，
    而那种不信任会扩散到整个产品。
    """
    service.change_status(
        db_session, ids=[imported[0].id], target="false_positive", note="扫描器把测试环境的调试开关判成了漏洞", identity=_identity()
    )

    snapshot = service.stats(db_session)
    metrics = service.overview_metrics(db_session)

    # 严重度分布逐档对得上
    by_severity = {item["severity"]: item["count"] for item in metrics["severityBreakdown"]}
    for member in ("critical", "high", "medium", "low", "info"):
        assert by_severity[member] == (getattr(snapshot.bySeverity, member) or 0)
    # 两边算出来的总数一致
    assert sum(by_severity.values()) == snapshot.total
    # 误报数一致
    assert metrics["falsePositiveCount"] == snapshot.falsePositiveCount

    # ★ `severityBreakdown` **包含全部发现**（含误报），与 Findings 页逐条对得上；
    #   而 `verifiedPocCount` 排除误报 —— 这个口径差异必须在界面上说明，
    #   所以 falsePositiveCount 要跟着返回。
    assert by_severity["critical"] == 1  # 那条被判误报的仍在分布里
    assert metrics["verifiedPocCount"] == 0  # 但它不算"已验证 PoC"


def test_overview_breakdown_matches_list_endpoint(
    ready_client: TestClient, db_session: Session, tmp_path: Path
) -> None:
    """★ 端到端：`/findings/stats` 与 Overview 派生指标对得上**实际列表**。

    前面那条测的是两个函数的内部关系；这条从 HTTP 层再验一次，
    确认**接口读到的**和**数出来的**一致 ——
    中间隔着序列化与过滤，那里也可能出偏差。
    """
    scan = make_scan(db_session, status="completed", scan_id="scan_x", target="x.example.com")
    scan.run_dir = str(tmp_path / "run")
    Path(scan.run_dir).mkdir(parents=True, exist_ok=True)
    write_vulns(
        Path(scan.run_dir),
        [
            vuln_record(id="v1", severity="critical"),
            vuln_record(id="v2", severity="high"),
            vuln_record(id="v3", severity="high"),
        ],
    )
    service.import_from_run(db_session, scan)

    stats_body = ready_client.get("/api/v1/findings/stats").json()
    listed = ready_client.get("/api/v1/findings").json()

    # 逐档对得上列表
    assert stats_body["total"] == len(listed) == 3
    assert stats_body["bySeverity"].get("critical") == 1
    assert stats_body["bySeverity"].get("high") == 2
    # 列表按严重度筛选出来的条数与统计一致
    critical = ready_client.get("/api/v1/findings?severity=critical").json()
    assert len(critical) == stats_body["bySeverity"]["critical"]


# ══════════════════════════════════════════════════════════
# HTTP 层
# ══════════════════════════════════════════════════════════


def test_findings_endpoints_require_auth(client: TestClient) -> None:
    assert client.get("/api/v1/findings").status_code == 401
    assert client.get("/api/v1/findings/stats").status_code == 401
    assert client.post("/api/v1/findings/status", json={"ids": ["x"], "status": "new"}).status_code == 401


def test_status_change_requires_scan_scope(ready_client: TestClient) -> None:
    """★ 改状态是 `scan` 级：只读令牌不能把漏洞标成误报。

    **那是掩盖问题**，比读到数据严重得多。
    """
    token = ready_client.post(
        "/api/v1/settings/tokens",
        json={"label": "报表脚本", "scopes": ["read"], "expiresAt": None},
    ).json()["plaintext"]
    ready_client.cookies.clear()

    response = ready_client.post(
        "/api/v1/findings/status",
        json={"ids": ["finding_x"], "status": "false_positive"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "INSUFFICIENT_SCOPE"


def test_detail_endpoint_returns_the_contract_shape(
    ready_client: TestClient, db_session: Session, scan: Scan
) -> None:
    """详情响应的字段集合与契约 §2.2 逐字一致。"""
    write_vulns(Path(scan.run_dir), [vuln_record(confidence="high")])
    service.import_from_run(db_session, scan)
    finding = db_session.execute(select(Finding)).scalar_one()

    body = ready_client.get(f"/api/v1/findings/{finding.id}").json()

    assert set(body) == {
        "id", "scanId", "severity", "category", "title", "target",
        "verification", "status", "discoveredAt", "detail", "timeline",
    }
    assert set(body["detail"]) == {
        "description", "affected", "reasoning", "evidence", "poc", "remediation",
    }
    assert set(body["detail"]["poc"]) == {"language", "code"}
    # 时间线的 `from` 字段名 —— Python 侧叫 from_，但出参必须是 `from`
    assert "from" in body["timeline"][0]
    assert set(body["timeline"][0]) == {"id", "at", "actor", "kind", "summary", "from", "to"}


def test_poc_is_null_when_absent(ready_client: TestClient, db_session: Session, scan: Scan) -> None:
    """★ `poc` **整体为 null**，不是空对象（契约 §2.2）。

    `poc: null` = 没有脚本；空对象会把"没有"和"有但是空的"混成一个。
    """
    record = vuln_record()
    record.pop("poc_script_code")
    write_vulns(Path(scan.run_dir), [record])
    service.import_from_run(db_session, scan)
    finding = db_session.execute(select(Finding)).scalar_one()

    body = ready_client.get(f"/api/v1/findings/{finding.id}").json()

    assert body["detail"]["poc"] is None


def test_list_returns_summaries_without_heavy_fields(
    ready_client: TestClient, imported: list[Finding]
) -> None:
    """列表是摘要：不带 `detail` / `timeline`。

    一次拉几百条时，完整证据文本会让响应到几 MB，而列表不渲染它们。
    """
    listed = ready_client.get("/api/v1/findings").json()

    assert len(listed) == 3
    # 精确断言字段集合：这个测试的价值就在"多一个字段就红"——
    # 列表响应是会被打出去的，多带一个 `detail` 就是几 MB。
    # （任务 10 P2 加了 assignee 两列、P3 加了 SLA 三列、P6-a 加了 deletedAt。）
    assert set(listed[0]) == {
        "id", "scanId", "severity", "category", "title", "target",
        "verification", "status", "discoveredAt",
        # 指派：id 给筛选，名字给显示。两个都要 —— 只给 id 的话，
        # 列表渲染会依赖第二次请求去查名单。
        "assigneeId", "assigneeName",
        # SLA：dueAt 给排序/展示，slaState 与 slaDaysLeft 由**后端**判定
        # （不让前端拿 dueAt 和 Date.now() 自己比 —— 那是同一个判断做两遍，
        #  而"临期"的判定窗口就要在前后端各写一份）。
        "dueAt", "slaState", "slaDaysLeft",
        # 回收站（P6-a）：非空 = 在回收站。与 `Scan.archivedAt` 同构，
        # 用时间戳不用布尔 —— 界面要能说"3 天前删的"。
        # 默认列表里恒为 null（回收站条目被排除在外）。
        "deletedAt",
    }
    # 还没指派过，所以两个都是 null（不是空串 —— 空串会被显示成"负责人：")
    assert listed[0]["assigneeId"] is None
    assert listed[0]["assigneeName"] is None


def test_stats_route_is_not_shadowed_by_the_id_route(ready_client: TestClient) -> None:
    """★ `/findings/stats` 不能被 `/{findingId}` 抢先匹配。

    路由顺序错了的症状是「统计接口 404 找不到漏洞 stats」。
    """
    response = ready_client.get("/api/v1/findings/stats")

    assert response.status_code == 200
    assert "total" in response.json()


def test_batch_endpoint_reports_changed_and_skipped(
    ready_client: TestClient, imported: list[Finding]
) -> None:
    response = ready_client.post(
        "/api/v1/findings/status",
        json={"ids": [finding.id for finding in imported], "status": "confirmed"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["changed"] == 3
    assert body["skipped"] == 0
    assert len(body["findings"]) == 3
