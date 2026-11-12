"""同题对比 Suvex 执行图与 ARTeX 任务（P3 度量脚本）。

两边数据库里都要有**同一个 objective/goal**的任务才有可比性。脚本不会
自己造题、也不会调模型：它只做对账，避免把"两个系统各跑各的"包装成对比。

用法（在能连到两个库的机器上）：

    python scripts/compare_agent_tasks.py \
      --suvex-url postgresql+psycopg://... \
      --artex-url postgresql+psycopg://... \
      --objective "对 https://example.com 做一次完整渗透"

输出 Markdown；`--json` 输出机器可读结果。匹配失败时明确列出两边的
objective，而不是静默输出 0。
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import create_engine, text


@dataclass
class Metrics:
    system: str
    task_id: str
    title: str
    objective: str
    status: str
    completed: bool
    goals_total: int
    goals_met: int
    intents_total: int
    intents_done: int
    intents_blocked: int
    findings: int
    tokens: int
    duration_seconds: float | None


def _suvex(engine, objective: str) -> Metrics | None:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT id, title, objective, status, created_at, completed_at
                FROM agent_tasks
                WHERE btrim(objective) = btrim(:objective)
                ORDER BY created_at DESC LIMIT 1
                """
            ),
            {"objective": objective},
        ).mappings().first()
        if row is None:
            return None
        counts = conn.execute(
            text(
                """
                SELECT
                  (SELECT count(*) FROM agent_goals WHERE task_id=:id) AS goals_total,
                  (SELECT count(*) FROM agent_goals WHERE task_id=:id AND status='met') AS goals_met,
                  (SELECT count(*) FROM agent_intents WHERE task_id=:id) AS intents_total,
                  (SELECT count(*) FROM agent_intents WHERE task_id=:id AND status='done') AS intents_done,
                  (SELECT count(*) FROM agent_intents WHERE task_id=:id AND status='blocked') AS intents_blocked
                """
            ),
            {"exploration_id": row["exploration_id"]},
        ).mappings().one()
    completed = row["status"] == "completed"
    duration = (
        (row["completed_at"] - row["created_at"]).total_seconds()
        if row["completed_at"] and row["created_at"]
        else None
    )
    return Metrics(
        system="suvex",
        task_id=row["id"],
        title=row["title"],
        objective=row["objective"],
        status=row["status"],
        completed=completed,
        goals_total=counts["goals_total"],
        goals_met=counts["goals_met"],
        intents_total=counts["intents_total"],
        intents_done=counts["intents_done"],
        intents_blocked=counts["intents_blocked"],
        findings=0,  # 任务与 findings 尚未硬关联；按 scope/target 统计会混入别的任务
        tokens=0,
        duration_seconds=duration,
    )


def _artex(engine, objective: str) -> Metrics | None:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT id, exploration_id, name, goal, status, created_at, completed_at
                FROM tasks
                WHERE btrim(coalesce(goal,'')) = btrim(:objective)
                   OR btrim(coalesce(name,'')) = btrim(:objective)
                ORDER BY created_at DESC LIMIT 1
                """
            ),
            {"objective": objective},
        ).mappings().first()
        if row is None:
            return None
        nodes = conn.execute(
            text(
                """
                SELECT
                  count(*) FILTER (WHERE kind IN ('goal')) AS goals_total,
                  count(*) FILTER (WHERE kind IN ('goal') AND state='done') AS goals_met,
                  count(*) FILTER (WHERE kind IN ('intent')) AS intents_total,
                  count(*) FILTER (WHERE kind IN ('intent') AND state='done') AS intents_done,
                  count(*) FILTER (WHERE kind IN ('intent') AND state IN ('blocked','exhausted')) AS intents_blocked
                FROM exploration_nodes WHERE exploration_id=:exploration_id
                """
            ),
            {"id": row["id"]},
        ).mappings().one()
        findings = conn.execute(
            text("SELECT count(*) FROM findings WHERE task_id=:id"), {"id": row["id"]}
        ).scalar_one()
        tokens = conn.execute(
            text(
                "SELECT coalesce(sum(input_tokens+output_tokens),0) FROM llm_usage WHERE task_id=:id"
            ),
            {"id": str(row["id"])},
        ).scalar_one()
    duration = (
        (row["completed_at"] - row["created_at"]).total_seconds()
        if row["completed_at"] and row["created_at"]
        else None
    )
    return Metrics(
        system="artex",
        task_id=str(row["id"]),
        title=row["name"] or "",
        objective=row["goal"] or "",
        status=row["status"],
        completed=row["status"] == "completed",
        goals_total=int(nodes["goals_total"] or 0),
        goals_met=int(nodes["goals_met"] or 0),
        intents_total=int(nodes["intents_total"] or 0),
        intents_done=int(nodes["intents_done"] or 0),
        intents_blocked=int(nodes["intents_blocked"] or 0),
        findings=int(findings or 0),
        tokens=int(tokens or 0),
        duration_seconds=duration,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suvex-url", default=os.environ.get("SUPEX_DATABASE_URL"))
    parser.add_argument("--artex-url", default=os.environ.get("ARTEX_DATABASE_URL"))
    parser.add_argument("--objective", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if not args.suvex_url or not args.artex_url:
        parser.error("需要 --suvex-url 与 --artex-url（或对应环境变量）")

    rows = [
        _suvex(create_engine(args.suvex_url), args.objective),
        _artex(create_engine(args.artex_url), args.objective),
    ]
    found = [row for row in rows if row is not None]
    if len(found) < 2:
        missing = [name for name, row in zip(("suvex", "artex"), rows) if row is None]
        print(f"没有找到同题任务。缺：{', '.join(missing)}；objective={args.objective!r}")
        return 2
    if args.json:
        print(json.dumps([asdict(row) for row in found], ensure_ascii=False, indent=2))
        return 0

    print(f"# 同题对比：{args.objective}\n")
    print("| 指标 | Suvex | ARTeX |")
    print("| --- | --- | --- |")
    left, right = found
    fields: list[tuple[str, Any, Any]] = [
        ("完成", left.completed, right.completed),
        ("状态", left.status, right.status),
        ("目标达成", f"{left.goals_met}/{left.goals_total}", f"{right.goals_met}/{right.goals_total}"),
        ("意图完成", f"{left.intents_done}/{left.intents_total}", f"{right.intents_done}/{right.intents_total}"),
        ("意图阻塞", left.intents_blocked, right.intents_blocked),
        ("漏洞数", left.findings, right.findings),
        ("token", left.tokens, right.tokens),
        (
            "耗时秒",
            round(left.duration_seconds or 0, 1),
            round(right.duration_seconds or 0, 1),
        ),
    ]
    for label, a, b in fields:
        print(f"| {label} | {a} | {b} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
