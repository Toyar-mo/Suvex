"""复测/验证角色用的漏洞上下文与结论写回工具。

此前 retester/verifier 的提示词要求读漏洞、写结论，但 Suvex 没有对应工具；
这里补最小闭环：读一条漏洞的真实证据，按复测/验证结论流转状态并留痕。
"""

from __future__ import annotations

import json
from typing import Any

from app.core.vocab import FindingStatus
from app.findings import service as finding_service


def _finding_payload(finding: Any) -> dict[str, Any]:
    return {
        "id": finding.id,
        "title": finding.title,
        "target": finding.target,
        "severity": finding.severity,
        "category": finding.category,
        "status": finding.status,
        "verification": finding.verification,
        "discoveredAt": finding.discovered_at.isoformat() if finding.discovered_at else None,
        "description": finding.detail_description,
        "reasoning": finding.detail_reasoning,
        "evidence": finding.detail_evidence,
        "poc": finding.poc,
        "remediation": finding.remediation,
        "affected": list(finding.detail_affected or []),
        "timeline": [
            {
                "at": row.at.isoformat() if row.at else None,
                "actor": row.actor,
                "kind": row.kind,
                "summary": row.summary,
                "from": row.from_status,
                "to": row.to_status,
            }
            for row in (finding.timeline or [])
        ],
    }


async def get_finding_context(ctx: Any, args: dict[str, Any]) -> str:
    finding_id = str(args.get("finding_id") or "").strip()
    if not finding_id:
        return "失败：需要 finding_id。"
    try:
        finding = finding_service.get_finding(ctx.db, finding_id)
    except Exception as exc:  # noqa: BLE001 - 把业务原因交回模型
        return f"失败：{exc}"
    return json.dumps(_finding_payload(finding), ensure_ascii=False, indent=2)


async def record_finding_result(ctx: Any, args: dict[str, Any]) -> str:
    """写回复测/验证结论；`inconclusive` 不改状态，只留结论文本。

    verdict：
      - reproduced → confirmed（仍可复现）
      - fixed → fixed（复测已修复）
      - verified → confirmed（独立验证成立）
      - false_positive → false_positive（必须有反证）
      - inconclusive → 不改状态
    """
    finding_id = str(args.get("finding_id") or "").strip()
    verdict = str(args.get("verdict") or "").strip().lower()
    evidence = str(args.get("evidence") or "").strip()
    if not finding_id:
        return "失败：需要 finding_id。"
    if verdict not in {"reproduced", "fixed", "verified", "false_positive", "inconclusive"}:
        return "失败：verdict 必须是 reproduced / fixed / verified / false_positive / inconclusive。"
    if not evidence:
        return "失败：必须提供 evidence（实际请求/响应/命令输出）。"
    from app.chat.engine import _need_scope

    _need_scope(ctx, "record_finding_result")
    try:
        finding = finding_service.get_finding(ctx.db, finding_id)
    except Exception as exc:  # noqa: BLE001
        return f"失败：{exc}"

    if verdict == "inconclusive":
        finding_service.record_verification_note(
            ctx.db, finding_id=finding_id, note=f"[inconclusive] {evidence}"
        )
        return f"漏洞 `{finding_id}` 结论记为 inconclusive（未改变状态），证据已写入时间线。"

    target = {
        "reproduced": FindingStatus.CONFIRMED.value,
        "verified": FindingStatus.CONFIRMED.value,
        "fixed": FindingStatus.FIXED.value,
        "false_positive": FindingStatus.FALSE_POSITIVE.value,
    }[verdict]
    if finding.status == target:
        finding_service.record_verification_note(
            ctx.db, finding_id=finding_id, note=f"[{verdict}] {evidence}"
        )
        return f"漏洞 `{finding_id}` 已经是 {target}，本次证据已写入时间线。"

    try:
        changed, _ = finding_service.change_status(
            ctx.db,
            ids=[finding_id],
            target=target,
            note=f"[{verdict}] {evidence}",
            identity=ctx.identity,
        )
    except Exception as exc:  # noqa: BLE001 - 把业务校验原因交回模型
        ctx.db.rollback()
        return f"失败：{exc}"
    return f"漏洞 `{finding_id}` 已写回：{verdict} → {target}（{len(changed)} 条变更）。"


__all__ = ["get_finding_context", "record_finding_result"]
