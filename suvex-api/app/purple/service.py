"""紫队规则匹配与 SIEM 导出（v1.82）。"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.core.ids import new_id
from app.findings.models import Finding
from app.purple.models import PurpleRule

EXPORT_FORMATS = ("jsonl", "cef")


def list_rules(db: Session, *, enabled_only: bool = False) -> list[PurpleRule]:
    stmt = select(PurpleRule).order_by(PurpleRule.created_at.desc())
    if enabled_only:
        stmt = stmt.where(PurpleRule.enabled.is_(True))
    return list(db.execute(stmt).scalars())


def create_rule(
    db: Session,
    *,
    name: str,
    severity: str = "medium",
    cwe: str | None = None,
    title_contains: str | None = None,
    target_contains: str | None = None,
    enabled: bool = True,
) -> PurpleRule:
    if not name.strip():
        raise ApiError.validation("规则名不能为空")
    row = PurpleRule(
        id=new_id("rule"),
        name=name.strip()[:200],
        severity=(severity or "medium").strip()[:16],
        cwe=(cwe or "").strip() or None,
        title_contains=(title_contains or "").strip() or None,
        target_contains=(target_contains or "").strip() or None,
        enabled=enabled,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def delete_rule(db: Session, rule_id: str) -> bool:
    row = db.get(PurpleRule, rule_id)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def matches(rule: PurpleRule, finding: Finding) -> bool:
    """规则命中判定。空字段视为"不约束"，全部有值时必须全部命中。"""
    if rule.cwe and (finding.cwe or "").strip().lower() != rule.cwe.strip().lower():
        return False
    if rule.title_contains and rule.title_contains.lower() not in finding.title.lower():
        return False
    if rule.target_contains and rule.target_contains.lower() not in finding.target.lower():
        return False
    # 没有任何约束的规则会命中全部发现 —— 那是误配，不是"全选"。
    return bool(rule.cwe or rule.title_contains or rule.target_contains)


def evaluate(db: Session) -> list[dict[str, Any]]:
    """用启用的规则扫一遍未删除的 findings，返回有命中的规则。"""
    rules = list_rules(db, enabled_only=True)
    findings = list(
        db.execute(select(Finding).where(Finding.deleted_at.is_(None))).scalars()
    )
    hits: list[dict[str, Any]] = []
    for rule in rules:
        matched = [f for f in findings if matches(rule, f)]
        if not matched:
            continue
        hits.append(
            {
                "rule": rule,
                "count": len(matched),
                "samples": [
                    {
                        "id": f.id,
                        "title": f.title,
                        "severity": f.severity,
                        "target": f.target,
                        "cwe": f.cwe,
                    }
                    for f in matched[:5]
                ],
            }
        )
    hits.sort(key=lambda hit: hit["count"], reverse=True)
    return hits


def _cef_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def export_events(db: Session, fmt: str) -> str:
    """把命中导出成 SIEM 可直接吃的行。

    `jsonl`：每行一个命中（含样本）；`cef`：每行一个样本（CEF 是
    "单事件"格式，一条规则挂多个样本必须拆行）。
    """
    if fmt not in EXPORT_FORMATS:
        raise ApiError.validation(f"format 只能是 {' / '.join(EXPORT_FORMATS)}")
    hits = evaluate(db)
    lines: list[str] = []
    if fmt == "jsonl":
        for hit in hits:
            rule = hit["rule"]
            lines.append(
                json.dumps(
                    {
                        "rule": rule.name,
                        "severity": rule.severity,
                        "cwe": rule.cwe,
                        "count": hit["count"],
                        "samples": hit["samples"],
                    },
                    ensure_ascii=False,
                )
            )
    else:
        for hit in hits:
            rule = hit["rule"]
            for sample in hit["samples"]:
                lines.append(
                    "CEF:0|Suvex|Purple|1.82|{name}|{name}|{sev}|"
                    "src={target} msg={title} cs1={cwe} cs1Label=CWE".format(
                        name=_cef_escape(rule.name),
                        sev=_cef_escape(rule.severity),
                        target=_cef_escape(sample["target"]),
                        title=_cef_escape(sample["title"]),
                        cwe=_cef_escape(sample.get("cwe") or ""),
                    )
                )
    return "\n".join(lines) + ("\n" if lines else "")
