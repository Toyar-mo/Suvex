"""验证上下文（v1.83，对齐 ARTEX 的 `get_finding_check_context`）。

## 它回答什么

"判这条 finding 之前，应该看到哪些**已记录的**证据？" —— 同一次扫描的
黑板事实、同扫描的相关发现、以及该类别的反证清单。

## 诚实边界

- 只返回**已记录**的事实；没有记录不等于没测过（与覆盖度同一条口径）。
- 不做证据推断、不把同类 finding 当成"同类漏洞"的证明。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.facts.service import load_facts
from app.findings import verifier
from app.findings.models import Finding


def verification_context(db: Session, finding: Finding) -> dict[str, Any]:
    """组装一条 finding 的验证上下文。纯读，不写库。"""
    facts = [
        {
            "factKey": fact.fact_key,
            "category": fact.category,
            "summary": fact.summary,
            "confidence": fact.confidence,
            "trustLevel": fact.trust_level or "",
            "updatedAt": fact.updated_at.isoformat() if fact.updated_at else "",
        }
        for fact in load_facts(db, finding.scan_id)
    ]

    related = list(
        db.execute(
            select(Finding)
            .where(
                Finding.scan_id == finding.scan_id,
                Finding.id != finding.id,
                Finding.deleted_at.is_(None),
            )
            .order_by(Finding.discovered_at.desc())
            .limit(10)
        ).scalars()
    )

    return {
        "finding": {
            "id": finding.id,
            "title": finding.title,
            "severity": finding.severity,
            "cwe": finding.cwe,
            "target": finding.target,
            "verification": finding.verification,
            "status": finding.status,
        },
        "facts": facts,
        "relatedFindings": [
            {
                "id": item.id,
                "title": item.title,
                "severity": item.severity,
                "verification": item.verification,
            }
            for item in related
        ],
        "checks": verifier.checks_for(finding.cwe, finding.category),
        "note": (
            "只列已记录的事实与同扫描发现；没有记录不等于没测过。"
            "反证清单是判 verified 前应逐条确认的对照。"
        ),
    }


__all__ = ["verification_context"]
