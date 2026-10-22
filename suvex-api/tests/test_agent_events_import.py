"""自研引擎的事件也要进活动流 —— `agent_run/events.jsonl` → `activity_events`。

## 这个测试守住的是什么

自研引擎（agent 后端）不写 strix 的 sqlite，如果只写 `scan.log`（纯文本），
导入器 `import_events` 读不到任何结构化事件，于是界面上每次 agent 扫描都是
「运行中 · 0 个智能体 / 活动流空白」——**而引擎其实在跑**。

2026-10-21 实测撞到的正是这个：scan.log 里 `[tool_call] run_shell …` 一条条
在出，界面上智能体数一直是 0。这条链修好后：引擎写 JSONL → 导入器按
`seq`（= `source_row_id`）去重后灌进 `activity_events`，活动流/名册/SSE 才活。

三条硬要求（与 strix 路径逐条对齐）：
1. **幂等**：反复导入同一段不产生重复（靠 `seq` + 唯一索引）。
2. **增量**：`seq <= 游标` 的跳过，新事件被追上。
3. **一条坏行不断流**：JSON 半行/坏行跳过，其余照常入库。
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select

from app.events.models import ActivityEvent, Agent
from app.scans import agent_runner, importer
from app.scans.models import Scan
from tests.factories import make_scan


def _write_events(run_dir: Path, records: list[dict]) -> None:
    path = run_dir / "agent_run" / agent_runner.EVENTS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


def _agent_scan(db, tmp_path: Path) -> Scan:
    scan = make_scan(db, status="running")
    scan.runner = agent_runner.RUNNER_NAME
    scan.run_dir = str(tmp_path / "scan")
    db.commit()
    (tmp_path / "scan").mkdir(parents=True, exist_ok=True)
    return scan


def test_constants_agree_between_importer_and_runner() -> None:
    """两份字面量必须一致 —— 否则导入器去找一个引擎根本不写的文件名。"""
    assert importer.AGENT_RUNNER_NAME == agent_runner.RUNNER_NAME
    assert importer.AGENT_EVENTS_FILENAME == agent_runner.EVENTS_FILENAME


def test_agent_events_become_activity_events(db_session, tmp_path) -> None:
    scan = _agent_scan(db_session, tmp_path)
    _write_events(
        Path(scan.run_dir),
        [
            {"seq": 1, "kind": "reasoning", "summary": "先看首页", "payload": {"text": "先看首页"}, "at": "2026-10-21T12:00:00+00:00"},
            {"seq": 2, "kind": "tool_call", "summary": "run_shell curl", "payload": {"tool": "run_shell", "command": "curl x", "output": "ok"}, "at": "2026-10-21T12:00:05+00:00"},
        ],
    )

    written = importer.import_events(db_session, scan)

    assert written == 2
    events = (
        db_session.execute(
            select(ActivityEvent).where(ActivityEvent.scan_id == scan.id).order_by(ActivityEvent.source_row_id)
        ).scalars().all()
    )
    assert [e.source_row_id for e in events] == [1, 2]
    assert events[0].type == "reasoning"
    assert events[1].type == "tool_call"
    # 工具调用的事件必须带出工具名（否则活动流里只知道"调了个工具"）
    assert events[1].detail.get("tool") == "run_shell"


def test_agent_scan_gets_a_self_agent_row(db_session, tmp_path) -> None:
    """必须有且只有一个「自研引擎」智能体 —— 否则界面显示 0 个智能体。"""
    scan = _agent_scan(db_session, tmp_path)
    _write_events(
        Path(scan.run_dir),
        [{"seq": 1, "kind": "tool_call", "summary": "nmap", "payload": {"tool": "run_shell", "command": "nmap", "output": ""}, "at": "2026-10-21T12:00:00+00:00"}],
    )

    importer.import_events(db_session, scan)

    agents = db_session.execute(select(Agent).where(Agent.scan_id == scan.id)).scalars().all()
    assert len(agents) == 1
    assert agents[0].target == scan.target
    assert agents[0].event_count >= 1


def test_import_is_idempotent(db_session, tmp_path) -> None:
    """反复导入同一段不产生重复（`seq` + 唯一索引）。"""
    scan = _agent_scan(db_session, tmp_path)
    _write_events(
        Path(scan.run_dir),
        [{"seq": 1, "kind": "reasoning", "summary": "a", "payload": {}, "at": "2026-10-21T12:00:00+00:00"}],
    )

    first = importer.import_events(db_session, scan)
    second = importer.import_events(db_session, scan)

    assert first == 1
    assert second == 0
    count = len(
        db_session.execute(select(ActivityEvent).where(ActivityEvent.scan_id == scan.id)).scalars().all()
    )
    assert count == 1


def test_import_is_incremental(db_session, tmp_path) -> None:
    """先导一批，再追加一批 —— 只导新的。"""
    scan = _agent_scan(db_session, tmp_path)
    run_dir = Path(scan.run_dir)
    _write_events(run_dir, [{"seq": 1, "kind": "reasoning", "summary": "a", "payload": {}, "at": "2026-10-21T12:00:00+00:00"}])
    assert importer.import_events(db_session, scan) == 1

    # 追加第 2、3 条
    _write_events(
        run_dir,
        [
            {"seq": 1, "kind": "reasoning", "summary": "a", "payload": {}, "at": "2026-10-21T12:00:00+00:00"},
            {"seq": 2, "kind": "reasoning", "summary": "b", "payload": {}, "at": "2026-10-21T12:00:01+00:00"},
            {"seq": 3, "kind": "tool_call", "summary": "c", "payload": {"tool": "run_shell"}, "at": "2026-10-21T12:00:02+00:00"},
        ],
    )
    assert importer.import_events(db_session, scan) == 2


def test_bad_line_does_not_break_the_batch(db_session, tmp_path) -> None:
    """一行坏 JSON 不能让整批断流 —— 其余照常入库。"""
    scan = _agent_scan(db_session, tmp_path)
    path = Path(scan.run_dir) / "agent_run" / agent_runner.EVENTS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"seq": 1, "kind": "reasoning", "summary": "ok", "payload": {}, "at": "2026-10-21T12:00:00+00:00"})
        + "\n{ this is not json\n"
        + json.dumps({"seq": 3, "kind": "reasoning", "summary": "ok2", "payload": {}, "at": "2026-10-21T12:00:02+00:00"})
        + "\n",
        encoding="utf-8",
    )

    written = importer.import_events(db_session, scan)

    assert written == 2  # seq 1 与 3，坏行跳过
    seqs = sorted(
        e.source_row_id
        for e in db_session.execute(select(ActivityEvent).where(ActivityEvent.scan_id == scan.id)).scalars()
    )
    assert seqs == [1, 3]


def test_no_events_file_is_zero_not_error(db_session, tmp_path) -> None:
    """引擎刚启动、还没写事件文件 —— 导 0 条，不是失败。"""
    scan = _agent_scan(db_session, tmp_path)

    assert importer.import_events(db_session, scan) == 0


def test_strix_scan_still_uses_strix_path(db_session, tmp_path) -> None:
    """反向对照：runner 不是 agent 时，绝不能走自研引擎那条（免得读错文件）。"""
    scan = make_scan(db_session, status="running")
    scan.runner = "strix"
    scan.run_dir = str(tmp_path / "scan")
    db_session.commit()
    (tmp_path / "scan").mkdir(parents=True, exist_ok=True)
    # 即便目录里放了一份 events.jsonl，strix 后端也不该读它
    _write_events(
        Path(scan.run_dir),
        [{"seq": 1, "kind": "tool_call", "summary": "x", "payload": {"tool": "run_shell"}, "at": "2026-10-21T12:00:00+00:00"}],
    )

    written = importer.import_events(db_session, scan)

    assert written == 0  # strix 路径读 sqlite，不看 events.jsonl


def test_multi_agent_events_get_separate_roster_rows(db_session, tmp_path) -> None:
    """多智能体：root 与各子 agent 的事件必须分到不同的名册行。

    这是「可解释审计」在多智能体下的底线 —— 如果所有 agent 的事件都挂在
    同一行，复盘时无法分辨某条命令是谁下的。引擎在 payload 里写
    `agent`/`agentName`（见 `multiagent.notify`），导入器据此建不同 Agent。
    """
    scan = _agent_scan(db_session, tmp_path)
    _write_events(
        Path(scan.run_dir),
        [
            {"seq": 1, "kind": "reasoning", "summary": "root 先侦察",
             "payload": {"text": "root 先侦察", "agent": "root", "agentName": "root", "turn": "1"},
             "at": "2026-10-22T12:00:00+00:00"},
            {"seq": 2, "kind": "tool_call", "summary": "root nmap",
             "payload": {"tool": "run_shell", "command": "nmap", "output": "", "agent": "root", "agentName": "root", "turn": "2"},
             "at": "2026-10-22T12:00:05+00:00"},
            {"seq": 3, "kind": "tool_call", "summary": "子 agent 枚举",
             "payload": {"tool": "run_shell", "command": "gobuster", "output": "", "agent": "child-1", "agentName": "API端点枚举", "turn": "1"},
             "at": "2026-10-22T12:00:10+00:00"},
        ],
    )

    written = importer.import_events(db_session, scan)

    assert written == 3
    agents = db_session.execute(select(Agent).where(Agent.scan_id == scan.id)).scalars().all()
    assert len(agents) == 2, "root 与子 agent 必须是两个名册行（否则审计时分不清谁做的）"
    # 每个 agent 的事件计数加起来等于总事件数
    assert sum(a.event_count for a in agents) == 3
    # 事件必须各归各的 agent，不能全挂一行
    rows = db_session.execute(
        select(ActivityEvent).where(ActivityEvent.scan_id == scan.id)
    ).scalars().all()
    by_agent: dict[str, int] = {}
    for r in rows:
        by_agent[r.agent_id] = by_agent.get(r.agent_id, 0) + 1
    assert sorted(by_agent.values()) == [1, 2], "root 两条、子 agent 一条"


def test_events_without_agent_key_fall_back_to_self(db_session, tmp_path) -> None:
    """老数据（无 agent/agentName 键）退回单一「自研引擎」，不建多个行。"""
    scan = _agent_scan(db_session, tmp_path)
    _write_events(
        Path(scan.run_dir),
        [
            {"seq": 1, "kind": "reasoning", "summary": "a", "payload": {"text": "a"}, "at": "2026-10-22T12:00:00+00:00"},
            {"seq": 2, "kind": "tool_call", "summary": "b", "payload": {"tool": "run_shell"}, "at": "2026-10-22T12:00:01+00:00"},
        ],
    )

    importer.import_events(db_session, scan)

    agents = db_session.execute(select(Agent).where(Agent.scan_id == scan.id)).scalars().all()
    assert len(agents) == 1
    assert agents[0].event_count == 2
