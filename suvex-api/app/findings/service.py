"""`findings` 领域的业务逻辑：导入、查询、状态流转、统计。

## ★ 统计只有一处实现

任务 5 的要求 4 与验收标准都盯着同一件事：

> 统计接口：按严重级别/状态的数量分布，这个接口要**同时服务 Overview 页和
> Findings 页，保证两处数字一致**（之前设计阶段就强调过这点，
> 后端实现上更要做到唯一数据源）。
>
> 验收：Overview 和 Findings 两个页面的统计数字**来自同一个查询逻辑**，不会对不上。

所以 `stats()` 是**唯一**算统计的地方，Overview 必须调它。
不允许在 Overview 里"顺手再数一遍" —— 那正是两页对不上的成因：
两次独立的计数查询在并发编辑下会取到不同的快照，而差值看起来像 bug。

## 状态流转是业务规则，不是 UI 细节

契约 §2.3 的流转表（`core/vocab.FINDING_STATUS_TRANSITIONS`）必须在**服务端**校验：
不要依赖前端不显示非法按钮 —— 那是双保险里的第二道，不是唯一一道。

**幂等**同样是要求：把已经是 `confirmed` 的条目再标 `confirmed`，
要返回成功但 `changed: 0`、`skipped: 1`，**且不写时间线** ——
否则时间线里会出现「待确认 → 待确认」的噪音，把真正的流转淹掉。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.auth.deps import Identity
from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.timeutil import now_utc
from app.core.vocab import (
    FINDING_STATUS_TRANSITIONS,
    FindingCategory,
    FindingStatus,
    Severity,
    TimelineActor,
    TimelineKind,
    Verification,
    values,
)
from app.findings import strix_vulns
from app.findings.models import Finding, FindingTimelineEntry
from app.findings.schemas import (
    FindingDetailOut,
    FindingOut,
    FindingStatsOut,
    FindingSummaryOut,
    PocOut,
    TimelineEntryOut,
    severity_counts_from,
)
from app.scans.models import Scan

logger = logging.getLogger("suvex.findings")

# 状态的中文名，用在流转摘要里。前端也有标签表，但**摘要文案是服务端写的**
# （它落库进时间线，以后不随前端文案变化），所以服务端要有自己的一份。
STATUS_LABELS = {
    FindingStatus.NEW.value: "待确认",
    FindingStatus.CONFIRMED.value: "已确认",
    FindingStatus.FIXED.value: "已修复",
    FindingStatus.FALSE_POSITIVE.value: "误报",
    FindingStatus.RETESTING.value: "复测中",
    FindingStatus.CLOSED.value: "已关闭",
}


# ══════════════════════════════════════════════════════════
# 导入
# ══════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class ImportResult:
    created: int
    updated: int
    skipped: int


def import_from_run(db: Session, scan: Scan) -> ImportResult:
    """把外部工具的漏洞记录导进本地库。

    **幂等**：靠 `(scan_id, source_ref)` 的部分唯一索引。
    重复导入时更新既有记录的**证据相关字段**（外部工具可能补写了
    `fix_verification` 之类），但**绝不动 `status`** ——
    那是人流转出来的，机器不能覆盖人的判断。

    返回 `(新建, 更新, 跳过)`，给日志与验收用。
    """
    if not scan.run_dir:
        return ImportResult(0, 0, 0)
    run_dir = Path(scan.run_dir)
    if not run_dir.is_dir():
        return ImportResult(0, 0, 0)

    records, error = strix_vulns.read_vulnerabilities(run_dir)
    if error:
        # **不静默当成空**：读失败与"确实没有漏洞"是完全不同的事，
        # 混起来会让"扫了一整轮没发现"和"文件坏了"看起来一样。
        logger.warning("扫描 %s 的漏洞文件读取失败：%s", scan.id, error)
        return ImportResult(0, 0, 0)
    if not records:
        return ImportResult(0, 0, 0)

    parsed = strix_vulns.parse(records, default_target=scan.target)
    created = updated = skipped = 0

    for item in parsed:
        existing = db.execute(
            select(Finding).where(
                Finding.scan_id == scan.id, Finding.source_ref == item.source_ref
            )
        ).scalar_one_or_none()

        if existing is None:
            finding = _new_finding(scan, item)
            db.add(finding)
            db.flush()
            _add_timeline(
                db,
                finding,
                actor=TimelineActor.AGENT.value,
                kind=TimelineKind.DETECTED.value,
                summary=f"扫描发现：{item.title}",
                at=item.discovered_at,
            )
            # 有 PoC 就补一条时间线 —— 它让"什么时候拿到脚本的"可追溯，
            # 而不是只能从现在有没有脚本来推断。
            if item.poc_code:
                _add_timeline(
                    db,
                    finding,
                    actor=TimelineActor.AGENT.value,
                    kind=TimelineKind.POC_GENERATED.value,
                    summary=f"生成了 {item.poc_language or '一段'} 验证脚本",
                    at=item.discovered_at,
                )
            if item.verification == Verification.VERIFIED.value:
                _add_timeline(
                    db,
                    finding,
                    actor=TimelineActor.AGENT.value,
                    kind=TimelineKind.VERIFIED.value,
                    summary="agent 标注为高置信度（已验证）",
                    at=item.discovered_at,
                )
            created += 1
        else:
            changed = _refresh_evidence(existing, item)
            updated += 1 if changed else 0
            skipped += 0 if changed else 1

    if created or updated:
        # SLA 到期时刻要在**导入时**算好（任务 10 P3）。
        # 它是冗余列，不在那时算的话要到第一次改状态才出现 ——
        # 而"刚导入的高危漏洞没有到期时间"恰恰是最需要它的时候。
        new_findings = list(
            db.execute(select(Finding).where(Finding.scan_id == scan.id)).scalars()
        )
        refresh_sla(db, new_findings)
        db.commit()
        logger.info(
            "扫描 %s 导入了漏洞：新建 %d 条，更新 %d 条，未变 %d 条",
            scan.id,
            created,
            updated,
            skipped,
        )
    return ImportResult(created, updated, skipped)


def _new_finding(scan: Scan, item: strix_vulns.ParsedFinding) -> Finding:
    # ★ `status` 一律 `new`：外部工具不产生人的判断。
    #   `verification` 来自 strix 的 confidence —— 那是**证据强度**轴，
    #   由 agent 产出，与处置状态是两件事（契约 §2.2）。
    return Finding(
        id=new_id("finding"),
        scan_id=scan.id,
        source_ref=item.source_ref,
        severity=item.severity,
        category=item.category,
        title=item.title[:300],
        target=(item.target or scan.target)[:512],
        verification=item.verification,
        status=FindingStatus.NEW.value,
        discovered_at=item.discovered_at,
        detail_description=item.description,
        detail_affected=list(item.affected),
        detail_reasoning=item.reasoning,
        detail_evidence=item.evidence,
        poc_language=item.poc_language,
        poc_code=item.poc_code,
        remediation=item.remediation,
        cwe=item.cwe,
        cvss_score=item.cvss_score,
    )


def _refresh_evidence(finding: Finding, item: strix_vulns.ParsedFinding) -> bool:
    """更新**证据相关**的字段。返回是否有变化。

    ⚠️ **不碰 `status`、不碰 `verification`。**
    前者是人的判断，后者是 agent 当初产出时的结论 —— 外部工具后续
    补写文件不该让它们漂移。要改就由人显式流转（那会写时间线）。
    """
    before = (
        finding.title,
        finding.severity,
        finding.category,
        finding.detail_description,
        tuple(finding.detail_affected or []),
        finding.detail_reasoning,
        finding.detail_evidence,
        finding.poc_code,
        finding.remediation,
        finding.cwe,
        finding.cvss_score,
    )
    finding.title = item.title[:300]
    finding.severity = item.severity
    finding.category = item.category
    finding.detail_description = item.description
    finding.detail_affected = list(item.affected)
    finding.detail_reasoning = item.reasoning
    finding.detail_evidence = item.evidence
    finding.poc_language = item.poc_language
    finding.poc_code = item.poc_code
    finding.remediation = item.remediation
    finding.cwe = item.cwe
    finding.cvss_score = item.cvss_score
    after = (
        finding.title,
        finding.severity,
        finding.category,
        finding.detail_description,
        tuple(finding.detail_affected or []),
        finding.detail_reasoning,
        finding.detail_evidence,
        finding.poc_code,
        finding.remediation,
        finding.cwe,
        finding.cvss_score,
    )
    return before != after


# ══════════════════════════════════════════════════════════
# 时间线
# ══════════════════════════════════════════════════════════


def _next_seq(db: Session, finding_id: str) -> int:
    """finding 内的自增序号。

    为什么不是 `count(*) + 1`：删过条目之后 count 会与已有 seq 撞上，
    而 `(finding_id, seq)` 上有唯一约束 —— 那会是一个"删了东西之后
    就再也写不进时间线"的 bug。
    """
    current = db.execute(
        select(func.coalesce(func.max(FindingTimelineEntry.seq), 0)).where(
            FindingTimelineEntry.finding_id == finding_id
        )
    ).scalar_one()
    return int(current) + 1


def _add_timeline(
    db: Session,
    finding: Finding,
    *,
    actor: str,
    kind: str,
    summary: str,
    at=None,  # noqa: ANN001
    from_status: str | None = None,
    to_status: str | None = None,
) -> FindingTimelineEntry:
    entry = FindingTimelineEntry(
        id=new_id("evt"),
        finding_id=finding.id,
        seq=_next_seq(db, finding.id),
        at=at or now_utc(),
        actor=actor,
        kind=kind,
        summary=summary,
        from_status=from_status,
        to_status=to_status,
    )
    db.add(entry)
    db.flush()
    return entry


# ══════════════════════════════════════════════════════════
# 查询
# ══════════════════════════════════════════════════════════


def _split_multi(value: str) -> list[str]:
    """把 `?severity=high,low` 拆成多个取值（任务 10 P6-b 补）。

    ## 为什么需要多值

    界面的筛选是**多选**的（可以同时勾「高危 + 中危」），而接口原本只收
    单值。这个不匹配有两个后果，其中一个很隐蔽：

    · 列表页没法把筛选委托给后端，只能在本地过滤（能工作，但导出就麻烦了）；
    · **导出必须表达同一个筛选**。如果后端只会单值，那用户勾了两个严重度
      再点导出，要么报错、要么**悄悄只导出一个** —— 而他会把那份表发出去。
      "错误的数据比没有数据更糟"（契约 §3.14）。

    所以参数改成接受**逗号分隔**的多个取值。这是向后兼容的加法：
    单个值的行为完全不变（`"high"` 拆出来还是 `["high"]`），
    而逗号在原来的取值集合里本来也不可能出现（枚举值里没有逗号，
    负责人 id 是 ULID）—— 所以原来会 400 的输入现在有了确定含义，
    不存在"同一个请求两种解释"。

    空片段被丢掉：`"high,,low"` 与 `"high,low"` 等价，
    而不是把空串当成一个取值去匹配（那会静默筛出 0 条）。
    """
    return [piece.strip() for piece in value.split(",") if piece.strip()]


def _multi(column, value: str):  # noqa: ANN001, ANN202
    """一个或多个取值 → SQL 条件。

    单值用 `==`、多值用 `IN`：单值写成 `IN ('x')` 虽然等价，
    但查询计划显示出来时更难读，而且 `==` 能走更直接的索引路径。
    """
    values = _split_multi(value)
    if len(values) == 1:
        return column == values[0]
    return column.in_(values)


def _filter_conditions(
    *,
    severity: str | None = None,
    status: str | None = None,
    category: str | None = None,
    q: str | None = None,
    scan_id: str | None = None,
    assignee_id: str | None = None,
    unassigned: bool = False,
    sla_state: str | None = None,
    deleted: bool = False,
    exclude: set[str] | None = None,
) -> list:
    """把筛选条件编译成一串 SQLAlchemy 条件（任务 10 P6 抽出）。

    ## ★ 为什么要抽出来

    抽之前，`list_findings` 与 `stats` **各写了一遍**筛选逻辑 ——
    而它们当时只有一个共同的参数（`scan_id`），所以看起来"没重复多少"。
    这正是最危险的时候：**一旦开始加第二个筛选参数，两份口径就会漂移**，
    而症状是"列表筛出来 3 条、角标写着 7 条"。用户无法解释这个差值，
    只能不再相信角标 —— 那这个功能就白做了。

    现在两者都调它，加筛选参数只需要改这一处。

    `exclude` 用于 **facet 计数**：算某一维度的角标时，要忽略那一维度
    自己的条件（见 `stats` 的说明），否则筛选之后其他档位全变 0、
    按钮全禁用，用户就**没法从当前筛选切到别的档位了**。
    """
    conditions: list = []
    skip = exclude or set()

    # ── 软删除（任务 10 P6-a）────────────────────────────
    #
    # ★ 默认**只看没删的**，而不是"全都返回让前端过滤"。
    #   后者会让回收站条目混进角标与统计，而"删了还在计数"是用户
    #   最不能接受的一种不一致。
    if deleted:
        conditions.append(Finding.deleted_at.is_not(None))
    else:
        conditions.append(Finding.deleted_at.is_(None))

    if severity and "severity" not in skip:
        conditions.append(_multi(Finding.severity, severity))
    if status and "status" not in skip:
        conditions.append(_multi(Finding.status, status))
    if category and "category" not in skip:
        conditions.append(_multi(Finding.category, category))
    if scan_id and "scanId" not in skip:
        conditions.append(Finding.scan_id == scan_id)
    if q and "q" not in skip:
        pattern = f"%{q.strip()}%"
        conditions.append(Finding.title.ilike(pattern) | Finding.target.ilike(pattern))

    # ── 按负责人筛选（任务 10 P2）────────────────────────
    #
    # `unassigned` 与 `assignee_id` 是**两个不同的查询**，不能合成一个：
    #   · `assignee_id=<id>`  = 这个人名下的
    #   · `unassigned=true`   = **还没人认领的**（`assignee_id IS NULL`）
    #
    # 后者不是前者的特例 —— 用 `assignee_id=""` 表达"未指派"会让空串
    # 成为一个特殊值，而数据库里存的是 NULL。
    if "assignee" not in skip:
        if unassigned:
            conditions.append(Finding.assignee_id.is_(None))
        elif assignee_id:
            conditions.append(Finding.assignee_id == assignee_id)

    # ── 按 SLA 状态筛选（任务 10 P3）──────────────────────
    #
    # ★ 用 `sla_due_at` 的**区间条件**，而不是把状态算出来再过滤 ——
    # 后者要先全表取回来（`sla_due_at` 是冗余列的意义就在这里）。
    if sla_state in ("overdue", "dueSoon", "onTrack") and "slaState" not in skip:
        from app.settings.models import SLA_WARN_WITHIN_DAYS

        now = now_utc()
        warn_edge = now + timedelta(days=SLA_WARN_WITHIN_DAYS)
        conditions.append(Finding.sla_due_at.is_not(None))
        if sla_state == "overdue":
            conditions.append(Finding.sla_due_at < now)
        elif sla_state == "dueSoon":
            conditions.append(Finding.sla_due_at >= now)
            conditions.append(Finding.sla_due_at <= warn_edge)
        else:
            conditions.append(Finding.sla_due_at > warn_edge)

    return conditions


def list_findings(
    db: Session,
    *,
    severity: str | None = None,
    status: str | None = None,
    category: str | None = None,
    q: str | None = None,
    scan_id: str | None = None,
    assignee_id: str | None = None,
    unassigned: bool = False,
    sla_state: str | None = None,
    deleted: bool = False,
    sort: str = "recent",
    direction: str = "desc",
) -> list[Finding]:
    """列表 + 筛选 + 排序（契约 §3.4 的查询参数）。

    排序在 SQL 里做。严重度**不能按字符串排**（`critical/high/info/low/medium`
    那个顺序毫无意义），所以用 CASE 表达式把权重下推到数据库 ——
    在 Python 里排意味着先全表取回来。
    """
    import sqlalchemy as sa

    stmt = select(Finding)
    for condition in _filter_conditions(
        severity=severity,
        status=status,
        category=category,
        q=q,
        scan_id=scan_id,
        assignee_id=assignee_id,
        unassigned=unassigned,
        sla_state=sla_state,
        deleted=deleted,
    ):
        stmt = stmt.where(condition)

    from app.core.vocab import SEVERITY_RANK

    descending = direction != "asc"
    if sort == "severity":
        weight = sa.case(
            {level: rank for level, rank in SEVERITY_RANK.items()},
            value=Finding.severity,
            else_=0,
        )
        order = weight.desc() if descending else weight.asc()
    else:
        order = Finding.discovered_at.desc() if descending else Finding.discovered_at.asc()

    # `Finding.id` 兜底：同一毫秒发现的记录如果只按时间排，
    # 两次查询的相对次序可能不同 —— 表现是翻页时看到重复项。
    return list(db.execute(stmt.order_by(order, Finding.id)).scalars())


def get_finding(db: Session, finding_id: str) -> Finding:
    """详情。**带 `selectinload` 一次把时间线取出来**。

    不让它惰性加载：详情页必然要渲染时间线，而惰性加载会在序列化时
    逐条发查询（N+1）。条目多了之后那是一次"看起来莫名其妙慢"的接口。
    """
    finding = db.execute(
        select(Finding)
        .where(Finding.id == finding_id)
        .options(selectinload(Finding.timeline))
    ).scalar_one_or_none()
    if finding is None:
        raise ApiError.not_found("这条漏洞记录")
    return finding


# ══════════════════════════════════════════════════════════
# 软删除 / 恢复 / 永久删除（任务 10 P6-a）
# ══════════════════════════════════════════════════════════


def _load_findings(db: Session, ids: list[str]) -> list[Finding]:
    """按 id 批量取，**保持传入顺序**。

    顺序有意义：批量操作的响应里 `findings` 会原样显示在界面上，
    而数据库返回的顺序不保证与传入一致（`IN (...)` 的顺序不是承诺）。
    """
    if not ids:
        return []
    rows = list(db.execute(select(Finding).where(Finding.id.in_(ids))).scalars())
    index = {row.id: row for row in rows}
    return [index[i] for i in ids if i in index]


def batch_set_deleted(db: Session, *, ids: list[str]) -> tuple[list[Finding], int]:
    """批量**软删除**（进回收站）。返回 `(changed, skipped)`。

    ★ 与扫描模块**同一套语义**（`scans.batch_set_deleted`）：这里不物理删
    任何东西。物理删除只在 `purge_finding`（用户在回收站里明确要求）发生。

    为什么要软删除而不是直接删：漏洞是**交付物的组成部分** ——
    报告会引用它们。误点一下删除就丢掉一条已写进客户报告的问题记录，
    是不可接受的。

    ⚠️ **不检查报告引用**。这是有意的：软删除**不破坏**报告 ——
    报告冻结的是 finding id 与当时的快照，被软删的漏洞仍然存在、
    仍然能渲染出来。拦截在这里会让"这条是误报，我不想再看到它"
    变成做不到的事，而那个需求是真实的。引用检查放在 `purge_finding`
    （那才是真正会造成破坏的一步）。
    """
    findings = _load_findings(db, ids)
    moment = now_utc()
    changed: list[Finding] = []
    skipped = 0

    for finding in findings:
        if finding.deleted_at is not None:
            skipped += 1  # 已经在回收站里
            continue
        finding.deleted_at = moment
        finding.updated_at = moment
        changed.append(finding)

    if changed:
        db.commit()
        for finding in changed:
            db.refresh(finding)
    return changed, skipped


def batch_restore(db: Session, *, ids: list[str]) -> tuple[list[Finding], int]:
    """从回收站恢复。返回 `(changed, skipped)`。

    ⚠️ **不动 `status`**：恢复的是"这条记录还在"，不是"它的处置状态"。
    漏洞在回收站里期间状态没有变过（谁也不会去改一条已经删掉的），
    所以这里恢复成删除之前的样子 —— 顺手改成 `new` 会抹掉分诊成果。
    """
    findings = _load_findings(db, ids)
    moment = now_utc()
    changed: list[Finding] = []
    skipped = 0

    for finding in findings:
        if finding.deleted_at is None:
            skipped += 1  # 本来就不在回收站里
            continue
        finding.deleted_at = None
        finding.updated_at = moment
        changed.append(finding)

    if changed:
        db.commit()
        for finding in changed:
            db.refresh(finding)
    return changed, skipped


def reports_referencing_finding(db: Session, finding_id: str) -> list[str]:
    """哪些报告引用了这条漏洞。返回**报告编号**列表。

    ★ 与 `scans.reports_referencing` 同一个理由，但**粒度更细**：
    那边问"有哪些报告用了这次扫描的漏洞"，这里问"有哪些报告用了这一条"。

    真实破坏路径：`report_findings.finding_id → findings ON DELETE CASCADE`。
    物理删除一条漏洞会连带删掉报告与它的关联行 —— 报告的 `findingIds`
    还在，但指向的东西没了，那份报告被**掏空一格**。
    而报告是交付物（有编号、客户拿去对账），悄悄缺一条是不可接受的。
    """
    from app.reports.models import Report, ReportFinding

    rows = db.execute(
        select(Report.code)
        .join(ReportFinding, ReportFinding.report_id == Report.id)
        .where(ReportFinding.finding_id == finding_id)
    ).all()
    seen: list[str] = []
    for (code,) in rows:
        if code not in seen:
            seen.append(code)
    return seen


def purge_finding(db: Session, finding_id: str) -> None:
    """**永久删除**一条漏洞（物理）。只在回收站内可用。

    三条闸，缺一不可：

    1. **必须先软删除**（`409`）。"先删进回收站，再永久删除"是两道闸；
       合成一步等于把 purge 变成不经过回收站的 `rm -rf`。
    2. **被报告引用时拒绝**（`409`），并说出是哪几份报告 ——
       理由见 `reports_referencing_finding`。
    3. **附件要一起清掉**。`ON DELETE CASCADE` 只删数据库行，
       **不会删磁盘上的文件**，所以必须在删漏洞之前显式清一遍，
       否则磁盘上会永久留下一批谁也引用不到的文件。
    """
    from app.findings import attachments as attachments_mod

    finding = get_finding(db, finding_id)

    if finding.deleted_at is None:
        raise ApiError.conflict(
            "FINDING_NOT_IN_TRASH",
            "这条漏洞不在回收站里。请先删除它（进回收站），再执行永久删除。",
        )

    blocking = reports_referencing_finding(db, finding_id)
    if blocking:
        raise ApiError.conflict(
            "FINDING_REFERENCED_BY_REPORT",
            f"有 {len(blocking)} 份报告引用了这条漏洞（{'、'.join(blocking[:3])}），"
            "永久删除会让那些报告缺一条内容。请先删除相关报告，或保留这条漏洞。",
        )

    # ★ 先清附件（文件 + 行），再删漏洞本身
    attachments_mod.delete_all_for_finding(db, finding_id)
    db.delete(finding)
    db.commit()


# ══════════════════════════════════════════════════════════
# CSV 导出（任务 10 P6-c）
# ══════════════════════════════════════════════════════════

# 导出列。**固定**，不由调用方指定 ——
# 契约 §3.4 的 GET /findings 已经给全了这些字段，而"让后端知道
# 列表显示哪几列"是展示层知识（同一个理由见 `lib/scan-export.ts`）。
#
# 但漏洞导出与扫描导出有一处**关键不同**：漏洞列表要交给客户/同事看，
# 所以带上 `detail` 的两列（描述、受影响位置）——
# 那是"这条问题到底是什么"，没有它这张表只能拿去计数。
EXPORT_COLUMNS = (
    "id",
    "severity",
    "title",
    "category",
    "target",
    "status",
    "verification",
    "assignee",
    "discoveredAt",
    "dueAt",
    "slaState",
)


def _csv_cell(value: str) -> str:
    """CSV 转义：含逗号/引号/换行的值用引号包起来，内部引号翻倍。

    ⚠️ 与 `lib/scan-export.ts` 的 `cell()` 是同一套规则 ——
    前端生成的 CSV 也走它，两边口径必须一致（否则同一个单元格
    在两种导出里长得不一样）。
    """
    if any(ch in value for ch in (",", '"', "\n", "\r")):
        return '"' + value.replace('"', '""') + '"'
    return value


def export_csv(db: Session, *, rows: list[Finding]) -> str:
    """把一批漏洞渲染成 CSV 文本。

    ## 为什么带 BOM

    首字符写 `\\ufeff`。Excel（尤其简体中文 Windows 上那个）默认按
    **本地编码**读 CSV，没有 BOM 的话中文列名会变成乱码 ——
    而"导出给别人看"正是这个功能的用途，打开是乱码等于没做。

    ## 为什么值用**枚举**而不是中文标签

    后端至今**没有任何一处**把 `severity` / `status` 翻成中文：
    `GET /findings` 回的是 `"high"` / `"confirmed"`，前端用
    `SEVERITY_META` / `FINDING_STATUS_LABEL` 自己渲染。
    在导出这里新造一份中文表，就多了一处会漂移的口径
    （前端加了状态、导出表没加 → 导出里出现英文枚举）。

    所以这里**照发枚举值**，中文列名保留（列名是给人读表头的，
    值是拿去筛选/对账的，两者用途不同）。
    """
    lines = [",".join(EXPORT_COLUMNS)]
    for row in rows:
        sla_state, _days_left = sla_state_of(row)
        values = [
            row.id,
            row.severity,
            row.title,
            row.category,
            row.target,
            row.status,
            row.verification,
            row.assignee_id or "",
            row.discovered_at.isoformat(),
            row.sla_due_at.isoformat() if row.sla_due_at else "",
            # `slaState` 由后端判定（契约 §3.4 的同一条规则）——
            # 前端与导出读的是同一个结论，不各自算一遍。
            # `none` 导出成空串：CSV 里"没有值"比"none"更不容易被误解
            # 成一个真的状态。
            "" if sla_state == "none" else sla_state,
        ]
        lines.append(",".join(_csv_cell(str(v)) for v in values))
    # \r\n：CSV 的正式行结束符（RFC 4180），也是 Excel 期望的
    return "\ufeff" + "\r\n".join(lines) + "\r\n"


# ══════════════════════════════════════════════════════════
# 状态流转
# ══════════════════════════════════════════════════════════


def change_status(
    db: Session,
    *,
    ids: list[str],
    target: str,
    note: str | None,
    identity: Identity,
) -> tuple[list[Finding], int]:
    """批量流转。返回 `(真正改了的条目, 跳过的条数)`。

    三条规则，逐条对应契约：

    1. **非法流转 → `400 VALIDATION_FAILED`**（§2.3）。服务端必须校验，
       不能只靠前端不显示非法按钮。
    2. **幂等**：已经是目标状态的条目算 `skipped`，**且不写时间线**（§2.3）。
       不写是因为否则时间线里会出现「待确认 → 待确认」的噪音。
    3. **留痕**（任务 5 要求 2）：每次变更记录**操作人**与时间戳。
       单管理员系统里操作人是常量，但仍然要存 ——
       为将来多用户做准备，与契约 §5.4 的 `authorized_by` 同一个判断。
    """
    if target not in values(FindingStatus):
        raise ApiError.validation(
            f"目标状态不合法：{target!r}",
            {"status": f"应为 {'、'.join(values(FindingStatus))} 之一"},
        )

    # ★ 判为误报必须给理由（任务 10，需求 §一.3）。
    #
    # 企业安全产品普遍要求误报有说明，因为它要回答一个具体问题：
    # **规则误报率是多少**。没有理由的误报只能计数，没法复盘
    # "是哪一类规则在误报" —— 而那个数字决定了下一步该去调规则、
    # 还是调扫描配置。
    #
    # 批量场景下**填一次理由应用到全部选中项**（`note` 天然就是这个语义），
    # 不强制逐条填写 —— 逐条填会让"批量判误报"重新变得不可用，
    # 而用户会转而用"已确认"糊过去，反而把这个信号丢掉了。
    if target == FindingStatus.FALSE_POSITIVE.value and not (note or "").strip():
        raise ApiError.validation(
            "把漏洞标记为误报时必须填写理由。",
            {
                "note": "误报理由不能为空 —— 它会写进每条漏洞的时间线，"
                "用于复盘规则误报率。"
            },
        )

    findings = list(
        db.execute(select(Finding).where(Finding.id.in_(ids)).options(selectinload(Finding.timeline))).scalars()
    )
    found_ids = {finding.id for finding in findings}
    missing = [item for item in ids if item not in found_ids]
    if missing:
        # 一次性说清哪些找不到，而不是"改到第一个不存在的就报错"——
        # 后者会让用户逐个排查，而批量操作里可能一次提交几十条。
        raise ApiError.not_found(f"其中 {len(missing)} 条漏洞记录（{missing[0]} 等）")

    changed: list[Finding] = []
    skipped = 0
    for finding in findings:
        current = finding.status
        if current == target:
            # ★ 幂等：跳过，**且不写时间线**（契约 §2.3）。
            skipped += 1
            continue

        allowed = FINDING_STATUS_TRANSITIONS.get(current, frozenset())
        if target not in allowed:
            raise ApiError.validation(
                f"不能把「{STATUS_LABELS.get(current, current)}」直接改成"
                f"「{STATUS_LABELS.get(target, target)}」。",
                {"status": _transition_hint(current)},
            )

        finding.status = target
        finding.updated_at = now_utc()
        _add_timeline(
            db,
            finding,
            actor=TimelineActor.USER.value,
            kind=TimelineKind.STATUS_CHANGED.value,
            summary=note or _default_summary(current, target, identity),
            from_status=current,
            to_status=target,
        )
        changed.append(finding)

    # ★ SLA 到期时刻要**跟着状态变**（任务 10 P3）。
    #
    # 放在 commit 之前，与状态变更同一个事务：两份数据在同一个事务里
    # 要么都成要么都不成，不会出现"状态变了但到期时间还是旧的"。
    #
    # ⚠️ 必须在 `db.refresh` **之前**：`compute_sla_due` 在
    # `start_from=confirmed` 时要读时间线找第一次确认的时刻，
    # 而刚追加的那条 `_add_timeline` 此刻还在会话里（未刷新）。
    refresh_sla(db, changed)

    db.commit()
    for finding in changed:
        db.refresh(finding)

    _refresh_scan_rollups(db, changed)
    return changed, skipped


def assign_findings(
    db: Session,
    *,
    ids: list[str],
    assignee_id: str | None,
    identity: Identity,
) -> tuple[list[Finding], int]:
    """批量指派负责人。返回 `(真正改了的条目, 跳过的条数)`。

    `assignee_id=None` = **取消指派**。这不是"没传"，而是一个明确的操作
    （"这条没人管了"），所以用 `None` 表达它，而不是另开一个端点。

    三条与 `change_status` 对齐的规则：

    1. **幂等**：已经是这个负责人的条目算跳过，**且不写时间线** ——
       否则时间线里会出现「指派给张三 → 指派给张三」的噪音。
    2. **逐条留痕**：每条漏洞各写一行（含当时的负责人名字）。
       批量一次改 50 条，时间线就是 50 行 —— 这是对的，
       因为"这条漏洞什么时候到了谁手上"是**每条各自的**历史。
    3. **指派人必须存在**：给一个不存在的 id 会 404 而不是静默写入，
       否则 `assignee_id` 会指向空，而界面上那一列永远是空的。
    """
    if assignee_id is not None:
        from app.members.models import Member

        if db.get(Member, assignee_id) is None:
            raise ApiError.not_found(f"处理人 {assignee_id}")

    findings = list(
        db.execute(
            select(Finding).where(Finding.id.in_(ids)).options(selectinload(Finding.timeline))
        ).scalars()
    )
    found_ids = {finding.id for finding in findings}
    missing = [item for item in ids if item not in found_ids]
    if missing:
        raise ApiError.not_found(f"其中 {len(missing)} 条漏洞记录（{missing[0]} 等）")

    # 名字只查一次，用于时间线的摘要文案
    name = assignee_name(db, assignee_id) if assignee_id else None

    changed: list[Finding] = []
    skipped = 0
    for finding in findings:
        if finding.assignee_id == assignee_id:
            # ★ 幂等：跳过且不写时间线
            skipped += 1
            continue

        finding.assignee_id = assignee_id
        finding.assigned_at = now_utc() if assignee_id else None
        finding.updated_at = now_utc()
        _add_timeline(
            db,
            finding,
            actor=TimelineActor.USER.value,
            kind=TimelineKind.ASSIGNED.value,
            summary=f"{identity.label}：指派给「{name}」" if name else f"{identity.label}：取消指派",
        )
        changed.append(finding)

    db.commit()
    for finding in changed:
        db.refresh(finding)
    return changed, skipped


def assignee_name(db: Session, assignee_id: str) -> str:
    """取处理人名字。

    公开函数而不是模块私有：router 在写审计摘要时也要用它，
    而"跨模块调用带下划线的私有函数"是那种一开始能跑、
    后来重构时被静默改坏的依赖。
    """
    from app.members.models import Member

    member = db.get(Member, assignee_id)
    return member.name if member else assignee_id


def assignee_map(db: Session, findings: list[Finding]) -> dict[str, str]:
    """`assignee_id → 名字`。

    一次查完再映射，**不是逐条 `db.get`** —— 列表页一次返回几百条，
    逐条查就是几百次往返（N+1）。这是列表接口最容易悄悄变慢的地方。
    """
    ids = {finding.assignee_id for finding in findings if finding.assignee_id}
    if not ids:
        return {}

    from app.members.models import Member

    rows = db.execute(select(Member.id, Member.name).where(Member.id.in_(ids))).all()
    return {row[0]: row[1] for row in rows}


def sla_config(db: Session):
    """读（必要时建）SLA 配置单例。"""
    from app.settings.models import DEFAULT_SLA_DAYS, SINGLETON_ID, FindingSlaConfig

    config = db.get(FindingSlaConfig, SINGLETON_ID)
    if config is None:
        config = FindingSlaConfig(
            id=SINGLETON_ID,
            critical_days=DEFAULT_SLA_DAYS["critical"],
            high_days=DEFAULT_SLA_DAYS["high"],
            medium_days=DEFAULT_SLA_DAYS["medium"],
            low_days=DEFAULT_SLA_DAYS["low"],
            info_days=DEFAULT_SLA_DAYS["info"],
        )
        db.add(config)
        db.commit()
        db.refresh(config)
    return config


def sla_days_for(config, severity: str) -> int:
    """取某一档的天数。认不出的档位返回 0（不设时限）。

    **认不出就当不设时限**，而不是回退到某个默认值：`severity` 是
    外部工具给的（`strix_vulns` 会映射它），出现一个我们没见过的档位时，
    悄悄给它套上"高危 7 天"的期限是**凭空发明一个承诺**。
    """
    return {
        "critical": config.critical_days,
        "high": config.high_days,
        "medium": config.medium_days,
        "low": config.low_days,
        "info": config.info_days,
    }.get(severity, 0)


def compute_sla_due(finding: Finding, config) -> datetime | None:
    """算出 `sla_due_at`。返回 `None` 表示**不计时**。

    ## 两种情况不计时

    1. **该档位设了 0 天** —— 用户明确表示这类问题不排期。
    2. **已经到终态**（`closed` / `false_positive`）—— 事情结束了，
       再挂一个倒计时只会制造噪音（而且它必然显示成"超期"）。
       `fixed` / `retesting` **仍然计时**：修复动作发生了，但还没验证完，
       对外承诺的期限依然成立。

    ## 起算点

    `start_from` 决定用发现时间还是确认时间。按确认时间时，如果这条
    还没被确认过，就用发现时间兜底 —— 否则未确认的漏洞会集体"没有期限"，
    而它们恰恰是最该被催的。
    """
    from app.core.vocab import FindingStatus, SlaStartFrom

    if finding.status in (FindingStatus.CLOSED.value, FindingStatus.FALSE_POSITIVE.value):
        return None

    days = sla_days_for(config, finding.severity)
    if days <= 0:
        return None

    base = finding.discovered_at
    if config.start_from == SlaStartFrom.CONFIRMED.value:
        confirmed_at = _confirmed_at(finding)
        if confirmed_at is not None:
            base = confirmed_at

    return base + timedelta(days=days)


def _confirmed_at(finding: Finding) -> datetime | None:
    """从时间线里找**第一次**变成 `confirmed` 的时刻。

    为什么不在 `findings` 上单独存一列：那是一个可以从既有数据
    精确推导的值，而多存一列就意味着多一处可能不一致。
    时间线本来就有 `from_status` / `to_status`，那就是权威记录。
    """
    stamps = [
        entry.at
        for entry in (finding.timeline or [])
        if entry.to_status == FindingStatus.CONFIRMED.value
    ]
    return min(stamps) if stamps else None


def refresh_sla(db: Session, findings: list[Finding]) -> int:
    """重算并写回 `sla_due_at`。返回**真正变化了的条数**。

    三个调用时机：导入、状态流转、规则变更。三处都在服务端 ——
    这是"前端永远不算 SLA"这条约束的落点。

    ⚠️ 它自己**不 commit**：调用方通常已经在一个事务里
    （比如状态流转刚改完状态），这里再 commit 会把那次变更提前固化，
    而出错时就回不去了。
    """
    config = sla_config(db)
    changed = 0
    for finding in findings:
        next_due = compute_sla_due(finding, config)
        if finding.sla_due_at != next_due:
            finding.sla_due_at = next_due
            changed += 1
    return changed


def backfill_sla(db: Session) -> int:
    """给**缺失** `sla_due_at` 的未终结漏洞补上。返回补了几条。

    ## 为什么需要它（任务 10 P3 实测发现的遗漏）

    `sla_due_at` 是**派生列**，而它是在功能上线时才加的。那时库里
    已经有几百条漏洞 —— 它们的 `sla_due_at` 全是 NULL，于是列表上
    一个倒计时都不显示。实测时就是这样：配置好了、接口也对，
    但 `slaState` 清一色 `none`。

    三个重算时机（导入 / 状态流转 / 规则变更）都**只覆盖新数据**：
    导入时机对已存在的行不会再触发一次。

    ## 为什么放在启动自愈而不是一次性回填脚本

    一次性脚本只修"这一次的历史"。而这条不变量应该是**始终成立**的：
    "未终结的漏洞，如果它的严重度档位不为 0，就必须有到期时刻"。
    将来任何一条忘了调 `refresh_sla` 的新代码路径，启动时都会被兜住。

    代价接近零：`WHERE sla_due_at IS NULL` 走索引，补完之后每次启动
    都是空扫描。而且它**只补 NULL**，不会覆盖已经算好的值 ——
    规则变更后重算由端点自己负责。

    ⚠️ 不改那条已经跑过的迁移：迁移是"那一刻发生过什么"的记录，
    回头修改它会让文件与实际执行过的内容不一致（而且它不会再跑第二次）。
    """
    from app.core.vocab import FindingStatus

    stale = list(
        db.execute(
            select(Finding)
            .options(selectinload(Finding.timeline))
            .where(
                Finding.sla_due_at.is_(None),
                Finding.status.notin_(
                    (FindingStatus.CLOSED.value, FindingStatus.FALSE_POSITIVE.value)
                ),
            )
        ).scalars()
    )
    if not stale:
        return 0

    changed = refresh_sla(db, stale)
    if changed:
        db.commit()
        logger.info("补齐了 %d 条漏洞的修复时限", changed)
    return changed


def _refresh_scan_rollups(db: Session, findings: list[Finding]) -> None:
    """把受影响扫描的汇总列（`Scan.findings` / `Scan.agent_count`）刷一遍。

    为什么必须做：`Scan.findings` 的口径**排除了误报**
    （见 `scans.service.findings_counts`），所以"把一条漏洞标成误报"
    会让扫描列表上的那条计数变小。不刷的话，扫描列表说 3 条、
    漏洞页说 2 条 —— 用户只能猜哪个是错的。

    异常吞掉：状态流转本身已经提交成功了，汇总列只是列表页的冗余快照。
    不能因为刷不动它就告诉用户"操作失败"（他会再点一次，
    而这次因为幂等会返回 `changed: 0`，白等一轮）。
    """
    from app.scans.models import Scan
    from app.scans.service import refresh_scan_rollup

    scan_ids = {finding.scan_id for finding in findings if finding.scan_id}
    for scan_id in scan_ids:
        scan = db.get(Scan, scan_id)
        if scan is None:
            continue
        try:
            refresh_scan_rollup(db, scan)
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("刷新扫描 %s 的汇总列失败（漏洞状态已落库）", scan_id)


def _default_summary(current: str, target: str, identity: Identity) -> str:
    """默认的流转摘要。

    带上操作人（`identity.describe()`）—— 时间线上"谁改的"必须能看见，
    而契约的 `FindingTimelineEntry` 里**没有** actor 字段之外的位置放它，
    所以写进摘要里。单管理员时它是"管理员会话"，多用户时是具体的标识。
    """
    who = identity.describe()
    return (
        f"{who}：{STATUS_LABELS.get(current, current)} → "
        f"{STATUS_LABELS.get(target, target)}"
    )


def _transition_hint(current: str) -> str:
    allowed = FINDING_STATUS_TRANSITIONS.get(current, frozenset())
    if not allowed:
        return "该状态没有可用的流转方向"
    labels = "、".join(STATUS_LABELS.get(item, item) for item in sorted(allowed))
    return f"当前只能流转到：{labels}"


# ══════════════════════════════════════════════════════════
# 统计 —— 唯一数据源
# ══════════════════════════════════════════════════════════


def stats(  # noqa: PLR0913
    db: Session,
    *,
    scan_id: str | None = None,
    severity: str | None = None,
    status: str | None = None,
    category: str | None = None,
    q: str | None = None,
    assignee_id: str | None = None,
    unassigned: bool = False,
    sla_state: str | None = None,
    deleted: bool = False,
    facets: bool = False,
) -> FindingStatsOut:
    """★ 统计的**唯一实现**。

    Findings 页的 chip 角标与 Overview 页的图表都必须调它。
    在别处"顺手再数一遍"会让两页在并发编辑下取到不同快照，
    而差值看起来像 bug —— 用户会以为其中一个是错的，
    那种不信任会扩散到整个产品。

    所有筛选参数都与 `GET /findings` **同形**（任务 10 P6-b），
    而且编译筛选条件走**同一个** `_filter_conditions` ——
    这样"列表有 3 条、角标写 7 条"这种不一致在结构上就不可能出现。

    ## ★ `facets=True`：角标要能当"切换目标"用

    这是本功能最容易做错的一处。角标有两种可能的含义：

    · **`facets=False`（默认）**：所有计数都在**当前筛选结果**之内。
      筛选 severity=high 之后，"中危 0" 是实话 —— 结果里确实没有中危。
      界面若把 0 的 chip **禁用**，那是对的。
    · **`facets=True`**：算某一维度时**忽略那一维度自己的条件**。
      同一个例子里，"中危"仍然显示它原本的数量。

    为什么需要后者：用户筛了「高危」，想改看「中危」时，
    若中危角标是 0 且按钮被禁用，他**根本切不过去** ——
    必须先把高危取消掉。这不是"数字准不准"的问题，
    而是筛选器还能不能用的问题。

    所以 `exclude` 只跳过**当前维度自己的**条件，其余条件（搜索词、
    扫描范围、负责人）照旧生效：搜索"登录"后的角标就该只统计
    命中"登录"的那些，否则角标会指着一批被搜索排除掉的条目。

    Overview 调它时两个开关都不传 —— 那些图表要的是全量口径。
    """
    # 每一维度算角标时，忽略它自己的筛选条件（见 docstring）
    def conds_for(own: str | None):  # noqa: ANN202
        return _filter_conditions(
            severity=severity,
            status=status,
            category=category,
            q=q,
            scan_id=scan_id,
            assignee_id=assignee_id,
            unassigned=unassigned,
            sla_state=sla_state,
            deleted=deleted,
            exclude={own} if (facets and own) else None,
        )

    def count_of(conditions: list) -> int:
        stmt = select(func.count()).select_from(Finding)
        for condition in conditions:
            stmt = stmt.where(condition)
        return int(db.execute(stmt).scalar_one())

    def group(column, conditions: list) -> list[tuple[str, int]]:  # noqa: ANN001
        stmt = select(column, func.count()).select_from(Finding)
        for condition in conditions:
            stmt = stmt.where(condition)
        stmt = stmt.group_by(column)
        return [(str(key), int(count)) for key, count in db.execute(stmt).all()]

    # `total` 用当前**全部**条件（它就是"筛出来几条"）
    total = count_of(conds_for(None))

    by_severity = group(Finding.severity, conds_for("severity"))
    by_status = group(Finding.status, conds_for("status"))
    by_category = group(Finding.category, conds_for("category"))

    return FindingStatsOut(
        total=total,
        # `Partial` 语义：为 0 的档**键不存在**（由 severity_counts_from 统一处理）
        bySeverity=severity_counts_from(by_severity),
        # 状态的四档**全部返回**（包括 0）：那是筛选 chip 的角标，
        # 缺一个会让 chip 消失 —— 而"这一类一条都没有"本身是信息。
        byStatus={member.value: dict(by_status).get(member.value, 0) for member in FindingStatus},
        byCategory={member.value: dict(by_category).get(member.value, 0) for member in FindingCategory},
        # ★ 单独返回：Overview 的指标卡排除误报，界面上必须说清排除了几条。
        # 前端注释原话：「排除是可以的，悄悄排除不行。」
        #
        # ⚠️ 它跟随 `status` 的 facet 口径（忽略 status 筛选）——
        #    否则筛了「已确认」之后这句话会变成"已排除 0 条误报"，
        #    而那个数字的原意是"这次统计的范围里有多少误报"。
        falsePositiveCount=dict(by_status).get(FindingStatus.FALSE_POSITIVE.value, 0),
    )


def overview_metrics(db: Session, *, scan_id: str | None = None) -> dict:
    """Overview 页用的一组派生指标。

    ⚠️ **它内部调 `stats()`**，不自己再数一遍 —— 这是任务 5 验收标准的
    核心（两页数字必须来自同一处查询逻辑）。
    """
    snapshot = stats(db, scan_id=scan_id)

    verified_stmt = (
        select(func.count())
        .select_from(Finding)
        .where(Finding.verification == Verification.VERIFIED.value)
        .where(Finding.status != FindingStatus.FALSE_POSITIVE.value)
    )
    if scan_id:
        # ⚠️ 不能在 `.where()` 里写 `scan_id if scan_id else True` ——
        # 那是 Django ORM 的写法，SQLAlchemy 会把裸 `True` 当成一个
        # SQL 表达式去编译，在 PostgreSQL 上直接报错。
        verified_stmt = verified_stmt.where(Finding.scan_id == scan_id)
    verified_poc = int(db.execute(verified_stmt).scalar_one())

    # ── 「本周新增高危」与同比 ────────────────────────────────
    #
    # 口径逐条照前端 `overview-mock.ts: deriveMetrics`：
    #
    # * **排除误报**（`isCountedFinding`）。这两个指标回答的是
    #   「我该处理什么」，一条被判误报的条目根本不是漏洞 ——
    #   把它算进"待处理的高危"会让这个数字**永远降不下来**：
    #   用户在 Findings 页认真分诊一圈，Overview 纹丝不动，
    #   那次分诊就成了没有反馈的无效劳动。
    # * **「已修复」仍算**。它曾经是个真漏洞，属于"发现过什么"的记录。
    #   按状态抹掉会让"本周新增"变成一个随处置而回溯变化的数字，没法用。
    #   一句话：这个过滤器排除的是「**判断错误**」，不是「已处理」。
    # ⚠️ 只取**一次** now —— 两次 `now_utc()` 会得到两个相差几微秒的时刻，
    # 而上面两个窗口是**首尾相接**的（`[two_weeks_ago, week_ago)` 与
    # `[week_ago, now]`）。落在那一小段缝里的发现会**两个窗口都不算**，
    # 于是"本周 + 上周"凭空少一条，且不可复现。
    now = now_utc()
    week_ago = now - timedelta(days=7)
    two_weeks_ago = now - timedelta(days=14)

    high_or_above = (
        select(func.count())
        .select_from(Finding)
        .where(Finding.severity.in_((Severity.CRITICAL.value, Severity.HIGH.value)))
        .where(Finding.status != FindingStatus.FALSE_POSITIVE.value)
    )
    if scan_id:
        high_or_above = high_or_above.where(Finding.scan_id == scan_id)

    this_week = int(db.execute(high_or_above.where(Finding.discovered_at >= week_ago)).scalar_one())
    last_week = int(
        db.execute(
            high_or_above.where(Finding.discovered_at >= two_weeks_ago).where(
                Finding.discovered_at < week_ago
            )
        ).scalar_one()
    )

    # 严重度分布**包含全部发现**（与 Findings 页逐条对得上），
    # 而上面两个指标排除误报 —— 这个口径差异必须在界面上说明，
    # 所以 `falsePositiveCount` 要跟着返回。
    breakdown = [
        {"severity": member.value, "count": getattr(snapshot.bySeverity, member.value) or 0}
        for member in Severity
    ]

    return {
        "severityBreakdown": breakdown,
        "verifiedPocCount": verified_poc,
        "falsePositiveCount": snapshot.falsePositiveCount,
        "severityCounts": snapshot.bySeverity,
        "newHighSeverityThisWeek": this_week,
        # ★ `null` 而不是 0 —— 契约 §3.6 用 ⚠️ 标了：上周为 0 时
        # 给不出有意义的同比（"+3 vs 0" 会误导），界面显示"暂无对比数据"。
        # 0 变化和无法比较是两回事。
        "newHighSeverityDelta": None if last_week == 0 else this_week - last_week,
    }


# ══════════════════════════════════════════════════════════
# 序列化
# ══════════════════════════════════════════════════════════


def sla_state_of(finding: Finding, now: datetime | None = None) -> tuple[str, int | None]:
    """算出 `(slaState, slaDaysLeft)`（任务 10 P3）。

    返回 `daysLeft` 而不是让前端从 `dueAt` 再算一次：那是同一个判断
    做两遍，而两遍的"今天"可能不是同一天（跨零点、时区、客户端时钟）。

    `daysLeft` 的取整规则是**向上取整**（`ceil`）：剩 4 小时应该显示
    "还剩 1 天"而不是"还剩 0 天" —— 后者读起来像"今天到期"，
    而它其实是"还有半天"。已超期时为负（`-1` = 超了不到一天）。
    """
    from app.settings.models import SLA_WARN_WITHIN_DAYS

    if finding.sla_due_at is None:
        return "none", None

    moment = now or now_utc()
    due = finding.sla_due_at
    # 统一成 aware，否则 aware 与 naive 相减会抛 TypeError
    if due.tzinfo is None:
        due = due.replace(tzinfo=UTC)

    seconds_left = (due - moment).total_seconds()
    if seconds_left >= 0:
        # 还没到期：**向上取整**。剩 4 小时显示「还剩 1 天」——
        # 用 floor 会给 0，读起来像"今天到期"，实则是"还有半天"。
        days_left = math.ceil(seconds_left / 86400)
    else:
        # 已超期：按**完整天数**算（向下取整），因为"超期几天"是要拿去
        # 催人的 —— 昨天到期说成"超期 2 天"是夸大。
        # 但**不足一天的超期要显示 -1 而不是 0**：0 会被读成
        # "今天到期"（还没超），而它明明已经过了。所以下限钳到 -1。
        overdue_days = math.floor(-seconds_left / 86400)
        days_left = -max(overdue_days, 1)

    if days_left < 0:
        return "overdue", days_left
    if days_left <= SLA_WARN_WITHIN_DAYS:
        return "dueSoon", days_left
    return "onTrack", days_left


def to_summary(
    finding: Finding,
    *,
    assignee_name: str | None = None,
    now: datetime | None = None,
) -> FindingSummaryOut:
    """列表项序列化。

    `assignee_name` 由调用方**批量查好后传进来**（见 `assignee_map`），
    而不是在这里 `db.get` —— 这个函数会在列表里被调几百次，
    每次查一次库就是标准的 N+1。默认 `None` 保持它是纯函数，
    单测里可以直接构造对象调它。

    `now` 同理：默认取当前时刻，但允许调用方/测试固定它 ——
    否则"还剩几天"这类断言会在跨零点时随机失败。
    """
    sla_state, days_left = sla_state_of(finding, now)
    return FindingSummaryOut(
        id=finding.id,
        scanId=finding.scan_id,
        severity=finding.severity,
        category=finding.category,
        title=finding.title,
        target=finding.target,
        verification=finding.verification,
        status=finding.status,
        discoveredAt=finding.discovered_at,
        assigneeId=finding.assignee_id,
        assigneeName=assignee_name,
        dueAt=finding.sla_due_at,
        slaState=sla_state,
        slaDaysLeft=days_left,
        # 非空 = 在回收站（v0.18）。与 `Scan.archivedAt` 同构：
        # 用时间戳不用布尔，因为界面要能说"3 天前删的"。
        deletedAt=finding.deleted_at,
    )


def summaries(db: Session, findings: list[Finding]) -> list[FindingSummaryOut]:
    """批量序列化 —— **一次**把负责人名字查全，再逐条映射。

    列表接口统一走这个，不要在各处自己 `[to_summary(f) for f in ...]`：
    那样会漏掉名字装配，而表现是"负责人那一列永远是空的"。
    """
    names = assignee_map(db, findings)
    return [
        to_summary(finding, assignee_name=names.get(finding.assignee_id or ""))
        for finding in findings
    ]


def to_detail(finding: Finding) -> FindingOut:
    poc = finding.poc
    return FindingOut(
        id=finding.id,
        scanId=finding.scan_id,
        severity=finding.severity,
        category=finding.category,
        title=finding.title,
        target=finding.target,
        verification=finding.verification,
        status=finding.status,
        discoveredAt=finding.discovered_at,
        detail=FindingDetailOut(
            description=finding.detail_description,
            affected=list(finding.detail_affected or []),
            reasoning=finding.detail_reasoning,
            evidence=finding.detail_evidence,
            poc=PocOut(**poc) if poc else None,
            remediation=finding.remediation,
        ),
        # ★ **倒序**（最新在前）—— 契约 §2.2 明确要求，而模型的 relationship
        # 已经按 `at desc, seq desc` 排好了（同一毫秒内靠 seq 定序）。
        timeline=[
            TimelineEntryOut(
                id=entry.id,
                at=entry.at,
                actor=entry.actor,
                kind=entry.kind,
                summary=entry.summary,
                from_=entry.from_status,
                to=entry.to_status,
            )
            for entry in finding.timeline
        ],
    )


def group_findings(
    db: Session,
    *,
    severity: str | None = None,
    status: str | None = None,
    category: str | None = None,
    q: str | None = None,
    scan_id: str | None = None,
    assignee_id: str | None = None,
    unassigned: bool = False,
    limit: int = 500,
) -> list[dict]:
    """按**漏洞标题**分组（任务 10 P4）。

    ## 为什么按 title 而不是按 category 或 id

    同一个漏洞会出现在**多个目标**上：截图里那条"调试接口未授权可读环境变量"
    在两个目标上各是一条独立记录 —— 那是**对的**（不同目标各自的实例，
    修复状态也各自独立），但它们显然是**同一类问题**。

    按 `category` 分太粗（`misconfiguration` 下有几十种不同的东西，
    折叠起来等于没分）；按 `source_ref` 或 id 分太细（每次扫描都重来一遍）。
    **标题才是"人眼里这是同一个问题"的那个粒度** —— 与扫描对比模块
    （`scans.service.compare_scans`）用的是同一个判据，两处必须一致，
    否则"两次扫描对比说消失了、分组视图里还在"。

    ## 为什么在 SQL 里分而不是取回来在 Python 里分

    `findings` 会到几万行。取回全部再 `itertools.groupby` 意味着
    每次切到分组视图都拉一次全表 —— 而分组视图恰恰是**最需要看全貌**的
    场景，不能让它是最慢的那个。

    ## 返回形状

    每个组带：标题、严重度（取组内**最高**的）、类别、实例数、
    涉及的目标列表、以及按状态分布的计数。前端据此渲染折叠行与展开行，
    **不需要再拉一次明细** —— 展开时用组内 `findingIds` 去筛列表即可。
    """
    from collections import defaultdict

    findings = list_findings(
        db,
        severity=severity,
        status=status,
        category=category,
        q=q,
        scan_id=scan_id,
        assignee_id=assignee_id,
        unassigned=unassigned,
        sort="severity",
        direction="desc",
    )

    # 严重度用**数值排名**比大小，不能用字符串（`critical/high/...` 的字典序无意义）
    from app.core.vocab import SEVERITY_RANK

    groups: dict[str, dict] = {}
    for finding in findings:
        key = finding.title
        bucket = groups.get(key)
        if bucket is None:
            groups[key] = {
                "title": key,
                "severity": finding.severity,
                "category": finding.category,
                "findingIds": [finding.id],
                "targets": [finding.target],
                "byStatus": defaultdict(int),
            }
            bucket = groups[key]
        else:
            bucket["findingIds"].append(finding.id)
            if finding.target not in bucket["targets"]:
                bucket["targets"].append(finding.target)
            # 组内最高严重度决定这一组长什么样 —— 取平均或取第一条都会
            # 让"这一组里有个 critical"被藏起来
            if SEVERITY_RANK.get(finding.severity, 0) > SEVERITY_RANK.get(bucket["severity"], 0):
                bucket["severity"] = finding.severity
        bucket["byStatus"][finding.status] += 1

    result = []
    for bucket in groups.values():
        result.append(
            {
                "title": bucket["title"],
                "severity": bucket["severity"],
                "category": bucket["category"],
                "instanceCount": len(bucket["findingIds"]),
                "targetCount": len(bucket["targets"]),
                "targets": bucket["targets"],
                "findingIds": bucket["findingIds"],
                "byStatus": dict(bucket["byStatus"]),
            }
        )

    # 排序：先按严重度降序，同档按**影响面**降序（影响目标多的先看）。
    # 影响面是分组视图存在的理由，所以它是第二个排序键。
    result.sort(
        key=lambda g: (-SEVERITY_RANK.get(g["severity"], 0), -g["targetCount"], g["title"])
    )
    return result[:limit]


__all__ = [
    "ImportResult",
    "STATUS_LABELS",
    "change_status",
    "get_finding",
    "group_findings",
    "import_from_run",
    "list_findings",
    "overview_metrics",
    "stats",
    "summaries",
    "to_detail",
    "to_summary",
]
