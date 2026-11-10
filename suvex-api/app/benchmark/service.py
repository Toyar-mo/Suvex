"""评测快照与对比（v1.81）。

## 为什么需要

`/overview/quality` 回答"现在怎么样"，但"变好还是变差"需要**两个时刻**。
本模块只做三件事：捕获快照、列快照、比较两份快照。它不触发任何扫描/解题
—— 评测运行由人决定，这里只记录与对比。

## 对比的口径

只比较**同一来源、同一时间窗**的快照。两次快照之间如果改了统计口径，
diff 会看起来像能力变化；所以 `compare` 里把两份 payload 原样带回来，
读的人可以自己核对窗口（`?since=` 在捕获时已固化进 payload 的前提里）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.benchmark.models import QualitySnapshot
from app.core.ids import new_id
from app.overview.quality import quality_snapshot


def capture(db: Session, *, label: str = "") -> QualitySnapshot:
    """把当前质量指标落成一行快照。"""
    row = QualitySnapshot(
        id=new_id("snap"),
        label=label.strip()[:200],
        payload=quality_snapshot(db),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_snapshots(db: Session, *, limit: int = 50) -> list[QualitySnapshot]:
    return list(
        db.execute(
            select(QualitySnapshot)
            .order_by(QualitySnapshot.captured_at.desc())
            .limit(limit)
        ).scalars()
    )


def get_snapshot(db: Session, snapshot_id: str) -> QualitySnapshot | None:
    return db.get(QualitySnapshot, snapshot_id)


def _rate_change(before: float | None, after: float | None) -> float | None:
    """两个比率相减；任一为 None（无法计算）时返回 None。"""
    if before is None or after is None:
        return None
    return round(after - before, 4)


def _share(verified: int, total: int) -> float | None:
    if total <= 0:
        return None
    return round(verified / total, 4)


def compare(before: QualitySnapshot, after: QualitySnapshot) -> dict[str, Any]:
    """比较两份快照，返回指标差（after - before）。

    正数不一定"更好"：误报率上升是坏事、覆盖率上升是好事。这里只给差值，
    好坏由读的人按指标语义判断 —— 与 `severity` 不替人下结论同一条纪律。
    """
    b = before.payload or {}
    a = after.payload or {}
    b_findings = b.get("findings", {})
    a_findings = a.get("findings", {})

    return {
        "before": {
            "id": before.id,
            "label": before.label,
            "capturedAt": before.captured_at.isoformat() if before.captured_at else "",
            "metrics": b,
        },
        "after": {
            "id": after.id,
            "label": after.label,
            "capturedAt": after.captured_at.isoformat() if after.captured_at else "",
            "metrics": a,
        },
        "delta": {
            "solveRate": _rate_change(
                b.get("challenges", {}).get("solveRate"),
                a.get("challenges", {}).get("solveRate"),
            ),
            "falsePositiveRate": _rate_change(
                b_findings.get("falsePositiveRate"),
                a_findings.get("falsePositiveRate"),
            ),
            "verifiedShare": _rate_change(
                _share(b_findings.get("verified", 0), b_findings.get("total", 0)),
                _share(a_findings.get("verified", 0), a_findings.get("total", 0)),
            ),
            "coverageSurfaces": (
                None
                if not b.get("coverage") or not a.get("coverage")
                else a["coverage"].get("surfaces", 0) - b["coverage"].get("surfaces", 0)
            ),
            "avgScanDurationSec": (
                None
                if b.get("scans", {}).get("avgDurationSec") is None
                or a.get("scans", {}).get("avgDurationSec") is None
                else round(
                    a["scans"]["avgDurationSec"] - b["scans"]["avgDurationSec"], 2
                )
            ),
        },
    }


__all__ = ["capture", "compare", "get_snapshot", "list_snapshots"]
