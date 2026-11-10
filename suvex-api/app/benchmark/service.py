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

from app.benchmark.models import BenchmarkCase, BenchmarkRun, QualitySnapshot
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
            "totalTokens": _rate_change(
                b.get("cost", {}).get("totalTokens"),
                a.get("cost", {}).get("totalTokens"),
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


# -- 评测用例(v1.81) ------------------------------------------
#
# 这一层**不触发任何扫描/解题**。判定只读关联对象：一次"跑基准"=
# 把每个用例的当前状态采集为一行结果。原因见用例模型与服务文件头：
# 评测不该偷偷花掉用户的扫描额度。

CASE_KINDS = ("ctf", "scan")


def create_case(
    db: Session,
    *,
    suite: str,
    name: str,
    kind: str,
    challenge_id: str | None = None,
    scan_id: str | None = None,
    expect: dict[str, Any] | None = None,
    enabled: bool = True,
) -> BenchmarkCase:
    """登记一条用例。缺关联对象时直接拒绝（否则永远判 error）。"""
    from app.core.errors import ApiError

    if kind not in CASE_KINDS:
        raise ApiError.validation(f"kind 只能是 {' / '.join(CASE_KINDS)}")
    if kind == "ctf" and not challenge_id:
        raise ApiError.validation("ctf 用例需要 challengeId")
    if kind == "scan" and not scan_id:
        raise ApiError.validation("scan 用例需要 scanId")
    row = BenchmarkCase(
        id=new_id("case"),
        suite=(suite or "default").strip()[:80],
        name=name.strip()[:200],
        kind=kind,
        challenge_id=challenge_id,
        scan_id=scan_id,
        expect=expect or {},
        enabled=enabled,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_cases(db: Session, *, suite: str | None = None) -> list[BenchmarkCase]:
    stmt = select(BenchmarkCase).order_by(BenchmarkCase.created_at.desc())
    if suite:
        stmt = stmt.where(BenchmarkCase.suite == suite)
    return list(db.execute(stmt).scalars())


def delete_case(db: Session, case_id: str) -> bool:
    row = db.get(BenchmarkCase, case_id)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def _judge(db: Session, case: BenchmarkCase) -> tuple[str, dict[str, Any]]:
    """读关联对象当前状态，给 `passed/failed/pending/error`。"""
    if case.kind == "ctf":
        from app.kernel.models import KernelChallenge

        challenge = db.get(KernelChallenge, case.challenge_id) if case.challenge_id else None
        if challenge is None:
            return "error", {"reason": "challenge_missing", "challengeId": case.challenge_id}
        status = {
            "solved": "passed",
            "failed": "failed",
        }.get(challenge.status, "pending")
        return status, {
            "kind": "ctf",
            "challengeId": challenge.id,
            "challengeStatus": challenge.status,
            "candidateCount": challenge.candidate_count,
            "wrongSubmissions": challenge.wrong_submission_count,
        }

    from sqlalchemy import func

    from app.findings.models import Finding
    from app.scans.models import Scan

    scan = db.get(Scan, case.scan_id) if case.scan_id else None
    if scan is None:
        return "error", {"reason": "scan_missing", "scanId": case.scan_id}
    findings = int(
        db.scalar(
            select(func.count()).select_from(Finding).where(Finding.scan_id == scan.id)
        )
        or 0
    )
    detail = {
        "kind": "scan",
        "scanId": scan.id,
        "scanStatus": scan.status,
        "findings": findings,
        "durationSec": scan.duration_sec,
    }
    if scan.status in ("failed", "aborted"):
        return "failed", detail
    if scan.status != "completed":
        return "pending", detail
    minimum = int((case.expect or {}).get("minFindings", 0) or 0)
    return ("passed" if findings >= minimum else "failed"), detail


def run_case(db: Session, case: BenchmarkCase) -> BenchmarkRun:
    status, detail = _judge(db, case)
    row = BenchmarkRun(
        id=new_id("run"),
        case_id=case.id,
        status=status,
        detail=detail,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def run_suite(db: Session, suite: str) -> list[BenchmarkRun]:
    cases = list(
        db.execute(
            select(BenchmarkCase).where(
                BenchmarkCase.suite == suite,
                BenchmarkCase.enabled.is_(True),
            )
        ).scalars()
    )
    return [run_case(db, case) for case in cases]


def latest_runs(db: Session, *, suite: str | None = None) -> list[BenchmarkRun]:
    """每个用例最近一次结果（按用例 id 分组取最新）。"""
    stmt = (
        select(BenchmarkRun)
        .order_by(BenchmarkRun.collected_at.desc())
        .limit(500)
    )
    rows = list(db.execute(stmt).scalars())
    if suite:
        case_ids = {case.id for case in list_cases(db, suite=suite)}
        rows = [row for row in rows if row.case_id in case_ids]
    seen: set[str] = set()
    latest: list[BenchmarkRun] = []
    for row in rows:
        if row.case_id in seen:
            continue
        seen.add(row.case_id)
        latest.append(row)
    return latest
