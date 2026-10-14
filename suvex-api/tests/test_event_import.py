"""strix 事件导入器：幂等、游标、落库优先（任务 4 要求 ③）。

这些测试盖的是任务原话里的那条：

> 事件要先落库（哪怕前端已经收到了实时推送），这样用户刷新页面或稍后
> 重新打开这次扫描时，能从历史记录里完整回放，不是"错过了就没了"。

所以核心断言是两条：**重复导入不产生重复事件**、**已经入库的能完整读出来**。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.events.models import ActivityEvent, Agent
from app.scans import importer
from app.scans.models import Scan
from tests.factories import make_scan


# 与 test_strix_events 里同一套造库逻辑。单独写一份而不是共享：
# 两个测试文件的关注点不同（一个测解析、一个测导入），
# 共享一个 fixture 会让它们的演进互相牵扯。
def _make_agents_db(run_dir: Path, rows: list[tuple[str, dict]], *, run_name: str = "run-1") -> Path:
    state = run_dir / "strix_runs" / run_name / ".state"
    state.mkdir(parents=True, exist_ok=True)
    db = state / "agents.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sessions ("
        " session_id TEXT PRIMARY KEY,"
        " created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
        " updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_messages ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " session_id TEXT NOT NULL,"
        " message_data TEXT NOT NULL,"
        " created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
        " FOREIGN KEY (session_id) REFERENCES sessions (session_id) ON DELETE CASCADE)"
    )
    for session_id, payload in rows:
        conn.execute(
            "INSERT INTO agent_messages (session_id, message_data) VALUES (?, ?)",
            (session_id, json.dumps(payload, ensure_ascii=False)),
        )
    conn.commit()
    conn.close()
    return db


def _append_rows(run_dir: Path, rows: list[tuple[str, dict]], *, run_name: str = "run-1") -> None:
    """往已有库里追加（模拟扫描还在写）。"""
    conn = sqlite3.connect(run_dir / "strix_runs" / run_name / ".state" / "agents.db")
    for session_id, payload in rows:
        conn.execute(
            "INSERT INTO agent_messages (session_id, message_data) VALUES (?, ?)",
            (session_id, json.dumps(payload, ensure_ascii=False)),
        )
    conn.commit()
    conn.close()


def _event_count(db: Session, scan_id: str) -> int:
    return int(
        db.execute(
            select(func.count()).select_from(ActivityEvent).where(ActivityEvent.scan_id == scan_id)
        ).scalar_one()
    )


@pytest.fixture()
def scan(db_session: Session, tmp_path: Path) -> Scan:
    """一条指向临时运行目录的扫描记录。

    直接插库不走 `POST /scans`：这条测试关心的是**导入器**，
    不是扫描的创建流程，也不希望调度器真的把进程拉起来。
    """
    created = make_scan(db_session, status="completed")
    created.run_dir = str(tmp_path / "scan")
    created.runner = "fake"
    db_session.commit()
    Path(created.run_dir).mkdir(parents=True, exist_ok=True)
    return created


# ══════════════════════════════════════════════════════════
# 基本导入
# ══════════════════════════════════════════════════════════


def test_imports_events_and_lands_them_in_the_database(db_session: Session, scan: Scan) -> None:
    """★ 事件**落库** —— 这是"刷新页面还能看到"的前提。"""
    _make_agents_db(
        Path(scan.run_dir),
        [
            ("a-root", {"type": "function_call", "call_id": "c1", "name": "think",
                        "arguments": json.dumps({"thought": "先侦察"})}),
            ("a-root", {"type": "function_call_output", "call_id": "c1",
                        "output": json.dumps({"success": True})}),
        ],
    )

    written = importer.import_events(db_session, scan)

    assert written == 1
    assert _event_count(db_session, scan.id) == 1
    event = db_session.execute(
        select(ActivityEvent).where(ActivityEvent.scan_id == scan.id)
    ).scalar_one()
    assert event.type == "reasoning"
    assert event.detail["text"] == "先侦察"
    # 溯源列写进去了 —— 它是幂等键，也是游标
    assert event.source_row_id == 2


def test_reimporting_is_idempotent(db_session: Session, scan: Scan) -> None:
    """★★ **反复导入不产生重复事件**。

    这是导入器最要紧的性质：它会被每一轮调度都调用一次
    （扫描在跑，每几秒读一次）。没有幂等，活动流里每条都会出现几十次 ——
    而那个现象看起来像"agent 疯了"，完全指错方向。
    """
    _make_agents_db(
        Path(scan.run_dir),
        [
            ("a", {"type": "function_call", "call_id": "c1", "name": "web_search",
                   "arguments": json.dumps({"q": "x"})}),
            ("a", {"type": "function_call_output", "call_id": "c1",
                   "output": json.dumps({"success": True})}),
        ],
    )

    first = importer.import_events(db_session, scan)
    second = importer.import_events(db_session, scan)
    third = importer.import_events(db_session, scan)

    assert (first, second, third) == (1, 0, 0)
    assert _event_count(db_session, scan.id) == 1


def test_cursor_only_reads_new_rows(db_session: Session, scan: Scan) -> None:
    """★ 游标来自**数据库**，不是内存。

    内存游标在服务重启后就丢了，那意味着每次重启都要重读整个库 ——
    而重复读的代价不只是慢：去重逻辑一旦有洞，重复事件就真的落库了。
    """
    _make_agents_db(
        Path(scan.run_dir),
        [
            ("a", {"type": "function_call", "call_id": "c1", "name": "think",
                   "arguments": json.dumps({"thought": "一"})}),
            ("a", {"type": "function_call_output", "call_id": "c1",
                   "output": json.dumps({"success": True})}),
        ],
    )
    assert importer.import_events(db_session, scan) == 1

    # 扫描继续写：模拟"又产生了两条"
    _append_rows(
        Path(scan.run_dir),
        [
            ("a", {"type": "function_call", "call_id": "c2", "name": "think",
                   "arguments": json.dumps({"thought": "二"})}),
            ("a", {"type": "function_call_output", "call_id": "c2",
                   "output": json.dumps({"success": True})}),
        ],
    )

    # 换一个会话（模拟服务重启）：游标必须仍然有效
    assert importer.import_events(db_session, scan) == 1
    assert _event_count(db_session, scan.id) == 2


def test_progressive_import_matches_single_pass(db_session: Session, scan: Scan, tmp_path: Path) -> None:
    """★ 边跑边导 与 最后一次性导，结果必须**一致**。

    不一致的话，"实时看到的"与"刷新后回放的"就是两份不同的历史 ——
    而任务 4 的验收标准同时要求这两件事。
    """
    rows = [
        ("a", {"type": "function_call", "call_id": f"c{i}", "name": "think",
               "arguments": json.dumps({"thought": f"步骤 {i}"})})
        for i in range(4)
    ]
    _make_agents_db(Path(scan.run_dir), rows[:2])
    importer.import_events(db_session, scan)
    _append_rows(Path(scan.run_dir), rows[2:])
    importer.import_events(db_session, scan)

    progressive = [
        event.detail["text"]
        for event in db_session.execute(
            select(ActivityEvent).where(ActivityEvent.scan_id == scan.id).order_by(ActivityEvent.seq)
        ).scalars()
    ]

    assert progressive == ["步骤 0", "步骤 1", "步骤 2", "步骤 3"]


def test_missing_run_dir_is_not_an_error(db_session: Session, scan: Scan) -> None:
    """没有运行目录（比如扫描还没启动就失败了）→ 导入 0 条，不抛异常。"""
    scan.run_dir = str(Path(scan.run_dir) / "nonexistent")
    db_session.commit()

    assert importer.import_events(db_session, scan) == 0


def test_scan_without_run_dir_is_skipped(db_session: Session, scan: Scan) -> None:
    scan.run_dir = None
    db_session.commit()

    assert importer.import_events(db_session, scan) == 0


def test_empty_database_is_not_an_error(db_session: Session, scan: Scan) -> None:
    """库建了但还没有行（刚启动）→ 0 条。

    报错的话调度器每轮都会记异常，真正的异常会被淹掉。
    """
    _make_agents_db(Path(scan.run_dir), [])

    assert importer.import_events(db_session, scan) == 0


# ══════════════════════════════════════════════════════════
# agent 名册
# ══════════════════════════════════════════════════════════


def test_agents_are_created_from_the_roster(db_session: Session, scan: Scan) -> None:
    """事件挂到的 agent 从 `agents.json` 的名册里建出来。

    `activity_events.agent_id` 是**非空**的（契约 §2.5），
    所以每条外部事件都必须先落一个 agent 行。
    """
    state = Path(scan.run_dir) / "strix_runs" / "run-1" / ".state"
    _make_agents_db(
        Path(scan.run_dir),
        [("a-root", {"type": "function_call", "call_id": "c1", "name": "think",
                     "arguments": json.dumps({"thought": "x"})})],
    )
    (state / "agents.json").write_text(
        json.dumps(
            {
                "statuses": {"a-root": "completed"},
                "names": {"a-root": "root"},
                "parent_of": {},
                "errors": {},
            }
        ),
        encoding="utf-8",
    )

    importer.import_events(db_session, scan)

    agents = list(
        db_session.execute(select(Agent).where(Agent.scan_id == scan.id)).scalars()
    )
    assert len(agents) == 1
    # 状态来自权威的 agents.json（completed → done）
    assert agents[0].status == "done"
    assert agents[0].id.startswith("sa_")


def test_agent_is_reused_across_imports(db_session: Session, scan: Scan) -> None:
    """★ 同一个外部 agent 反复导入必须命中**同一行**。

    映射不稳定的话，名册会被拆成好几份重复的影子 agent ——
    而界面上会看到同一个人出现五次。
    """
    _make_agents_db(
        Path(scan.run_dir),
        [
            ("a-root", {"type": "function_call", "call_id": "c1", "name": "think",
                        "arguments": json.dumps({"thought": "一"})}),
        ],
    )
    importer.import_events(db_session, scan)
    _append_rows(
        Path(scan.run_dir),
        [("a-root", {"type": "function_call", "call_id": "c2", "name": "think",
                     "arguments": json.dumps({"thought": "二"})})],
    )
    importer.import_events(db_session, scan)

    agents = list(db_session.execute(select(Agent).where(Agent.scan_id == scan.id)).scalars())
    assert len(agents) == 1
    # event_count 累加
    assert agents[0].event_count == 2


# ══════════════════════════════════════════════════════════
# 收尾补齐
# ══════════════════════════════════════════════════════════


def test_backfill_reads_everything_left(db_session: Session, scan: Scan) -> None:
    """★ 收尾补齐：扫描结束后把剩下的**全部**读进来。

    为什么需要：进程在退出前可能刚写完最后几行，而那一轮导入还没跑到。
    不补齐，"历史扫描完整回放"就永远差最后几条。
    """
    rows = [
        ("a", {"type": "function_call", "call_id": f"c{i}", "name": "think",
               "arguments": json.dumps({"thought": str(i)})})
        for i in range(3)
    ]
    _make_agents_db(Path(scan.run_dir), rows)

    total = importer.backfill_after_finish(db_session, scan)

    assert total == 3
    assert _event_count(db_session, scan.id) == 3


def test_backfill_is_idempotent_with_normal_import(db_session: Session, scan: Scan) -> None:
    """先边跑边导、结束后再补齐 —— 不产生重复。"""
    _make_agents_db(
        Path(scan.run_dir),
        [("a", {"type": "function_call", "call_id": "c1", "name": "think",
                "arguments": json.dumps({"thought": "x"})})],
    )
    importer.import_events(db_session, scan)

    assert importer.backfill_after_finish(db_session, scan) == 0
    assert _event_count(db_session, scan.id) == 1


# ══════════════════════════════════════════════════════════
# 回放
# ══════════════════════════════════════════════════════════


def test_events_are_replayable_in_order(db_session: Session, scan: Scan) -> None:
    """★ 历史回放：按 `seq` 升序读出来就是当时发生的顺序。

    SSE 的 `Last-Event-ID` 补发与 `/events/recent` 的 seed 都依赖这个顺序。
    """
    rows = [
        ("a", {"type": "function_call", "call_id": f"c{i}", "name": "think",
               "arguments": json.dumps({"thought": f"第 {i} 步"})})
        for i in range(5)
    ]
    _make_agents_db(Path(scan.run_dir), rows)
    importer.import_events(db_session, scan)

    events = list(
        db_session.execute(
            select(ActivityEvent).where(ActivityEvent.scan_id == scan.id).order_by(ActivityEvent.seq)
        ).scalars()
    )

    assert [event.detail["text"] for event in events] == [f"第 {i} 步" for i in range(5)]
    # seq 是单调递增的 —— 补发游标就是它
    assert [event.seq for event in events] == sorted(event.seq for event in events)


def test_all_eight_detail_kinds_stay_within_the_contract_vocabulary(
    db_session: Session, scan: Scan
) -> None:
    """★ 导入的事件类型必须落在契约 §2.5 的 8 种里。

    冒出第 9 种的话，前端 `EventRow` 的 8 个分支一个都匹配不上 ——
    表现是活动流里出现一行空白，而没有任何报错。
    """
    from app.core.vocab import EventDetailKind

    _make_agents_db(
        Path(scan.run_dir),
        [
            ("a", {"type": "function_call", "call_id": "c1", "name": "think",
                   "arguments": json.dumps({"thought": "想一下"})}),
            ("a", {"type": "function_call_output", "call_id": "c1",
                   "output": json.dumps({"success": True})}),
            ("a", {"type": "function_call", "call_id": "c2", "name": "web_search",
                   "arguments": json.dumps({"q": "x"})}),
            ("a", {"type": "function_call_output", "call_id": "c2",
                   "output": json.dumps({"success": True})}),
            ("a", {"role": "assistant", "content": "说一句"}),
            ("a", {"type": "function_call", "call_id": "c3", "name": "repeat_request",
                   "arguments": json.dumps({})}),
            ("a", {"type": "function_call_output", "call_id": "c3",
                   "output": json.dumps({"success": False, "error": "失败"})}),
            ("a", {"type": "function_call", "call_id": "c4",
                   "name": "create_vulnerability_report",
                   "arguments": json.dumps({"title": "某问题"})}),
            ("a", {"type": "function_call_output", "call_id": "c4",
                   "output": json.dumps({"success": True})}),
        ],
    )
    importer.import_events(db_session, scan)

    events = list(
        db_session.execute(select(ActivityEvent).where(ActivityEvent.scan_id == scan.id)).scalars()
    )
    allowed = {member.value for member in EventDetailKind}
    assert events
    for event in events:
        assert event.type in allowed, f"type={event.type} 不在契约的 8 种里"
        assert event.detail["kind"] in allowed, f"detail.kind={event.detail['kind']} 不在契约里"
        assert event.detail["kind"] == event.type


# ══════════════════════════════════════════════════════════
# ★★ 毒丸事件：一条坏数据不许拖垮整条事件流
# ══════════════════════════════════════════════════════════


def _poison_rows() -> list[tuple[str, dict]]:
    """三条事件，中间那条的返回值里带 NUL 字节。

    这是**真实发生过**的形状（2026-09-27）：agent 抓 favicon，
    响应体是 PNG，`\\x89PNG\\r\\n\\x1a\\n\\x00` 里的那个 NUL 被记进了
    事件 detail。PostgreSQL 的 text/jsonb 存不了它。
    """
    return [
        # 1) 毒丸之前 —— 正常
        ("a", {"type": "function_call", "call_id": "c1", "name": "think",
               "arguments": json.dumps({"thought": "第一个"})}),
        ("a", {"type": "function_call_output", "call_id": "c1",
               "output": json.dumps({"success": True})}),
        # 2) ★ 毒丸：返回值是二进制
        ("a", {"type": "function_call", "call_id": "c2", "name": "exec_command",
               "arguments": json.dumps({"cmd": "curl -s http://t/favicon.ico"})}),
        ("a", {"type": "function_call_output", "call_id": "c2",
               "output": "HTTP/1.1 200 OK\r\n\r\n\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"}),
        # 3) 毒丸**之后** —— 它有没有被连累，才是这个测试的重点
        ("a", {"type": "function_call", "call_id": "c3", "name": "think",
               "arguments": json.dumps({"thought": "第三个"})}),
        ("a", {"type": "function_call_output", "call_id": "c3",
               "output": json.dumps({"success": True})}),
    ]


def test_event_with_nul_byte_does_not_jam_the_import(db_session: Session, scan: Scan) -> None:
    """★★ 含 NUL 的事件必须**被清洗后入库**，而不是让导入卡死。

    这条测试盯的是实测踩到的那个连环故障：

    * 那一条插不进去 → 游标停在它之前**不再前进**；
    * 同批里已经 add 但未提交的正常事件被一起回滚；
    * 于是那次扫描后面的 100 条事件、连同**漏洞导入**（它排在事件导入
      之后）全部丢失 —— 界面上只表现为"这次没扫到东西"。

    所以断言分三层：**导入不抛异常**、**毒丸之后的事件照样进得来**、
    **存下去的内容里没有 NUL**。
    """
    _make_agents_db(Path(scan.run_dir), _poison_rows())

    # ① 不抛异常（旧代码在这里抛 DataError，被上层吞成 debug 日志）
    written = importer.import_events(db_session, scan)

    # ② 三条都进来了 —— 毒丸被清洗，不是被丢弃，更没有连累后面那条
    assert written == 3, "含 NUL 的事件要么被丢、要么拖垮了同批的其它事件"
    assert _event_count(db_session, scan.id) == 3

    # ③ 落库的内容里没有 NUL（PostgreSQL 存不了它，能查到就说明洗过了）
    events = list(
        db_session.execute(
            select(ActivityEvent)
            .where(ActivityEvent.scan_id == scan.id)
            .order_by(ActivityEvent.source_row_id)
        ).scalars()
    )
    for event in events:
        assert "\x00" not in json.dumps(event.detail, ensure_ascii=False), "detail 里仍有 NUL"

    # ④ 游标走到了最后一行 —— "不再前进"正是旧行为最致命的地方
    assert max(event.source_row_id for event in events) == 6


def test_poison_event_is_idempotent_on_reimport(db_session: Session, scan: Scan) -> None:
    """清洗过的毒丸重复导入不会变成第二条（幂等键仍按原始 row_id 算）。"""
    _make_agents_db(Path(scan.run_dir), _poison_rows())

    assert importer.import_events(db_session, scan) == 3
    assert importer.import_events(db_session, scan) == 0
    assert importer.backfill_after_finish(db_session, scan) == 0
    assert _event_count(db_session, scan.id) == 3
