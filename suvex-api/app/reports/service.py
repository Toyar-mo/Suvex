"""`reports` 领域的业务逻辑（契约 §3.5、§3.5.1）。

## 三条不能违反的规则（都在前端有完整论证）

1. **`excludedCount` 在生成那一刻算好并冻结。** 不复现时重算 ——
   重算恒为 0，会让"收录 0 条"的报告谎称"本次范围内未发现问题"。
   **那不是没问题，那是没看。**
2. **`findingIds` 的顺序是报告的一部分。** 存进 `report_findings.position`，
   复现时按它取回，不按当前数据重排。
3. **`code` 由生成动作分配，之后不再重算。** 编号是交付物的身份，
   客户拿它引用；草稿阶段它就是没有的。

## 关于 PDF

契约 §3.5.1 定了本轮**保留浏览器打印**，且真要做服务端 PDF 时必须
打印**前端渲染的那份 HTML**（不得引入第二套排版引擎）。
本模块因此**不产出 PDF** —— 见 `docs/adr-007-report-export.md` 的完整判据，
以及契约 §3.5.1 里补的实测证据。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.errors import ApiError
from app.core.ids import new_id, report_code
from app.core.timeutil import now_utc
from app.core.vocab import ReportFormat, ReportScopeKind
from app.findings.models import Finding
from app.reports import document as ir
from app.reports.markdown import render_markdown
from app.reports.models import Report, ReportFinding
from app.scans.models import Scan

logger = logging.getLogger("suvex.reports")


@dataclass(frozen=True, slots=True)
class Scope:
    """报告范围。与契约 §2.7 的 discriminated union 一致。

    用**三个可选字段**在 Python 侧表示一个 union 是别扭的，所以这里用
    一个 dataclass + `kind` 判别，并在 `__post_init__` 里拒绝非法组合 ——
    DB 上那条 CHECK 约束（`scope_exactly_one_shape`）是同一件事的第二道。
    """

    kind: str
    scan_id: str | None = None
    target: str | None = None
    from_: datetime | None = None
    to: datetime | None = None
    # ★ `kind="findings"` 时的成员（任务 10 P5）。
    #
    # ⚠️ 它**不进 `reports` 表** —— 那张表上只有四个 scope 列，
    # 而一串 id 塞不进任何一列。成员存在既有的 `report_findings`
    # 关联表里（那本来就是"这份报告冻结了哪些漏洞"），所以：
    #   · 生成时从这里取成员；
    #   · 复现时从关联表读回来（见 `scope_of`）。
    # 这样"冻结"这件事只有一处事实来源，不会出现"scope 里的 id
    # 与 report_findings 里的不一致"。
    finding_ids: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.kind not in {member.value for member in ReportScopeKind}:
            raise ValueError(f"未知的范围类型：{self.kind!r}")

        # ★ union 语义要**两边都查**：该有的在，且不该有的**必须为空**。
        #
        # 第一版只查了前者，于是 `Scope(kind="scan", scan_id="s", target="t")`
        # 能构造出来 —— 而"到底按哪个过滤"要靠读实现猜。
        # 测试当场抓到（`test_scope_rejects_mixed_shapes`）。
        # 数据库上那条 CHECK（`scope_exactly_one_shape`）是同一件事的第二道。
        provided = {
            ReportScopeKind.SCAN.value: self.scan_id is not None,
            ReportScopeKind.TARGET.value: self.target is not None,
            ReportScopeKind.RANGE.value: self.from_ is not None and self.to is not None,
            ReportScopeKind.FINDINGS.value: bool(self.finding_ids),
        }
        if not provided[self.kind]:
            raise ValueError(f"kind={self.kind} 时缺少对应的字段")
        extras = [key for key, value in provided.items() if key != self.kind and value]
        if extras:
            raise ValueError(
                f"scope 只能给出一种范围，不能同时给 {'、'.join(extras)}"
            )

    def to_api(self) -> dict[str, Any]:
        if self.kind == ReportScopeKind.SCAN.value:
            return {"kind": "scan", "scanId": self.scan_id}
        if self.kind == ReportScopeKind.TARGET.value:
            return {"kind": "target", "target": self.target}
        if self.kind == ReportScopeKind.FINDINGS.value:
            # 这一种要把 id 列表带回给前端 —— 报告页要能显示"这份报告
            # 冻结了哪几条"，以及重新导出时按同一批复现。
            return {"kind": "findings", "findingIds": list(self.finding_ids or ())}
        return {"kind": "range", "from": ir._iso(self.from_), "to": ir._iso(self.to)}


def scope_of(report: Report, *, finding_ids: Sequence[str] | None = None) -> Scope:
    """从报告行还原 scope（任务 10 P5 扩展）。

    `finding_ids` 缺省时读 `report.finding_ids` —— 那是 `Report` 上既有的
    属性（按 `position` 排好的成员 id）。`reports` 表上**没有**对应的列
    （一串 id 塞不进任何一列），成员在 `report_findings` 关联表里，
    而那个属性正是它的读取口。

    ⚠️ 读它会让 `entries` 关系惰性加载。序列化路径上的调用点应该
    `selectinload(Report.entries)` —— 否则列表里每份报告各发一次查询。

    ⚠️ `kind="findings"` 而成员为空时，`Scope.__post_init__` 会抛
    "缺少对应的字段"。那是**对的**：一份 findings 范围的报告却没有成员，
    说明数据不一致，静默当成空报告比报错更糟 —— 用户会拿到一份
    "0 条发现"的报告而不知道出了什么事。
    """
    resolved = finding_ids if finding_ids is not None else report.finding_ids
    return Scope(
        kind=report.scope_kind,
        scan_id=report.scope_scan_id,
        target=report.scope_target,
        from_=report.scope_from,
        to=report.scope_to,
        # ★ **只在 `kind="findings"` 时带上成员**（任务 10 P5）。
        #
        # 这是个容易搞混的地方：`report_findings` 里存的成员，对
        # `scan`/`target`/`range` 三种 scope 来说是**查出来的结果**
        # （"这次范围内有哪些漏洞"），而对 `findings` 来说是
        # **scope 的定义本身**（"用户手选了这几条"）。
        #
        # 无条件带上会直接炸：`Scope.__post_init__` 的 union 检查看到
        # "scan 和 findings 两组都非空"，抛「scope 只能给出一种范围」——
        # 而它报的那句话跟真正的原因（成员是结果不是定义）毫无关系，
        # 排查时会往 scope 的构造上去找。
        finding_ids=(
            tuple(resolved) if resolved and report.scope_kind == ReportScopeKind.FINDINGS.value else None
        ),
    )


# ══════════════════════════════════════════════════════════
# 范围筛选 —— 对应 report-build.ts: findingsInScope
# ══════════════════════════════════════════════════════════


def findings_in_scope(db: Session, scope: Scope) -> list[Finding]:
    """按范围挑出发现。对应前端 `findingsInScope`。

    ⚠️ 时间区间用**闭区间** [from, to]，且 `to` 要覆盖当天最后一毫秒：

    用户说"2月1日到2月14日"时，心里想的是包含这两天的。
    开区间会让"我明明选了今天"发现少一条。
    """
    stmt = select(Finding).options(selectinload(Finding.timeline))

    if scope.kind == ReportScopeKind.SCAN.value:
        stmt = stmt.where(Finding.scan_id == scope.scan_id)
    elif scope.kind == ReportScopeKind.TARGET.value:
        stmt = stmt.where(Finding.target == scope.target)
    elif scope.kind == ReportScopeKind.FINDINGS.value:
        # ★ 用户手选的那几条（任务 10 P5）。
        #
        # 用 `in_` 一次查完，且**不保留传入的顺序** —— 报告有自己的
        # 排序规则（`sort_for_report`：严重度降序、同级按发现时间升序），
        # 而那比"用户勾选的先后"更有意义：给管理层看的文档里，
        # 顺序应该反映严重程度而不是勾选动作。
        stmt = stmt.where(Finding.id.in_(scope.finding_ids or ()))
    elif scope.kind == ReportScopeKind.RANGE.value:
        start = _ensure_utc(scope.from_)
        # 让 to 覆盖当天最后一毫秒
        end = _ensure_utc(scope.to).replace(hour=23, minute=59, second=59, microsecond=999999)
        stmt = stmt.where(Finding.discovered_at >= start).where(Finding.discovered_at <= end)

    return list(db.execute(stmt).scalars())


def _ensure_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def scans_for_document(db: Session, findings: list[Finding]) -> list[Scan]:
    """报告附录 A 需要扫描的上下文（名称、状态、技能）。

    取**全部**扫描而不是只取涉及的那些：附录里要解析扫描名，
    而"范围内没有发现"时还要退回范围指向的那次扫描。
    """
    del findings  # 取全量，理由见 docstring
    return list(db.execute(select(Scan)).scalars())


# ══════════════════════════════════════════════════════════
# 生成
# ══════════════════════════════════════════════════════════


def preview(
    db: Session,
    *,
    title: str,
    scope: Scope,
    options: ir.ReportOptions,
) -> dict[str, Any]:
    """预览：**不落库**，返回 ReportDocument IR。

    `provisional=True` ⇒ `meta.code` 是空串。这是刻意的（契约 §2.7）：
    编号由生成动作分配，生成之前它不存在。草稿显示一个"将来的编号"
    会让预览与下载对不上，而编号恰恰是客户用来对账的东西。
    """
    findings = findings_in_scope(db, scope)
    scans = scans_for_document(db, findings)

    return ir.build_document(
        findings=findings,
        scans=scans,
        scope=scope.to_api(),
        options=options,
        title=title,
        now=now_utc(),
        provisional=True,
    )


def generate(
    db: Session,
    *,
    title: str,
    scope: Scope,
    options: ir.ReportOptions,
    fmt: str,
) -> Report:
    """生成一份报告并**冻结**下来。

    冻结的是三样东西：`finding_ids`（含顺序）、`excluded_count`、
    `severity_counts`。之后无论漏洞怎么变，这份报告的内容都不变 ——
    报告是一份**交付物**，它必须和当时发出去的那份一致。
    """
    if fmt not in {member.value for member in ReportFormat}:
        raise ApiError.validation(
            f"格式不合法：{fmt!r}",
            {"format": "应为 markdown 或 pdf"},
        )
    # ⚠️ 本轮**只支持 markdown 落库**。pdf 走浏览器打印，不在这里生成文件
    # （契约 §3.5.1）。允许用 pdf 建记录是为了让历史列表能显示
    # "这份报告当初是按 PDF 交付的"——但下载时只会给 markdown，
    # 而前端按 `format` 分发（browser-print vs file）。
    moment = now_utc()
    findings = findings_in_scope(db, scope)

    selected = ir.apply_options(findings, options)
    ordered = ir.sort_for_report(selected)
    excluded = len(findings) - len(selected)
    stats = ir.summarize(selected)

    report = Report(
        id=new_id("rpt"),
        # ★ 编号**只在这里**分配一次，之后跟着报告行走、不再重算。
        code=report_code(moment),
        title=title,
        scope_kind=scope.kind,
        scope_scan_id=scope.scan_id,
        scope_target=scope.target,
        scope_from=scope.from_,
        scope_to=scope.to,
        opt_include_poc=options.include_poc,
        opt_include_reasoning=options.include_reasoning,
        opt_only_confirmed=options.only_confirmed,
        opt_include_remediation=options.include_remediation,
        format=fmt,
        created_at=moment,
        finding_count=len(ordered),
        # ★ 冻结的排除数 —— 复现时不重算（会恒为 0，见模块 docstring）
        excluded_count=excluded,
        severity_counts=stats["bySeverity"],
    )
    db.add(report)
    db.flush()

    # ★ 顺序也是报告的一部分（严重度降序），所以用 position 记下来，
    #   而不是复现时按当前数据重排。
    for position, finding in enumerate(ordered):
        db.add(ReportFinding(report_id=report.id, finding_id=finding.id, position=position))

    db.commit()
    db.refresh(report)
    logger.info(
        "已生成报告 %s（%s，%d 条，排除 %d 条）",
        report.code,
        scope.kind,
        report.finding_count,
        report.excluded_count,
    )
    return report


def document_for(db: Session, report: Report) -> dict[str, Any]:
    """复现一份报告 —— 对应前端 `rebuildReportDocument`。

    ★ **用冻结的 `findingIds` 重建，而不是按 scope 重新过滤。**

    按 scope 重算的话，三个月后回看会看到"和当初不一样"的内容 ——
    期间修好的漏洞消失了、新发现的凭空出现。那就是同一份交付物有了两个版本，
    在合规场景里是致命的。

    同时 `options` 也用**当时**的：用户后来把开关改回默认，
    不能让昨天签出去的报告跟着变。
    """
    frozen_ids = [entry.finding_id for entry in report.entries]
    by_id = {
        finding.id: finding
        for finding in db.execute(
            select(Finding)
            .where(Finding.id.in_(frozen_ids))
            .options(selectinload(Finding.timeline))
        ).scalars()
    }
    # 按**冻结的顺序**取回。
    # 漏洞被彻底删除时该条目会消失：如实少几条，比造一条假数据补位要诚实。
    frozen = [by_id[finding_id] for finding_id in frozen_ids if finding_id in by_id]
    missing = len(frozen_ids) - len(frozen)
    if missing:
        logger.warning("报告 %s 有 %d 条冻结条目已不存在", report.code, missing)

    scans = scans_for_document(db, frozen)
    options = ir.ReportOptions(
        include_poc=report.opt_include_poc,
        include_reasoning=report.opt_include_reasoning,
        only_confirmed=report.opt_only_confirmed,
        include_remediation=report.opt_include_remediation,
    )

    doc = ir.build_document(
        findings=frozen,
        scans=scans,
        scope=scope_of(report).to_api(),
        options=options,
        title=report.title,
        now=report.created_at,
        # 用**冻结的编号**重建 —— 不是重新分配一个
        report_id=report.code,
        excluded_count=report.excluded_count,
    )
    return doc


def render_for_download(db: Session, report: Report) -> tuple[str, str]:
    """下载用：返回 `(文件名, 内容)`。

    ⚠️ **文件名用报告编号，不是标题**（契约 §3.5）。
    标题是中文、可能带空格和斜杠（"api.acme.internal 第二轮复测"），
    直接当文件名会出现编码差异甚至被截断。编号是 ASCII 且天然唯一，
    客户拿到文件后也能和正文里的编号对上。
    """
    doc = document_for(db, report)
    return f"{report.code}.md", render_markdown(doc)


# ══════════════════════════════════════════════════════════
# 历史
# ══════════════════════════════════════════════════════════


def list_reports(db: Session) -> list[Report]:
    return list(
        db.execute(select(Report).options(selectinload(Report.entries)).order_by(Report.created_at.desc())).scalars()
    )


def get_report(db: Session, report_id: str) -> Report:
    report = db.execute(
        select(Report).where(Report.id == report_id).options(selectinload(Report.entries))
    ).scalar_one_or_none()
    if report is None:
        raise ApiError.not_found("这份报告")
    return report


def delete_report(db: Session, report_id: str) -> None:
    report = get_report(db, report_id)
    db.delete(report)
    db.commit()
    logger.info("已删除报告 %s", report.code)


# ══════════════════════════════════════════════════════════
# 序列化
# ══════════════════════════════════════════════════════════


def to_out(report: Report) -> dict[str, Any]:
    """`Report` 的出参形状 —— 与前端 `data/report.ts` 的 `Report` 逐字一致。"""
    return {
        "id": report.id,
        "title": report.title,
        "scope": scope_of(report).to_api(),
        "options": {
            "includePoc": report.opt_include_poc,
            "includeReasoning": report.opt_include_reasoning,
            "onlyConfirmed": report.opt_only_confirmed,
            "includeRemediation": report.opt_include_remediation,
        },
        "createdAt": ir._iso(report.created_at),
        "findingCount": report.finding_count,
        "excludedCount": report.excluded_count,
        # `Partial`：为 0 的档键不存在
        "severityCounts": dict(report.severity_counts or {}),
        "format": report.format,
        "findingIds": [entry.finding_id for entry in report.entries],
        # 前端 `Report` 类型里没有 `code`，但报告编号是交付物身份，
        # 历史列表与正文里都要显示它 —— 加在出参里是**加法**，
        # 前端可以忽略，也可以用它显示编号。
        "code": report.code,
    }


__all__ = [
    "Scope",
    "delete_report",
    "document_for",
    "findings_in_scope",
    "generate",
    "get_report",
    "list_reports",
    "preview",
    "render_for_download",
    "scope_of",
    "to_out",
]
