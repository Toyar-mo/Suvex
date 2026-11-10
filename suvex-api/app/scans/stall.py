"""扫描侧重复动作检测（v1.83）。

聊天侧已有 `MAX_REPEATED_ACTIONS`；扫描侧一直只有“时间上的停滞”
（`stallInfo`），没有“动作上的原地打转”。这里补上：
同一扫描最近 `WINDOW` 条 `tool_call` 里，连续相同 summary 达到
`REPEAT_THRESHOLD` 即视为停滞。

只读 events，不修改扫描；是否终止由人决定。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.events.models import ActivityEvent
from app.scans.models import Scan

REPEAT_THRESHOLD = 5
WINDOW = 20


def stalled_scans(db: Session) -> list[dict]:
    scans = list(
        db.execute(select(Scan).where(Scan.status == "running")).scalars()
    )
    out: list[dict] = []
    for scan in scans:
        seq = list(
            db.execute(
                select(ActivityEvent.summary)
                .where(
                    ActivityEvent.scan_id == scan.id,
                    ActivityEvent.type == "tool_call",
                )
                .order_by(ActivityEvent.seq.desc())
                .limit(WINDOW)
            ).scalars()
        )
        seq.reverse()
        best = 0
        run = 1
        sample = ""
        for index in range(1, len(seq)):
            if seq[index] == seq[index - 1]:
                run += 1
                if run > best:
                    best = run
                    sample = seq[index]
            else:
                run = 1
        if len(seq) == 1:
            best = 1
        if best >= REPEAT_THRESHOLD:
            out.append(
                {
                    "scanId": scan.id,
                    "name": scan.name,
                    "target": scan.target,
                    "repeats": best,
                    "sample": sample[:200],
                }
            )
    return out


__all__ = ["REPEAT_THRESHOLD", "WINDOW", "stalled_scans"]
