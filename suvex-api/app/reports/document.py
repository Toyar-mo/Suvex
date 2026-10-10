"""报告文档 IR —— **逐字复刻前端 `lib/report-document.ts` 的结构**。

## 为什么是"复刻"而不是"重新设计"

契约 §3.5.1 与前端注释都写明：

> `buildReportDocument` 是「预览与导出的唯一真相」。

后端产出的 IR 必须与前端 `buildReportDocument` 的输出**形状完全一致**，
否则前端拿到它渲染预览时会发现字段对不上 —— 而那种失败是"预览空白"，
排查方向会往网络/序列化上找。

所以本模块里每个字段、每处措辞、每个排序都是照着
`report-document.ts` / `report-build.ts` / `report-markdown.ts`
抄下来的，并且在有分歧风险的地方标了出处。

## 三条不能改的语义（都在前端有完整论证）

1. **`meta.code` 在草稿时是空串**，不是"暂时随便给一个"。
   编号由生成动作分配，生成之前它不存在。空串是**如实**的表达。
2. **`excludedCount` 是生成时冻结的值**，复现时不许重算。
   重算恒为 0，会让"收录 0 条"的报告谎称"本次范围内未发现问题"——
   **那不是没问题，那是没看。**
3. **`findingIds` 的顺序是报告的一部分**（严重度降序，同级按发现时间升序）。
   复现时按冻结顺序取回，不按当前数据重排。

## 哪些值来自前端、哪些来自后端

标签文案（`SEVERITY_META.label`、`FINDING_CATEGORY_LABEL` 等）在前端是常量表。
后端**要自己有一份** —— 因为它要把这些文案写进导出的 Markdown 与 PDF。
但两份必须一致，所以这里的每个表都在注释里标了它对应的前端来源，
而 `scripts/verify_report_parity.py` 会拿前端源码来对账（见该脚本）。

> ⚠️ 与"授权声明"「provider 默认地址」是同一类约束：
> **两端必须逐字一致的常量**。这是第三处。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.core.vocab import FindingCategory, FindingStatus, Severity, Verification

# ══════════════════════════════════════════════════════════
# 常量表 —— 逐字对齐前端
# ══════════════════════════════════════════════════════════

# 来自 `data/findings.ts: SEVERITY_ORDER`
# 顺序**就是**严重度降序，后面排序直接用它。
SEVERITY_ORDER: tuple[str, ...] = (
    Severity.CRITICAL.value,
    Severity.HIGH.value,
    Severity.MEDIUM.value,
    Severity.LOW.value,
    Severity.INFO.value,
)

# 来自 `data/findings.ts: SEVERITY_META`（label + definition）
SEVERITY_LABEL: dict[str, str] = {
    Severity.CRITICAL.value: "严重",
    Severity.HIGH.value: "高危",
    Severity.MEDIUM.value: "中危",
    Severity.LOW.value: "低危",
    Severity.INFO.value: "提示",
}

SEVERITY_DEFINITION: dict[str, str] = {
    Severity.CRITICAL.value: "无需任何前置条件即可利用，应当按已发生的事件处置。",
    Severity.HIGH.value: "在真实条件下可直接利用。",
    Severity.MEDIUM.value: "可被利用，但需要一个攻击者通常无法控制的前置条件。",
    Severity.LOW.value: "加固类问题，单独看直接影响很小。",
    Severity.INFO.value: "仅为观察项，未证明存在实际影响。",
}

# 来自 `data/finding.ts: FINDING_CATEGORY_LABEL`
CATEGORY_LABEL: dict[str, str] = {
    FindingCategory.INJECTION.value: "注入",
    FindingCategory.BROKEN_ACCESS_CONTROL.value: "访问控制缺陷",
    FindingCategory.AUTH.value: "认证与会话",
    FindingCategory.XSS.value: "跨站脚本",
    FindingCategory.SSRF.value: "服务端请求伪造",
    FindingCategory.MISCONFIGURATION.value: "配置缺陷",
    FindingCategory.INFO_DISCLOSURE.value: "信息泄露",
    FindingCategory.CRYPTO.value: "加密问题",
}

# 来自 `data/finding.ts: FINDING_STATUS_LABEL`
STATUS_LABEL: dict[str, str] = {
    FindingStatus.NEW.value: "待确认",
    FindingStatus.CONFIRMED.value: "已确认",
    FindingStatus.FIXED.value: "已修复",
    FindingStatus.FALSE_POSITIVE.value: "误报",
    FindingStatus.RETESTING.value: "复测中",
    FindingStatus.CLOSED.value: "已关闭",
}

# 来自 `data/finding.ts: VERIFICATION_LABEL`
VERIFICATION_LABEL: dict[str, str] = {
    Verification.NONE.value: "未验证",
    Verification.PARTIAL.value: "部分验证",
    Verification.VERIFIED.value: "已验证 PoC",
}

# 来自 `data/scan.ts: SKILLS`
SKILL_LABEL: dict[str, str] = {
    "web": "Web 应用",
    "api": "API 接口",
    "container": "容器配置",
    "cloud": "云配置",
    "dependencies": "依赖漏洞",
    "secrets": "密钥泄露",
}


# ══════════════════════════════════════════════════════════
# 范围与选项
# ══════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class ReportOptions:
    """来自 `data/report.ts: ReportOptions`。四个开关，默认值见 `DEFAULT_OPTIONS`。"""

    include_poc: bool = False
    include_reasoning: bool = False
    only_confirmed: bool = False
    include_remediation: bool = True


# 来自 `data/report.ts: DEFAULT_REPORT_OPTIONS`
DEFAULT_OPTIONS = ReportOptions()


# ══════════════════════════════════════════════════════════
# IR 的数据形状（用 dict 而不是 dataclass）
# ══════════════════════════════════════════════════════════
#
# 用 dict 而不是 dataclass 是刻意的：这份 IR 的**权威定义在前端**
# （`report-document.ts` 的 TypeScript interface），而它的形状还会随前端演进。
# 用 dict 时，"多一个字段"是加法；用 dataclass 时，每加一个字段
# 都要同时改后端的两处，而漏改的那处会以"字段丢失"的形式暴露在渲染时。
# 校验交给 `schemas.py` 的 Pydantic 模型（它只做出参校验，不定义形状）。


def build_document(
    *,
    findings: list[Any],
    scans: list[Any],
    scope: dict[str, Any],
    options: ReportOptions,
    title: str,
    now: datetime,
    report_id: str | None = None,
    provisional: bool = False,
    excluded_count: int | None = None,
) -> dict[str, Any]:
    """组装报告文档 —— 对应前端 `buildReportDocument`。

    纯函数：不读数据库、不取当前时间（`now` 显式传入）、不做 IO。
    这样它能被独立验证，也保证"同一次生成的元信息与条目编号稳定"。

    `excluded_count` 传入时**优先用它**（复现历史报告时必传）——
    理由见模块 docstring 第 2 条。
    """
    from app.core.ids import report_code

    in_scope = findings
    selected = _apply_options(in_scope, options)
    excluded = excluded_count if excluded_count is not None else len(in_scope) - len(selected)
    ordered = sort_for_report(selected)
    entries = [_to_entry(finding, index, options) for index, finding in enumerate(ordered, start=1)]
    stats = summarize(selected)

    return {
        "meta": {
            # ⚠️ 草稿（provisional）时是**空串** —— 编号由生成动作分配，
            # 生成之前它不存在。渲染器见到空串显示"生成后分配"。
            "code": "" if provisional else (report_code(now) if report_id is None else report_id),
            "provisional": provisional,
            "title": title,
            "scope": describe_scope(scope, scans),
            "generatedAt": _iso(now),
            "options": {
                "includePoc": options.include_poc,
                "includeReasoning": options.include_reasoning,
                "onlyConfirmed": options.only_confirmed,
                "includeRemediation": options.include_remediation,
            },
            "excludedCount": excluded,
        },
        "verdict": risk_verdict(stats),
        "stats": stats,
        # 只含有值的档，且按 SEVERITY_ORDER 排好（对应前端
        # `.filter(s => (stats.bySeverity[s] ?? 0) > 0)`）
        "distribution": [
            {"severity": severity, "label": SEVERITY_LABEL[severity], "count": stats["bySeverity"][severity]}
            for severity in SEVERITY_ORDER
            if stats["bySeverity"].get(severity)
        ],
        "entries": entries,
        "method": _build_method(selected, scans, scope),
        "severityDefinitions": [
            {
                "severity": severity,
                "label": SEVERITY_LABEL[severity],
                "definition": SEVERITY_DEFINITION[severity],
            }
            for severity in SEVERITY_ORDER
        ],
    }


# ══════════════════════════════════════════════════════════
# 纯函数 —— 逐字对应 report-build.ts
# ══════════════════════════════════════════════════════════


def apply_options(findings: list[Any], options: ReportOptions) -> list[Any]:
    """对应 `report-build.ts: applyOptions`。

    ★ 「只含已确认」保留 **confirmed + fixed**，不只是 confirmed：

    * `confirmed` = 已确认存在；
    * `fixed` = **曾经确认过、现在修了** —— 从交付角度它仍然发生并处理过，
      把它剔掉会让报告显示"什么都没发现"，那是失真；
    * `new`（待确认）与 `false_positive`（误报）才是该排除的：
      前者还没定论，后者已判定不是问题。

    给客户的报告里放这两种是失职。
    """
    if not options.only_confirmed:
        return findings
    # ★ 用「排除」而不是「白名单」（任务 10 修正）。
    #
    # 原来是 `status in (CONFIRMED, FIXED)`。状态从 4 个涨到 6 个之后，
    # 这个白名单会**静默漏掉** `retesting` 与 `closed` —— 而这两个明明
    # 都是确认过的（它们只能从 confirmed 之后流转过来）。给客户的报告里
    # 少放两条真问题，这种错误没有任何人会当场发现。
    #
    # 语义上真正要排除的只有两类：**还没确认的**（`new`）和**判定为误报的**。
    # 写成排除式之后，将来再加状态也不会漏 —— 白名单每加一个状态就要
    # 有人记得回来改一次，而排除式只在"新增一类不该进报告的东西"时才需要动。
    return [
        finding
        for finding in findings
        if finding.status
        not in (FindingStatus.NEW.value, FindingStatus.FALSE_POSITIVE.value)
    ]


# 内部别名，保持调用点短
_apply_options = apply_options


def sort_for_report(findings: list[Any]) -> list[Any]:
    """对应 `report-build.ts: sortForReport`。

    **永远按严重度降序**，同级按发现时间**升序**（先发现的在前）。

    不提供排序选项 —— 报告不是列表视图，它有一个天然正确的顺序。
    给管理层看的文档里出现"按时间排序"只会让人漏掉严重问题。

    ⚠️ 同级用**升序**，与列表页的"最近在前"**相反**：
    报告叙述的是"我们依次发现了什么"。
    """
    rank = {severity: index for index, severity in enumerate(SEVERITY_ORDER)}

    def key(finding: Any) -> tuple[int, str]:
        return (rank.get(finding.severity, 99), _iso(finding.discovered_at))

    return sorted(findings, key=key)


def summarize(findings: list[Any]) -> dict[str, Any]:
    """对应 `report-build.ts: summarize`。"""
    by_severity: dict[str, int] = {}
    for finding in findings:
        by_severity[finding.severity] = by_severity.get(finding.severity, 0) + 1

    return {
        "total": len(findings),
        # `Partial`：为 0 的档**键不存在**
        "bySeverity": by_severity,
        # 需要优先处置的：严重 + 高危
        "urgent": by_severity.get(Severity.CRITICAL.value, 0)
        + by_severity.get(Severity.HIGH.value, 0),
        "verified": sum(
            1 for finding in findings if finding.verification == Verification.VERIFIED.value
        ),
        # 已修复 = 修复动作**已经发生**的那几档。
        #
        # ★ 不能只数 `fixed`（任务 10 修正）：用户走完复测流程后状态会变成
        # `retesting` 或 `closed`，报告里的"已修复 N 条"会**随着流程推进而变小** ——
        # 那是明显违背直觉的（事情做得越多，修复数越少）。
        "fixed": sum(
            1
            for finding in findings
            if finding.status
            in (
                FindingStatus.FIXED.value,
                FindingStatus.RETESTING.value,
                FindingStatus.CLOSED.value,
            )
        ),
        "targets": len({finding.target for finding in findings}),
        "scans": len({finding.scan_id for finding in findings}),
    }


def risk_verdict(stats: dict[str, Any]) -> str:
    """对应 `report-build.ts: riskVerdict` —— 措辞逐字一致。

    报告开头需要一句**能被直接引用**的判断，而不是让读者自己对数字。
    措辞逐级收敛，且刻意避免"可能存在"这类无信息量的模糊表述 ——
    有 PoC 就说有，没有就说没有。
    """
    if stats["total"] == 0:
        return "本次范围内未发现安全问题。"
    if stats["urgent"] == 0:
        return (
            f"本次范围内共发现 {stats['total']} 个问题，均不构成直接可利用风险，"
            "建议按常规加固计划处理。"
        )
    if stats["verified"] > 0:
        return (
            f"本次范围内共发现 {stats['total']} 个安全问题，其中 {stats['urgent']} 个为"
            f"严重或高危，已通过可复现的验证脚本确认其中 {stats['verified']} 个，建议优先处置。"
        )
    return (
        f"本次范围内共发现 {stats['total']} 个安全问题，其中 {stats['urgent']} 个为"
        "严重或高危，建议优先处置。"
    )


# ══════════════════════════════════════════════════════════
# 内部：条目裁剪与附录
# ══════════════════════════════════════════════════════════


def _to_entry(finding: Any, index: int, options: ReportOptions) -> dict[str, Any]:
    """对应 `report-document.ts: toEntry`。

    ★ 两个开关的行为差异容易会被做成同一个（前端注释专门写了）：

    * `includePoc` 关掉 → `poc` 置 null，但 **`verificationLabel` 保留**。
      报告仍会写"已验证 PoC"，只是不附脚本 —— **结论不因附带材料而消失**。
    * `includeReasoning` 关掉 → `reasoning` 置 null。判断依据被拿掉后，
      描述与证据仍在，读者仍能理解问题是什么。
    """
    timeline = list(getattr(finding, "timeline", None) or [])
    return {
        "index": index,
        "findingId": finding.id,
        "title": finding.title,
        "severity": finding.severity,
        "severityLabel": SEVERITY_LABEL.get(finding.severity, finding.severity),
        "categoryLabel": CATEGORY_LABEL.get(finding.category, finding.category),
        "target": finding.target,
        "affected": list(finding.detail_affected or []),
        "statusLabel": STATUS_LABEL.get(finding.status, finding.status),
        "verificationLabel": VERIFICATION_LABEL.get(finding.verification, finding.verification),
        "discoveredAt": format_date(finding.discovered_at),
        "description": finding.detail_description,
        "reasoning": finding.detail_reasoning if options.include_reasoning else None,
        "evidence": finding.detail_evidence,
        "poc": finding.poc if options.include_poc else None,
        "remediation": finding.remediation if options.include_remediation else None,
        # 只收**人工**动作，不收 agent 的过程事件：
        # 这里回答的是「这个结论被人怎么处理过」，那是交付报告里客户真正要看的一栏。
        "dispositions": [
            {"at": format_date(entry.at), "summary": _disposition_summary(entry)}
            for entry in timeline
            if entry.actor == "user"
        ],
    }


def _disposition_summary(entry: Any) -> str:
    """对应 `report-document.ts: dispoSummary`。

    时间线里的 summary 是站内用的（"管理员会话：待确认 → 已确认"），
    直接搬进报告会带着界面引号与操作人前缀，读起来像把 GUI 贴进了文档。
    这里改写成报告口吻的完整句。
    """
    if entry.kind == "status_changed" and entry.to_status:
        return f"处置状态更新为「{STATUS_LABEL.get(entry.to_status, entry.to_status)}」"
    return entry.summary


def _build_method(findings: list[Any], scans: list[Any], scope: dict[str, Any]) -> dict[str, Any]:
    """对应 `report-document.ts: buildMethod`（附录 A）。

    为什么必须有一节：报告里全是结论，读者无从判断这些结论的分量。
    "用哪些能力测了什么、覆盖多久"决定了"没写的问题是不是真的没有"。
    """
    scan_ids = {finding.scan_id for finding in findings}
    by_id = {scan.id: scan for scan in scans}

    if scan_ids:
        relevant = [by_id[scan_id] for scan_id in scan_ids if scan_id in by_id]
    elif scope.get("kind") == "scan":
        relevant = [by_id[scope["scanId"]]] if scope.get("scanId") in by_id else []
    else:
        relevant = []

    skill_ids = {skill for scan in relevant for skill in (scan.skills or [])}
    times = sorted(_iso(finding.discovered_at) for finding in findings)

    # ★ 覆盖目标**以"测了什么"为准，而不是"从中发现了什么"**。
    #
    # 这两个在多数报告里恰好相等，于是很容易写成从 findings 里取 ——
    # 但一旦报告没收录任何条目（比如"只含已确认"把待确认与误报全筛掉了），
    # 从 findings 取会得到空，封面于是写着"涉及目标 0 个"，
    # 而实际上那个目标**被测过、且这次确实是干净的**。
    # "0 个目标"传达的是"这次什么都没测"，与事实完全相反。
    targets_from_findings = sorted({finding.target for finding in findings})
    targets = (
        targets_from_findings
        if targets_from_findings
        else sorted({scan.target for scan in relevant})
    )

    return {
        "targets": targets,
        "scans": [
            {
                "id": scan.id,
                "name": scan.name,
                "target": scan.target,
                "finishedAt": _iso(scan.finished_at) if scan.finished_at else None,
                # ★ 带上状态，让附录 A 能如实标注"这次扫描未跑完"。
                # 被终止的扫描会产出真实发现，那些发现值得纳入报告 ——
                # 但读者有权知道**覆盖是不完整的**。
                "status": scan.status,
            }
            for scan in relevant
        ],
        "skills": [
            SKILL_LABEL[skill] for skill in SKILL_LABEL if skill in skill_ids
        ],
        "span": {"from": format_date(times[0]), "to": format_date(times[-1])} if times else None,
    }


def coverage_note(status: str) -> str:
    """对应 `report-document.ts: coverageNote` —— 措辞逐字一致。

    放在 IR 层而不是各渲染器里：预览与 Markdown 必须给出**同一句话**，
    各写一份就是两个事实来源的起点。
    """
    return {
        "completed": "完整",
        "aborted": "不完整（已终止）",
        "running": "不完整（进行中）",
        "failed": "未产出结果",
    }.get(status, "未知")


def describe_scope(scope: dict[str, Any], scans: list[Any]) -> str:
    """对应 `data/report.ts: describeScope`。"""
    kind = scope.get("kind")
    if kind == "scan":
        name = next(
            (scan.name for scan in scans if scan.id == scope.get("scanId")),
            scope.get("scanId"),
        )
        return f"扫描任务「{name}」"
    if kind == "target":
        return f"目标 {scope.get('target')}"
    if kind == "range":
        return f"{format_date(scope.get('from'))} 至 {format_date(scope.get('to'))}"
    return ""


def format_date(value: Any) -> str:
    """对应 `data/report.ts: formatDate` —— 「2026年2月14日」。

    报告里不用 `2026-02-14`：那是机器格式。
    """
    parsed = _parse(value)
    if parsed is None:
        return str(value or "")
    return f"{parsed.year}年{parsed.month}月{parsed.day}日"


def format_datetime(value: Any) -> str:
    """对应 `data/report.ts: formatDateTime` —— 「2026年2月14日 15:30」。"""
    parsed = _parse(value)
    if parsed is None:
        return str(value or "")
    return f"{format_date(value)} {parsed.hour:02d}:{parsed.minute:02d}"


def _parse(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    # 显示用本地时区 —— 报告是给人看的，"发现时间"应该是读者所在时区。
    return parsed.astimezone()


def _iso(value: Any) -> str:
    """ISO 8601 带时区，与契约 §1.1 的统一格式一致。"""
    from app.core.timeutil import to_iso

    if isinstance(value, datetime):
        return to_iso(value)
    return str(value or "")


__all__ = [
    "CATEGORY_LABEL",
    "DEFAULT_OPTIONS",
    "ReportOptions",
    "SEVERITY_DEFINITION",
    "SEVERITY_LABEL",
    "SEVERITY_ORDER",
    "SKILL_LABEL",
    "STATUS_LABEL",
    "VERIFICATION_LABEL",
    "build_document",
    "coverage_note",
    "describe_scope",
    "format_date",
    "format_datetime",
    "risk_verdict",
    "sort_for_report",
    "summarize",
]
