"""质量指标快照（v1.81）。

## 它回答什么问题

「这套 agent 到底在变好还是变差」——不能只看漏洞数。一个只报 3 条但
全部可复现的扫描，比报 30 条、其中 20 条是误报的扫描更有价值。市场头部
（XBOW / AIxCC）都用同一组维度说明能力：**成功率、可复现率、覆盖度、成本**。

本模块先落能从现有数据算出来的四项：

* **扫描**：总数、终态分布、已完成的平均耗时；
* **发现**：总数、按证据强度分布（verified/partial/none）、误报数/率；
* **CTF**：总数、已解、解题率、候选产出、错误提交；
* **覆盖度**：已记录的 surface 数与目标数（来自 `record_coverage` 落库）。

## 刻意不做的

* **不把「未解出」当失败**：CTF 的 `failed` 是"这一轮没解出"，不是
  "agent 能力差"；两者混在一起会让指标失去解释力。
* **不推断缺口**：只数**有记录**的面。没有记录不等于没测，见
  `coverage_for_target` 的同一条说明。
* **不混入成本**：当前没有把 token/费用落成可查询的列；等有了再加，
  不在这里用估算值假装精确。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from app.ctf_race.cost_tracker import resolve_pricing
from app.facts.models import ScanFact
from app.findings.models import Finding
from app.kernel.models import KernelChallenge, KernelSolveUsage
from app.scans.models import Scan

#: 终态扫描的三种状态（`running`/`queued` 不计入完成率的分母之外的桶）。
_TERMINAL_SCAN_STATUSES = ("completed", "failed", "aborted")


def _rate(numerator: int, denominator: int) -> float | None:
    """分母为 0 时返回 None —— 「无法计算」不能显示成 0%。"""
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def quality_snapshot(db: Session, *, since: datetime | None = None) -> dict[str, Any]:
    """从现有表聚合一份质量快照。只读，不做任何写入。

    `since` 非空时只统计该时刻之后产生的数据（扫描按 created_at、
    发现按 discovered_at、覆盖记录按 updated_at）。CTF 题目是**全量**
    口径 —— 题目表没有可靠的"这个时间之后解出"列，混入会撒谎。
    """
    scan_stmt = select(Scan.status, func.count(), func.avg(Scan.duration_sec)).group_by(
        Scan.status
    )
    if since is not None:
        scan_stmt = scan_stmt.where(Scan.created_at >= since)
    scan_rows = db.execute(scan_stmt).all()
    scans_by_status = {status: count for status, count, _avg in scan_rows}
    total_scans = sum(scans_by_status.values())
    completed_avg = next(
        (avg for status, _count, avg in scan_rows if status == "completed" and avg is not None),
        None,
    )

    verification_stmt = select(Finding.verification, func.count()).group_by(
        Finding.verification
    )
    if since is not None:
        verification_stmt = verification_stmt.where(Finding.discovered_at >= since)
    verification_rows = db.execute(verification_stmt).all()
    by_verification = {value: count for value, count in verification_rows}
    total_findings = sum(by_verification.values())
    false_positive_stmt = select(func.count()).select_from(Finding).where(
        Finding.status == "false_positive"
    )
    if since is not None:
        false_positive_stmt = false_positive_stmt.where(Finding.discovered_at >= since)
    false_positive = int(db.scalar(false_positive_stmt) or 0)

    challenge_rows = db.execute(
        select(KernelChallenge.status, func.count()).group_by(KernelChallenge.status)
    ).all()
    by_challenge_status = {status: count for status, count in challenge_rows}
    total_challenges = sum(by_challenge_status.values())
    solved = by_challenge_status.get("solved", 0)
    candidate_count = int(
        db.scalar(select(func.coalesce(func.sum(KernelChallenge.candidate_count), 0))) or 0
    )
    wrong_submissions = int(
        db.scalar(select(func.coalesce(func.sum(KernelChallenge.wrong_submission_count), 0))) or 0
    )

    surface_stmt = select(func.count()).select_from(ScanFact).where(
        ScanFact.category == "surface"
    )
    if since is not None:
        surface_stmt = surface_stmt.where(ScanFact.updated_at >= since)
    surfaces = int(db.scalar(surface_stmt) or 0)
    target_stmt = select(func.count(distinct(ScanFact.target))).select_from(ScanFact).where(
        ScanFact.category == "surface"
    )
    if since is not None:
        target_stmt = target_stmt.where(ScanFact.updated_at >= since)
    coverage_targets = int(db.scalar(target_stmt) or 0)

    # -- 成本(v1.81) ------------------------------------------
    #
    # token 只统计 `KernelSolveUsage`（解题侧）。扫描侧由 strix 自己记账，
    # 当前没有可查询的用量列 —— 与其把扫描成本显示成 0，不如只报解题侧，
    # 并在前端写明口径。
    usage_stmt = select(
        KernelSolveUsage.model,
        func.count(),
        func.coalesce(func.sum(KernelSolveUsage.prompt_tokens), 0),
        func.coalesce(func.sum(KernelSolveUsage.completion_tokens), 0),
        func.coalesce(func.sum(KernelSolveUsage.total_tokens), 0),
    ).group_by(KernelSolveUsage.model)
    if since is not None:
        usage_stmt = usage_stmt.where(KernelSolveUsage.created_at >= since)
    usage_rows = db.execute(usage_stmt).all()

    unreported_stmt = select(func.count()).select_from(KernelSolveUsage).where(
        KernelSolveUsage.usage_reported.is_(False)
    )
    if since is not None:
        unreported_stmt = unreported_stmt.where(KernelSolveUsage.created_at >= since)
    unreported_calls = int(db.scalar(unreported_stmt) or 0)

    prompt_tokens = sum(int(row[2]) for row in usage_rows)
    completion_tokens = sum(int(row[3]) for row in usage_rows)
    total_tokens = sum(int(row[4]) for row in usage_rows)
    calls = sum(int(row[1]) for row in usage_rows)
    usd = 0.0
    unpriced_tokens = 0
    for _model, _count, prompt, completion, total in usage_rows:
        price = resolve_pricing(_model)
        if price is None:
            unpriced_tokens += int(total)
            continue
        usd += (
            int(prompt) * price.get("input", 0.0)
            + int(completion) * price.get("output", 0.0)
        ) / 1_000_000

    return {
        "cost": {
            "calls": calls,
            "unreportedCalls": unreported_calls,
            "promptTokens": prompt_tokens,
            "completionTokens": completion_tokens,
            "totalTokens": total_tokens,
            "usd": round(usd, 4),
            "unpricedTokens": unpriced_tokens,
        },
        "scans": {
            "total": total_scans,
            "completed": scans_by_status.get("completed", 0),
            "failed": scans_by_status.get("failed", 0),
            "aborted": scans_by_status.get("aborted", 0),
            "avgDurationSec": round(float(completed_avg), 2) if completed_avg is not None else None,
        },
        "findings": {
            "total": total_findings,
            "verified": by_verification.get("verified", 0),
            "needsFollowUp": by_verification.get("partial", 0),
            "none": by_verification.get("none", 0),
            "falsePositive": false_positive,
            "falsePositiveRate": _rate(false_positive, total_findings),
        },
        "challenges": {
            "total": total_challenges,
            "solved": solved,
            "solveRate": _rate(solved, total_challenges),
            "candidateCount": candidate_count,
            "wrongSubmissions": wrong_submissions,
        },
        "coverage": {
            "surfaces": surfaces,
            "targets": coverage_targets,
        },
    }


__all__ = ["quality_snapshot"]
