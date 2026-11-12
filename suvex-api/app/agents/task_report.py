"""执行图 → Markdown 任务报告。"""

from __future__ import annotations

from typing import Any

from app.core.timeutil import now_utc

_STATUS = {
    "planning": "规划中",
    "running": "运行中",
    "completed": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
    "open": "待处理",
    "met": "已达成",
    "abandoned": "已放弃",
    "done": "完成",
    "exhausted": "已探尽",
    "blocked": "阻塞",
    "stopped": "已停止",
}


def _cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def render_task_report(data: dict[str, Any]) -> str:
    task = data["task"]
    goals = data["goals"]
    intents = data["intents"]
    runs = data["runs"]
    scans = data.get("scans", [])

    lines = [
        f"# {task.title}",
        "",
        f"- 任务：`{task.id}`",
        f"- 状态：{_STATUS.get(task.status, task.status)}",
        f"- 目标：{task.objective or '—'}",
        f"- 范围/约束：{task.scope or '—'}",
        f"- 生成时间：{now_utc().strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "## 目标",
        "",
    ]
    if goals:
        lines += ["| # | 目标 | 状态 | 证据 |", "| --- | --- | --- | --- |"]
        for goal in goals:
            lines.append(
                f"| {goal.seq} | {_cell(goal.text)} | {_STATUS.get(goal.status, goal.status)} | {_cell(goal.evidence)} |"
            )
    else:
        lines.append("（无）")

    lines += ["", "## 意图与结果", ""]
    if intents:
        for intent in intents:
            lines += [
                f"### {intent.seq}. {intent.summary}",
                "",
                f"- 状态：{_STATUS.get(intent.status, intent.status)}",
                f"- 优先级：{intent.priority}",
                f"- 领取会话：`{intent.worker_session_id or '—'}`",
                "",
            ]
            if intent.result:
                lines += ["```text", intent.result, "```", ""]
            else:
                lines.append("（还没有写回结果）\n")
    else:
        lines.append("（无）")

    lines += ["", "## 运行记录", ""]
    if runs:
        lines += ["| 角色 | 状态 | 会话 |", "| --- | --- | --- |"]
        for run in runs:
            lines.append(
                f"| {_cell(run.role)} | {_STATUS.get(run.status, run.status)} | `{run.session_id or '—'}` |"
            )
    else:
        lines.append("（无）")
    lines += ["", "## 关联扫描", ""]
    if scans:
        lines += ["| 扫描 | 目标 | 状态 |", "| --- | --- | --- |"]
        for scan in scans:
            lines.append(
                f"| `{scan.id}` | {_cell(scan.target)} | {_STATUS.get(scan.status, scan.status)} |"
            )
    else:
        lines.append("（无）")

    lines += ["", "---", "", "由 Suvex 执行图生成。结果来自 worker 写回，未写回的方向不代表已排除。", ""]
    return "\n".join(lines)


__all__ = ["render_task_report"]
