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
