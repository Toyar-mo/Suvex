"""strix 事件解析层（任务 4 要求 ①）。

## 这些测试的依据是什么

**不是猜的格式。** 每一条断言都能追到 strix 1.6.2 的源码：

| 被测行为 | 依据（strix 源码） |
| --- | --- |
| `agent_messages` 的表结构与列名 | `agents/memory/sqlite_session.py` 的 `CREATE TABLE` |
| `session_id` 列里装的是 agent_id | `interface/tui/history.py` 的查询把它读出来当 agent_id |
| 只读打开、`mode=ro` | `history.py` 的 `sqlite3.connect(f"file:{db}?mode=ro", uri=True)` |
| `message_data` 的三种 `type` | `live_view._ingest_session_history_item` |
| `function_call` / `function_call_output` 靠 `call_id` 配对 | 同上（它用 `(agent_id, call_id)` 做键） |
| `result.success is False` ⇒ 失败 | `live_view._tool_status_from_result` |
| `think` 是推理来源 | `tools/thinking/tool.py` 的 `think(thought: str)` |
| 内部轮的判定前缀 | `live_view._INTERNAL_TURN_PREFIXES` |
| `agents.json` 的四个字典 | `live_view.hydrate_from_run_dir` |
| 终态集合 | `interface/viewer/transcript.py` 的 `_TERMINAL_STATUSES` |

## 与真机对账的边界（如实记）

这些测试用的是**按上述源码 schema 造出来的库**。它们能证明
"解析逻辑与源码描述的格式一致"，**不能**证明"真实 strix 写出来的库
逐字段就是这个样子" —— 那需要一次真实运行（本机没有 Docker，跑不了）。

所以这条边界写在这里：**格式来自源码，未与真实产物对账。**
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.scans import strix_events


def _make_agents_db(run_dir: Path, rows: list[tuple[str, dict]], *, run_name: str = "run-1") -> Path:
    """按 SDK 的建表语句造一个 agents.db，写入给定的消息行。"""
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


def _write_roster(run_dir: Path, *, run_name: str = "run-1") -> None:
    state = run_dir / "strix_runs" / run_name / ".state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "agents.json").write_text(
        json.dumps(
            {
                "statuses": {"a-root": "completed", "a-recon": "running"},
                "names": {"a-root": "root", "a-recon": "recon"},
                "parent_of": {"a-recon": "a-root"},
                "errors": {},
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def run_dir(tmp_path: Path) -> Path:
    return tmp_path / "scan"


# ══════════════════════════════════════════════════════════
# 读库
# ══════════════════════════════════════════════════════════


def test_reads_rows_in_id_order_with_agent_id(run_dir: Path) -> None:
    """`session_id` 就是 agent_id —— 这是照 `history.py` 的读法确认的。"""
    _make_agents_db(
        run_dir,
        [
            ("a-root", {"role": "assistant", "content": "第一条"}),
            ("a-recon", {"role": "assistant", "content": "第二条"}),
        ],
    )

    rows, error = strix_events.read_events(run_dir)

    assert error is None
    assert [row[0] for row in rows] == [1, 2]
    assert [row[1] for row in rows] == ["a-root", "a-recon"]


def test_cursor_skips_already_imported_rows(run_dir: Path) -> None:
    """游标是 `id > ?` —— 导入器靠它增量读取，重复读不会得到旧行。"""
    _make_agents_db(
        run_dir,
        [("a", {"role": "assistant", "content": str(index)}) for index in range(5)],
    )

    rows, _ = strix_events.read_events(run_dir, after_row_id=2)

    assert [row[0] for row in rows] == [3, 4, 5]


def test_missing_db_is_not_an_error(run_dir: Path) -> None:
    """扫描刚启动时库还不存在 —— 那是**正常状态**，不是失败。

    报错的话调度器每轮都会记一条异常，而真正的异常会被淹掉。
    """
    run_dir.mkdir(parents=True, exist_ok=True)

    rows, error = strix_events.read_events(run_dir)

    assert rows == []
    assert error is None


def test_reader_never_writes_to_the_database(run_dir: Path) -> None:
    """★ 只读打开，不改外部工具的库。

    依据是 `history.py` 的注释：扫描进程可能正在另一个进程里写这个 WAL 库，
    读者绝不能加锁或改它。这里用一个"文件 mtime 不变"来验。
    """
    db = _make_agents_db(run_dir, [("a", {"role": "assistant", "content": "x"})])
    before = (db.stat().st_mtime, db.stat().st_size)

    strix_events.read_events(run_dir)
    strix_events.read_events(run_dir)

    after = (db.stat().st_mtime, db.stat().st_size)
    assert after == before


def test_unreadable_row_is_skipped_not_fatal(run_dir: Path) -> None:
    """一行坏 JSON 不该让整次导入失败 —— 其余行照常读出。"""
    db = _make_agents_db(run_dir, [("a", {"role": "assistant", "content": "好的"})])
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO agent_messages (session_id, message_data) VALUES (?, ?)",
        ("a", "{ 这不是 JSON"),
    )
    conn.commit()
    conn.close()

    rows, error = strix_events.read_events(run_dir)

    assert error is None
    assert len(rows) == 1
    assert rows[0][2]["content"] == "好的"


# ══════════════════════════════════════════════════════════
# 翻译：确证的映射
# ══════════════════════════════════════════════════════════


def test_think_tool_becomes_reasoning(run_dir: Path) -> None:
    """★ `think` → `reasoning`，这是**确证**的映射。

    `tools/thinking/tool.py` 的签名就是 `think(thought: str)`，
    docstring 写明 "private chain-of-thought note"。所以它不是猜的。
    """
    _make_agents_db(
        run_dir,
        [
            (
                "a",
                {"type": "function_call", "call_id": "c1", "name": "think",
                 "arguments": json.dumps({"thought": "先做端口识别"})},
            ),
            (
                "a",
                {"type": "function_call_output", "call_id": "c1",
                 "output": json.dumps({"success": True, "message": "Thought recorded"})},
            ),
        ],
    )

    events = strix_events.translate(strix_events.read_events(run_dir)[0])

    assert len(events) == 1
    assert events[0].type == "reasoning"
    assert events[0].detail["intent"] == "plan"
    assert events[0].detail["text"] == "先做端口识别"


def test_successful_tool_call_becomes_tool_call(run_dir: Path) -> None:
    _make_agents_db(
        run_dir,
        [
            ("a", {"type": "function_call", "call_id": "c1", "name": "web_search",
                   "arguments": json.dumps({"query": "acme"})}),
            ("a", {"type": "function_call_output", "call_id": "c1",
                   "output": json.dumps({"success": True, "results": []})}),
        ],
    )

    events = strix_events.translate(strix_events.read_events(run_dir)[0])

    assert len(events) == 1
    event = events[0]
    assert event.type == "tool_call"
    assert event.detail["kind"] == "tool_call"
    assert event.detail["tool"] == "web_search"
    # ⚠️ 这两个**恒为 null**：strix 不给单次调用的退出码与耗时。
    # 编一个 0 会让人以为那是真的进程退出码。
    assert event.detail["exitCode"] is None
    assert event.detail["durationMs"] is None
    assert "acme" in event.detail["command"]


def test_failed_tool_call_becomes_failed(run_dir: Path) -> None:
    """`result.success is False` ⇒ 失败，依据 `_tool_status_from_result`。"""
    _make_agents_db(
        run_dir,
        [
            ("a", {"type": "function_call", "call_id": "c1", "name": "repeat_request",
                   "arguments": json.dumps({"request_id": "r1"})}),
            ("a", {"type": "function_call_output", "call_id": "c1",
                   "output": json.dumps({"success": False, "error": "目标拒绝重放"})}),
        ],
    )

    events = strix_events.translate(strix_events.read_events(run_dir)[0])

    assert events[0].type == "failed"
    assert events[0].detail["kind"] == "failed"
    assert "目标拒绝重放" in events[0].detail["error"]


def test_vulnerability_report_becomes_discovery(run_dir: Path) -> None:
    """产出漏洞报告 → `discovery`。

    ★ **任务 5 的对账修正了任务 4 的一处错误。**

    任务 4 的这版测试断言 `detail["severity"] == "high"`，而那个值来自
    我当时按"名字最可能对得上"猜的参数名 `args["severity"]`。
    任务 5 读 `tools/reporting/tool.py` 的签名确认：
    **`create_vulnerability_report` 根本没有 `severity` 参数** ——
    它由 `cvss_breakdown` 经 CVSS3 算出来（`_calculate_cvss`）。

    所以那个分支从来没命中过，而当时的注释已经标了
    "载荷启发式、待对账"。这条标注的价值就在这一刻兑现了：
    它指明了该去核对哪里，而不是让一个错字段安静地留在代码里。

    现在 `severity` 只在**调用方真的传了** `cvss_breakdown` /
    结果里带了 `severity` 时才出现 —— 取不到就不放这个键。
    """
    _make_agents_db(
        run_dir,
        [
            ("a", {"type": "function_call", "call_id": "c1",
                   "name": "create_vulnerability_report",
                   "arguments": json.dumps({"title": "未授权读取环境变量",
                                            "evidence": "HTTP/1.1 200 OK",
                                            "confidence": "high"},
                                           ensure_ascii=False)}),
            ("a", {"type": "function_call_output", "call_id": "c1",
                   "output": json.dumps({"success": True, "report_id": "vuln-9"})}),
        ],
    )

    events = strix_events.translate(strix_events.read_events(run_dir)[0])

    assert events[0].type == "discovery"
    assert events[0].detail["subject"] == "未授权读取环境变量"
    assert events[0].detail["reportId"] == "vuln-9"
    assert events[0].agent_phase == "verify"
    # `confidence` 的合法取值来自源码的 `_VALID_CONFIDENCE`（high/medium/low），
    # 所以直接用它，而不是自己编一个 `reported`。
    assert events[0].detail["confidence"] == "high"
    # ⚠️ 参数里没有 severity ⇒ **不放这个键**（而不是编一个默认值）。
    # 真正的严重度在 `vulnerabilities.json` 里，由任务 5 的导入路径读它。
    assert "severity" not in events[0].detail


def test_discovery_omits_severity_when_absent(run_dir: Path) -> None:
    """★ 取不到 `severity` 就**不放这个键**，而不是编一个默认值。

    放一个假的 "medium" 会让人以为 agent 判过严重度 —— 那是编造。
    而它的**权威来源**是 `vulnerabilities.json`（任务 5 的导入路径读它），
    实时路径本来就不该冒充知道严重度。
    """
    _make_agents_db(
        run_dir,
        [
            ("a", {"type": "function_call", "call_id": "c1",
                   "name": "create_vulnerability_report",
                   "arguments": json.dumps({"title": "某问题"})}),
            ("a", {"type": "function_call_output", "call_id": "c1",
                   "output": json.dumps({"success": True})}),
        ],
    )

    events = strix_events.translate(strix_events.read_events(run_dir)[0])

    assert "severity" not in events[0].detail


def test_assistant_message_becomes_reasoning_with_narration_intent(run_dir: Path) -> None:
    """assistant 发言 → `reasoning`，但 `intent` 标成 `narration`。

    ⚠️ 这是**语义近似**：契约的 8 种里没有"对外发言"这一类，
    而"agent 在想"与"agent 在说"必须能区分 —— 靠 `intent` 保住。
    把两者混成一类会丢掉真实信息。
    """
    _make_agents_db(run_dir, [("a", {"role": "assistant", "content": "进入利用阶段"})])

    events = strix_events.translate(strix_events.read_events(run_dir)[0])

    assert events[0].type == "reasoning"
    assert events[0].detail["intent"] == "narration"


def test_first_user_turn_is_dropped_as_the_task(run_dir: Path) -> None:
    """agent 的第一条 user 轮是"它的任务"，不是人发的消息。

    依据 `live_view._hydrate_sdk_session_history` 的 `first_user_turn` 处理。
    契约的 8 种里没有"任务指派"，所以丢掉（而不是硬塞进某一类）。
    """
    _make_agents_db(
        run_dir,
        [
            ("a", {"role": "user", "content": "对 api.example.com 做安全测试"}),
            ("a", {"role": "assistant", "content": "开始"}),
        ],
    )

    events = strix_events.translate(strix_events.read_events(run_dir)[0])

    assert [event.type for event in events] == ["reasoning"]
    assert events[0].detail["text"] == "开始"


@pytest.mark.parametrize(
    "content",
    [
        "[Message from agent-recon] 我发现了一个端点",
        "== Inherited context from parent ==\n...",
        "Your previous message ended a turn without a tool call.",
        "[NOTICE] Turn budget: 70% used",
        "[URGENT] Scan cost budget: 85% used",
    ],
)
def test_internal_turns_are_dropped(run_dir: Path, content: str) -> None:
    """系统灌给 agent 的引导不是"人发的消息"。

    前缀表照抄 `live_view._INTERNAL_TURN_PREFIXES` —— 不照抄的话，
    这些会被当成用户消息渲染出来，而用户从没打过那些字。
    """
    _make_agents_db(
        run_dir,
        [
            ("a", {"role": "user", "content": "任务"}),
            ("a", {"role": "user", "content": content}),
        ],
    )

    events = strix_events.translate(strix_events.read_events(run_dir)[0])

    assert events == []


def test_pending_call_without_output_still_produces_an_event(run_dir: Path) -> None:
    """调用发出但结果没回来（扫描被终止）→ 仍然发一条。

    不发的话，活动流里会出现"调用了一个工具然后就没了下文"——
    而那看起来像事件丢了。
    """
    _make_agents_db(
        run_dir,
        [("a", {"type": "function_call", "call_id": "c1", "name": "web_search",
                "arguments": json.dumps({"query": "x"})})],
    )

    events = strix_events.translate(strix_events.read_events(run_dir)[0])

    assert len(events) == 1
    assert events[0].type == "tool_call"
    assert events[0].detail["output"] == ""


def test_calls_are_paired_by_call_id_not_by_order(run_dir: Path) -> None:
    """靠 `call_id` 配对，不是靠顺序 —— 并发工具调用会交错。

    strix 自己也是用 `(agent_id, call_id)` 做键的（`live_view`）。
    按顺序配对的话，两个并发调用会互相串结果。

    ⚠️ 断言方式要注意：契约 §2.6 里 `failed` 的载荷是
    `{kind, attempted, error}` —— **没有 `tool` 字段**（`tool` 只属于
    `tool_call`）。所以这里按类型分别取，而不是统一按 `detail["tool"]` 找。
    """
    _make_agents_db(
        run_dir,
        [
            ("a", {"type": "function_call", "call_id": "c1", "name": "web_search",
                   "arguments": json.dumps({"q": "1"})}),
            ("a", {"type": "function_call", "call_id": "c2", "name": "repeat_request",
                   "arguments": json.dumps({"q": "2"})}),
            ("a", {"type": "function_call_output", "call_id": "c2",
                   "output": json.dumps({"success": False, "error": "第二个失败"})}),
            ("a", {"type": "function_call_output", "call_id": "c1",
                   "output": json.dumps({"success": True})}),
        ],
    )

    events = strix_events.translate(strix_events.read_events(run_dir)[0])

    assert len(events) == 2
    failed = [event for event in events if event.type == "failed"]
    ok = [event for event in events if event.type == "tool_call"]
    assert len(failed) == 1 and len(ok) == 1

    # 结果没有串：失败的那条属于 c2（repeat_request），成功的那条是 c1（web_search）
    assert "repeat_request" in failed[0].detail["attempted"]
    assert "第二个失败" in failed[0].detail["error"]
    assert ok[0].detail["tool"] == "web_search"


# ══════════════════════════════════════════════════════════
# 名册与映射
# ══════════════════════════════════════════════════════════


def test_roster_reads_the_four_dictionaries(run_dir: Path) -> None:
    """`agents.json` 的四个字典，键是 agent_id。"""
    _write_roster(run_dir)

    roster = strix_events.read_agents_roster(run_dir)

    assert roster["a-root"]["name"] == "root"
    assert roster["a-root"]["status"] == "completed"
    assert roster["a-recon"]["parentId"] == "a-root"


def test_terminal_statuses_map_to_done_or_failed() -> None:
    """终态映射。

    `completed`/`stopped`/`interrupted` 是 strix 自己在
    `interface/viewer/transcript.py` 的 `_TERMINAL_STATUSES` 里列的终态集合。
    """
    assert strix_events.status_for("completed") == "done"
    assert strix_events.status_for("stopped") == "done"
    assert strix_events.status_for("running") == "running"
    assert strix_events.status_for("failed") == "failed"
    assert strix_events.status_for("interrupted") == "failed"
    # 认不出来的退回 idle，而不是当成"在跑" —— 谎报在跑会让界面一直转
    assert strix_events.status_for("某个没见过的状态") == "idle"


def test_agent_id_is_stable_and_fits_the_column() -> None:
    """★ 映射必须**稳定**且放得进 `String(64)`。

    不稳定的话，同一次扫描反复导入会把名册拆成好几份重复的 agent；
    超长的话直接写库失败。
    """
    short = "agent-root"
    assert strix_events.stable_agent_id(short) == strix_events.stable_agent_id(short)

    long_id = "x" * 400
    mapped = strix_events.stable_agent_id(long_id)
    assert len(mapped) <= 64
    assert mapped == strix_events.stable_agent_id(long_id)


def test_text_is_clipped_with_an_honest_note() -> None:
    """超长文本截断，而且**说明被截了**。

    静默截断会让人以为 agent 就输出了这么多。
    """
    clipped = strix_events._clip("字" * 5000, 100)

    assert clipped.startswith("字" * 100)
    assert "已截断" in clipped
    assert "5000" in clipped


def test_broken_timestamp_falls_back_to_now() -> None:
    """坏时间戳退回"现在"，而不是抛异常。

    一条时间戳坏掉的事件不该让整次导入失败 —— 而排序靠 `at`，
    给个坏值会让它在活动流里乱跳。
    """
    assert strix_events._parse_at("不是时间").tzinfo is not None
    assert strix_events._parse_at("2026-09-24T07:30:00").tzinfo is not None
