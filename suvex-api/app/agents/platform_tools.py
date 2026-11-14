"""Worker/planner 的平台读工具：把 Suvex 的扫描、漏洞、资产接进执行图。"""

from __future__ import annotations

from typing import Any


async def list_findings(ctx: Any, args: dict[str, Any]) -> str:
    from app.findings import service as finding_service

    q = str(args.get("q") or "").strip() or None
    severity = str(args.get("severity") or "").strip() or None
    status = str(args.get("status") or "").strip() or None
    limit = min(max(int(args.get("limit") or 20), 1), 100)
    rows = finding_service.list_findings(
        ctx.db, q=q, severity=severity, status=status, limit=limit
    )
    if not rows:
        return "没有匹配的漏洞。"
    lines = [f"漏洞 {len(rows)} 条："]
    for row in rows:
        lines.append(
            f"- `{row.id}` [{row.severity}/{row.status}] {row.title} @ {row.target}"
        )
    return "\n".join(lines)


async def list_assets(ctx: Any, args: dict[str, Any]) -> str:
    from app.assets import service as asset_service

    limit = min(max(int(args.get("limit") or 30), 1), 100)
    rows, truncated, total = asset_service.list_assets(ctx.db, limit=limit)
    if not rows:
        return "资产清单为空。"
    lines = [f"资产 {len(rows)}/{total} 个" + ("（已截断）" if truncated else "") + "："]
    for row in rows:
        label = row.get("target") or row.get("name") or "?"
        findings = row.get("findingCount") or 0
        open_count = row.get("openFindingCount") or 0
        lines.append(f"- {label}（漏洞 {findings}，未处置 {open_count}）")
    return "\n".join(lines)


async def list_scans(ctx: Any, args: dict[str, Any]) -> str:
    from app.scans import service as scan_service

    limit = min(max(int(args.get("limit") or 10), 1), 50)
    rows = scan_service.list_scans(ctx.db, sort="recent")[:limit]
    if not rows:
        return "还没有扫描任务。"
    lines = [f"扫描 {len(rows)} 条："]
    for row in rows:
        lines.append(f"- `{row.id}` [{row.status}] {row.target} — {row.name or '—'}")
    return "\n".join(lines)


async def read_skill(ctx: Any, args: dict[str, Any]) -> str:
    from app.kernel import skills as skill_mod

    name = str(args.get("name") or "").strip()
    if not name:
        return "失败：需要 name。"
    text = skill_mod.load_skill_text(name)
    return text[:12000]


__all__ = ["list_assets", "list_findings", "list_scans", "read_skill"]


async def save_report(ctx: Any, args: dict[str, Any]) -> str:
    """把漏洞冻结成平台报告（历史报告列表里可见）。"""
    from app.reports import document as report_ir
    from app.reports import service as report_service

    title = str(args.get("title") or "").strip()
    if not title:
        return "失败：需要 title。"
    finding_ids = args.get("finding_ids") or args.get("findingIds")
    if isinstance(finding_ids, str):
        finding_ids = [item.strip() for item in finding_ids.split(",") if item.strip()]
    finding_ids = [str(item).strip() for item in (finding_ids or []) if str(item).strip()]
    scan_id = str(args.get("scan_id") or args.get("scanId") or "").strip()
    target = str(args.get("target") or "").strip()

    if finding_ids:
        scope = report_service.Scope(kind="findings", finding_ids=tuple(finding_ids))
    elif scan_id:
        scope = report_service.Scope(kind="scan", scan_id=scan_id)
    elif target:
        scope = report_service.Scope(kind="target", target=target)
    else:
        return "失败：需要 finding_ids、scan_id、target 三者之一来限定报告范围。"

    options = report_ir.ReportOptions(
        include_poc=True,
        include_reasoning=True,
        only_confirmed=bool(args.get("only_confirmed") or args.get("onlyConfirmed")),
        include_remediation=True,
    )
    try:
        report = report_service.generate(
            ctx.db, title=title[:300], scope=scope, options=options, fmt="markdown"
        )
    except Exception as exc:  # noqa: BLE001 - 业务拒绝要变成可读结果
        return f"失败：生成报告被拒（{exc}）。"
    return (
        f"报告已生成：`{report.code}`（{report.id}），"
        f"收录 {report.finding_count} 条，排除 {report.excluded_count} 条。"
        "在「报告」页可以看到。"
    )


async def request_scope_approval(ctx: Any, args: dict[str, Any]) -> str:
    """登记一个待批准目标；批准前不得主动探测。"""
    from sqlalchemy import select

    from app.chat.prompts import may_call_tool
    from app.scans import scope_service
    from app.scans import service as scan_service
    from app.scans.models import Scan

    if not may_call_tool("request_scope_approval", getattr(ctx, "identity", None)):
        return "失败：当前账号没有 `scan` 权限，不能登记待批准目标。"

    target = str(args.get("target") or "").strip()
    if not target:
        return "失败：需要 target（要新扩的网段/地址）。"
    reason = str(args.get("reason") or "").strip()
    scan_id = str(args.get("scan_id") or args.get("scanId") or "").strip()
    if not scan_id:
        task_id = getattr(getattr(ctx, "session", None), "agent_task_id", None)
        if task_id:
            scan_id = (
                ctx.db.execute(
                    select(Scan.id)
                    .where(Scan.agent_task_id == task_id)
                    .order_by(Scan.created_at.desc())
                ).scalars().first()
                or ""
            )
    if not scan_id:
        return "失败：需要 scan_id，当前会话也没有挂到某次扫描上。"
    try:
        scan = scan_service.get_scan(ctx.db, scan_id)
    except Exception as exc:  # noqa: BLE001
        return f"失败：找不到扫描 `{scan_id}`（{exc}）。"

    row = scope_service.add(ctx.db, scan_id=scan.id, target=target, reason=reason)
    return (
        f"已登记待批准目标 `{row.target}`（记录 `{row.id}`，状态 {row.status}）。"
        "等人工在扫描页批准后，才能对它主动探测。"
    )


__all__ = [
    "list_assets",
    "list_findings",
    "list_scans",
    "read_skill",
    "request_scope_approval",
    "save_report",
]
