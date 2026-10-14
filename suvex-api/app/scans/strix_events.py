"""把 strix 的结构化事件翻译成契约 §2.5 / §2.6 定义的事件。

## 格式不是猜的 —— 逐条来自 strix 源码

> 官方文档说事件在 `strix_runs/<run>/events.jsonl`。
> **在 `strix-agent` 1.6.2 的源码里全包搜索 `events.jsonl`，零命中。**
> 那个文件不存在。所以这份实现按**源码**写，不按文档写。

真实的产物（`core/paths.py`、`telemetry/logging.py`、`runtime/runner.py`、
`interface/viewer/transcript.py` 交叉确认）：

| 文件 | 内容 |
| --- | --- |
| `strix_runs/<run>/run.json` | 运行记录（`status` / `start_time` / `end_time` / `targets_info` / `user_instruction`） |
| `strix_runs/<run>/.state/agents.json` | agent 名册（`statuses` / `names` / `parent_of` / `errors`） |
| `strix_runs/<run>/.state/agents.db` | **SQLite**，`agent_messages` 表 —— **结构化事件流在这里** |
| `strix_runs/<run>/strix.log` | 纯文本日志 |
| `strix_runs/<run>/vulnerabilities.json` | 漏洞列表 |

`agents.db` 的表结构（来自 openai-agents SDK 的 `sqlite_session.py`）：

```sql
CREATE TABLE agent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,      -- 这一列就是 strix 的 agent_id
    message_data TEXT NOT NULL,    -- 一行一个 JSON（SDK 会话项）
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
```

`message_data` 里的 JSON 有三种 `type`（来自 `live_view._ingest_session_history_item`）：

* `role in {user, assistant}` 且 `type in {null, "message"}` → 一条消息，正文在 `content`；
* `type == "function_call"` → 一次工具调用（`call_id` / `name` / `arguments`）；
* `type == "function_call_output"` → 该调用的结果（`call_id` / `output`）。

**读法照抄 strix 自己**：`interface/tui/history.py` 用
`file:...?mode=ro` 只读打开。那行注释写得很清楚：

> 扫描进程可能正在另一个进程里写这个 WAL 数据库（本地 viewer 就是实时 tail 它），
> 读者绝不能加锁或改它。

## 语义映射：哪些是确证的，哪些是启发式

契约 §2.5 定义了 **8 种**事件类型，而 strix 的结构化流只有 **2 种**
（`chat` / `tool`）。所以这里不是改字段名，是一次真实的语义翻译。
下面每个映射都标了它的依据强度：

| 契约类型 | strix 来源 | 依据 |
| --- | --- | --- |
| `tool_call` | `tool` 调用**成功**结束 | ✅ 直接对应 |
| `failed` | `tool` 调用**失败**结束 | ✅ 直接对应（`result.success is False`） |
| `reasoning` | `tool_name == "think"` | ✅ **确证**：`tools/thinking/tool.py` 的签名就是 `think(thought: str)`，docstring 写明 "private chain-of-thought note" |
| `reasoning` | `chat` 里 `role == "assistant"` | ⚠️ 语义近似：strix 把"内心推理"与"对外发言"分开，而契约的 8 种里没有"发言"这一类。用 `intent` 字段区分两者（`plan` / `narration`） |
| `discovery` | `tool_name == "create_vulnerability_report"` 成功 | ✅ 确证是"产出了一条漏洞"，⚠️ 但**载荷字段是启发式的**（见下） |

**这 8 种里，有 4 种在 strix 侧没有对应来源**，如实记在这里，
免得后来者以为是漏了：`poc_generated` / `verified` / `blocked` / `completed`。
其中 `completed` 由**扫描生命周期**发出（见 `scans/service.py`），不需要 strix 提供；
另三种 strix 根本没有对应的结构化信号。

### ⚠️ 启发式的部分（需要一次真实运行才能确认）

`create_vulnerability_report` 的**参数结构**我没有在源码里逐字段追到底，
所以 `discovery` 的三个载荷字段是按"名字最可能对得上"取的：

* `subject` ← `args["title"]`；
* `evidence` ← `args["evidence"]` 或 `args["description"]`，取到哪个用哪个，都没有就留空；
* `severity` ← `args["severity"]`；**取不到就不放这个键**，而不是编一个。
  （权威来源其实是 `vulnerabilities.json` 里的 `severity`，
  但那要等工具把报告写完才能读到，不适合实时路径。）

这三条**必须**在有 Docker 的机器上跑一次真实扫描、对着
`vulnerabilities.json` 核对之后才算坐实。在核对之前，
解析层产出的 `discovery` 事件是"结构正确、字段待验证"。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.vocab import ActivityEventType, AgentPhase, AgentRole, AgentStatus

logger = logging.getLogger("suvex.strix")

# 读取单个字段的上限。工具输出动辄几十 KB（一次 nmap 的完整输出），
# 全存进 events 表会让"回放一次扫描"变成拉几百 MB。
# strix 自己也有这个约束（`tools/output_store.py` 的 `bound_text`）。
MAX_TEXT = 4000
MAX_SUMMARY = 200

# strix 事件 → 我们的阶段。用于 `ActivityFeedItem` 的阶段标签（"利用中"）。
# ⚠️ 这是**展示层**的粗分类，依据是工具名里最可能对得上的一段。
_PHASE_BY_TOOL = (
    ("web_search", AgentPhase.RECON),
    ("web_get_contents", AgentPhase.RECON),
    ("view_sitemap_entry", AgentPhase.RECON),
    ("list_sitemap", AgentPhase.RECON),
    ("scope_rules", AgentPhase.RECON),
    ("record_coverage", AgentPhase.SCAN),
    ("update_coverage", AgentPhase.SCAN),
    ("list_coverage", AgentPhase.SCAN),
    ("repeat_request", AgentPhase.EXPLOIT),
    ("view_request", AgentPhase.EXPLOIT),
    ("list_requests", AgentPhase.EXPLOIT),
    ("create_vulnerability_report", AgentPhase.VERIFY),
    ("update_vulnerability_report", AgentPhase.VERIFY),
    ("create_dependency_report", AgentPhase.VERIFY),
    ("finish_scan", AgentPhase.REPORT),
    ("agent_finish", AgentPhase.REPORT),
    ("list_reports", AgentPhase.REPORT),
    ("get_report", AgentPhase.REPORT),
)

# strix 的 agent 名 → 我们的 5 个角色。
# ⚠️ 启发式：strix 的 agent 名由它的 agent graph 生成，
# 我没有在源码里找到"角色"这个概念的固定取值。按子串匹配，
# 命中不了就退回 `scanner`。需要一次真实运行来核对。
_ROLE_BY_NAME = (
    ("recon", AgentRole.RECON),
    ("scout", AgentRole.RECON),
    ("scan", AgentRole.SCANNER),
    ("exploit", AgentRole.EXPLOITER),
    ("verify", AgentRole.VERIFIER),
    ("valid", AgentRole.VERIFIER),
    ("report", AgentRole.REPORTER),
    ("summar", AgentRole.REPORTER),
)

# strix 的运行状态 → 我们的 agent 状态。
# `completed`/`stopped`/`interrupted` 来自 `interface/viewer/transcript.py`
# 的 `_TERMINAL_STATUSES`（那是权威的终态集合）。
_STATUS_MAP = {
    "running": AgentStatus.RUNNING,
    "completed": AgentStatus.DONE,
    "stopped": AgentStatus.DONE,
    "interrupted": AgentStatus.FAILED,
    "failed": AgentStatus.FAILED,
    "error": AgentStatus.FAILED,
    "blocked": AgentStatus.BLOCKED,
    "waiting": AgentStatus.IDLE,
    "idle": AgentStatus.IDLE,
}

# 系统灌给 agent 的"用户轮"（不是人打的字）。照抄
# `live_view._INTERNAL_TURN_PREFIXES` —— 复刻它的理由：
# 这些是 strix 自己认定的"内部引导"，把它们当成人发的消息会污染活动流。
_INTERNAL_TURN_PREFIXES = (
    "[Message from ",
    "== Inherited context from parent",
    "Your previous message ended a turn without a tool call.",
    "Your previous response ended the autonomous run without a lifecycle tool call.",
    *(
        f"[{label}] {subject}"
        for label in ("NOTICE", "URGENT", "CRITICAL")
        for subject in ("Turn budget:", "Scan cost budget:")
    ),
)


@dataclass(frozen=True, slots=True)
class StrixEvent:
    """一条翻译好的事件（还没落库）。

    `source_row_id` 是 `agent_messages.id` —— 它同时是**幂等键**：
    导入器靠它去重，所以重复导入同一个库不会产生重复事件。
    `agent_ref` 是 strix 的 agent_id，导入器据此建/找本地的 agent 行。
    """

    source_row_id: int
    agent_ref: str
    type: str
    summary: str
    detail: dict[str, Any] | None
    at: datetime
    agent_phase: str
    agent_name: str = ""


# ══════════════════════════════════════════════════════════
# 读取
# ══════════════════════════════════════════════════════════


def agents_db_path(run_dir: Path) -> Path:
    """`strix_runs/<run>/.state/agents.db`。

    `.state` 这个目录名来自 `core/paths.py` 的 `RUNTIME_STATE_DIR_NAME`。
    """
    return run_dir / "strix_runs" / ".state" / "agents.db"


def find_agents_db(run_dir: Path) -> Path | None:
    """在运行目录里找到那个 agents.db。

    正常路径是 `strix_runs/<run>/.state/agents.db`，但 run 目录名是 strix
    生成的、我们不一定知道（`run_id` 读取失败时会不知道）。
    所以这里做一次浅搜索：`strix_runs/*/.state/agents.db`。
    取最新修改的那个 —— 一次扫描正常只产生一个 run 目录。
    """
    direct = agents_db_path(run_dir)
    if direct.is_file():
        return direct
    base = run_dir / "strix_runs"
    if not base.is_dir():
        return None
    candidates = [p for p in base.glob("*/.state/agents.db") if p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def read_agents_roster(run_dir: Path) -> dict[str, dict[str, Any]]:
    """读 `agents.json` 的名册：`{agent_id: {name, parentId, status, error}}`。

    键名照抄 `live_view.hydrate_from_run_dir`（`statuses` / `names` /
    `parent_of` / `errors` 四个字典，都以 agent_id 为键）。
    """
    path = run_dir / "strix_runs" / ".state" / "agents.json"
    if not path.is_file():
        base = run_dir / "strix_runs"
        found = sorted(base.glob("*/.state/agents.json")) if base.is_dir() else []
        if not found:
            return {}
        path = max(found, key=lambda p: p.stat().st_mtime)

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}

    statuses = data.get("statuses") or {}
    names = data.get("names") or {}
    parents = data.get("parent_of") or {}
    errors = data.get("errors") or {}
    roster: dict[str, dict[str, Any]] = {}
    if not isinstance(statuses, dict):
        return {}
    for agent_id, status in statuses.items():
        if not isinstance(agent_id, str):
            continue
        roster[agent_id] = {
            "name": (names.get(agent_id) if isinstance(names, dict) else None) or agent_id,
            "parentId": parents.get(agent_id) if isinstance(parents, dict) else None,
            "status": str(status),
            "error": errors.get(agent_id) if isinstance(errors, dict) else None,
        }
    return roster


def read_events(run_dir: Path, *, after_row_id: int = 0) -> tuple[list[tuple[int, str, Any, str]], str | None]:
    """读 `agent_messages`，返回 `([(row_id, agent_id, item, timestamp)], error)`。

    **只读、不加锁、不改库** —— 照抄 strix `interface/tui/history.py` 的做法：
    扫描进程可能正在另一个进程里写这个 WAL 数据库，读者绝不能干扰它。

    返回 `(rows, error)` 而不是抛异常：读不到只是"现在还没有事件"，
    而扫描刚启动时 agents.db 确实可能还不存在 —— 那不该算失败。
    """
    path = find_agents_db(run_dir)
    if path is None:
        return [], None

    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        rows = conn.execute(
            "SELECT id, session_id, message_data, created_at FROM agent_messages "
            "WHERE id > ? ORDER BY id",
            (after_row_id,),
        ).fetchall()
    except sqlite3.Error as exc:
        # WAL 正在被写、或者文件刚建出来还是空的 —— 都是**暂时**状态，
        # 下一轮再读即可。所以是 debug 而不是 exception。
        logger.debug("读取 strix 事件库失败（将在下一轮重试）：%s", exc)
        return [], str(exc)
    finally:
        if conn is not None:
            conn.close()

    out: list[tuple[int, str, Any, str]] = []
    for row_id, agent_id, message_data, created_at in rows:
        try:
            item = json.loads(message_data)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(item, dict):
            out.append((int(row_id), str(agent_id), item, _to_iso(created_at)))
    return out, None


def _to_iso(value: Any) -> str:
    """SQLite 的 `created_at` → ISO 8601 带时区。

    照抄 `history._sqlite_timestamp_to_iso`：naive 时间按 UTC 处理 ——
    那是 strix 写它时用的时区（`CURRENT_TIMESTAMP` 在 SQLite 里就是 UTC）。
    """
    if not isinstance(value, str) or not value.strip():
        return datetime.now(UTC).isoformat()
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


# ══════════════════════════════════════════════════════════
# 翻译
# ══════════════════════════════════════════════════════════


def translate(rows: list[tuple[int, str, Any, str]]) -> list[StrixEvent]:
    """把 SDK 会话项翻成契约事件。

    集合式（而不是逐条）处理，因为一次工具调用由**两行**组成：
    `function_call` 带参数、`function_call_output` 带结果，
    它们靠 `call_id` 配对。逐条处理的话，参数与结果永远凑不到一起。
    """
    # call_id → (row_id, agent_id, item, timestamp)
    pending_calls: dict[tuple[str, str], tuple[int, str, dict, str]] = {}
    events: list[StrixEvent] = []
    # 每个 agent 的第一次 user 轮是"它的任务"，不是人发的消息 ——
    # 照抄 `live_view._hydrate_sdk_session_history` 的处理。
    tasked: set[str] = set()

    for row_id, agent_id, item, timestamp in rows:
        item_type = item.get("type")
        role = item.get("role")

        if role in {"user", "assistant"} and item_type in {None, "message"}:
            content = _content_text(item.get("content"))
            if not content:
                continue
            if role == "user":
                first = agent_id not in tasked
                tasked.add(agent_id)
                # 第一次是任务；带内部前缀的是系统引导。两者都不是"人发的消息"，
                # 而契约的 8 种类型里没有"任务指派"这一类，所以丢掉。
                if first or _is_internal_turn(content):
                    continue
                # 非首次、非内部的人发消息：自主扫描里不会出现（没有交互入口）。
                # 真出现了也只当推理记录，不编一个不存在的类型。
                events.append(_reasoning(row_id, agent_id, content, timestamp, intent="message"))
                continue
            events.append(_reasoning(row_id, agent_id, content, timestamp, intent="narration"))
            continue

        if item_type == "function_call":
            call_id = str(item.get("call_id") or item.get("id") or "")
            pending_calls[(agent_id, call_id)] = (row_id, agent_id, item, timestamp)
            continue

        if item_type == "function_call_output":
            call_id = str(item.get("call_id") or item.get("id") or "")
            call = pending_calls.pop((agent_id, call_id), None)
            events.append(_tool_event(row_id, agent_id, call, item, timestamp))
            continue

    # 有调用但始终没有结果的（扫描被中途终止）也发出来 ——
    # 不然活动流里会出现"调用了一个工具然后什么都没说"。
    for (_agent, _call_id), (row_id, agent_id, item, timestamp) in pending_calls.items():
        events.append(_tool_event(row_id, agent_id, (row_id, agent_id, item, timestamp), None, timestamp))

    return events


def _tool_event(
    row_id: int,
    agent_id: str,
    call: tuple[int, str, dict, str] | None,
    output: dict | None,
    timestamp: str,
) -> StrixEvent:
    """一次工具调用 → `tool_call` / `failed` / `discovery` / `reasoning`。

    一次调用**只产出一条事件**，在它结束时。理由：

    * 契约的 `tool_call` 载荷里有 `output` 与 `exitCode` —— 那只有结束时才知道；
    * strix 自己是"先建调用事件、结果回来再 `version+1` 更新同一条"，
      而契约的 ActivityEvent **没有 version 字段**，前端按 id 去重排序。
      要在契约里表达"同一条被更新"就得加字段，而那是前端类型变更。
    * 代价如实记：**活动流里看不到"正在执行"的那一行**，
      只在工具结束时出现。要那个效果得给契约加版本号，属于后续工作。
    """
    item = output or (call[2] if call else {})
    tool_name = str((call[2].get("name") if call else None) or item.get("name") or "tool")
    args = _parse_json_object(call[2].get("arguments")) if call else {}
    raw_output = item.get("output")
    result = _parse_json_value(raw_output)
    ok = not (isinstance(result, dict) and result.get("success") is False)
    text = _clip(_render(result))
    phase = _phase_for(tool_name)

    # ★ `think` 是**确证**的推理来源：`tools/thinking/tool.py` 的签名就是
    # `think(thought: str)`，docstring 写明 "private chain-of-thought note"。
    if tool_name == "think":
        thought = str(args.get("thought") or text or "")
        return _reasoning(row_id, agent_id, thought, timestamp, intent="plan")

    # 产出漏洞报告：这是"一次发现"的唯一可靠信号。
    if tool_name in {"create_vulnerability_report", "create_dependency_report"} and ok:
        return StrixEvent(
            source_row_id=row_id,
            agent_ref=agent_id,
            type=ActivityEventType.DISCOVERY.value,
            summary=_clip(str(args.get("title") or f"产出了一条 {tool_name} 记录"), MAX_SUMMARY),
            detail=_discovery_detail(tool_name, args, result),
            at=_parse_at(timestamp),
            agent_phase=phase,
        )

    if not ok:
        error = ""
        if isinstance(result, dict):
            error = str(result.get("error") or result.get("message") or "")
        return StrixEvent(
            source_row_id=row_id,
            agent_ref=agent_id,
            type=ActivityEventType.FAILED.value,
            summary=_clip(f"{tool_name} 执行失败{('：' + error) if error else ''}", MAX_SUMMARY),
            detail={
                "kind": ActivityEventType.FAILED.value,
                "attempted": _clip(f"{tool_name} {_render(args)}", MAX_SUMMARY),
                "error": _clip(error or "工具返回了失败状态但未给出原因", MAX_TEXT),
            },
            at=_parse_at(timestamp),
            agent_phase=phase,
        )

    return StrixEvent(
        source_row_id=row_id,
        agent_ref=agent_id,
        type=ActivityEventType.TOOL_CALL.value,
        summary=_clip(f"{tool_name} {_render(args)}", MAX_SUMMARY),
        detail={
            "kind": ActivityEventType.TOOL_CALL.value,
            "tool": tool_name,
            # `command` 是给人看的一行。strix 给的是结构化 args，
            # 所以优先取其中的 `command` 字段，没有就把整个 args 压成一行 JSON。
            "command": _clip(str(args.get("command") or _render(args)), MAX_TEXT),
            # ⚠️ **恒为 null**：strix 不提供每次调用的退出码
            # （它给的是 `result.success` 布尔）。契约允许 null，
            # 而编一个 0/1 会让人以为那是真的进程退出码。
            "exitCode": None,
            # ⚠️ 同样恒为 null：strix 不记录单次工具调用的耗时。
            "durationMs": None,
            "output": text,
        },
        at=_parse_at(timestamp),
        agent_phase=phase,
    )


def _reasoning(row_id: int, agent_id: str, text: str, timestamp: str, *, intent: str) -> StrixEvent:
    return StrixEvent(
        source_row_id=row_id,
        agent_ref=agent_id,
        type=ActivityEventType.REASONING.value,
        summary=_clip(text, MAX_SUMMARY),
        detail={
            "kind": ActivityEventType.REASONING.value,
            "text": _clip(text, MAX_TEXT),
            # `intent` 用来区分两个来源：`plan` = `think` 工具（内心推理），
            # `narration` = 对外发言。契约的 8 种里没有"发言"这一类，
            # 而两者都是"agent 在想/在说" —— 用这个字段保住区别。
            "intent": intent,
        },
        at=_parse_at(timestamp),
        agent_phase=AgentPhase.SCAN,
    )


def _discovery_detail(tool_name: str, args: dict, result: Any) -> dict:
    """`discovery` 的载荷。

    ★ **任务 5 的对账结论（修正了任务 4 的一处错误）**：
    读 `tools/reporting/tool.py` 的签名确认，`create_vulnerability_report`
    的参数里**没有 `severity`** —— 它由 `cvss_breakdown` 经 CVSS3 算出来
    （`_calculate_cvss`）。所以任务 4 里读 `args["severity"]` 的那个分支
    **从来没有命中过**，而当时的注释已经标了"载荷启发式、待对账"。
    这正是那条标注的意义：它在被证伪时指明了要看哪里。

    现在改成从 `cvss_breakdown` 里取**能拿到的部分**，而不再假装
    参数里有 `severity`。真正的严重度在 `vulnerabilities.json` 里
    （任务 5 的导入路径读它），实时路径只求"有就带上、没有就不放"。
    """
    detail: dict[str, Any] = {
        "kind": ActivityEventType.DISCOVERY.value,
        "subject": _clip(str(args.get("title") or args.get("name") or tool_name), MAX_SUMMARY),
        "evidence": _clip(str(args.get("evidence") or args.get("description") or ""), MAX_TEXT),
        # 工具**成功返回**了报告 ⇒ 这次发现至少被 agent 自己坐实了一层。
        # confidence 的合法取值是 `high|medium|low`（源码 `_VALID_CONFIDENCE`），
        # 所以直接用它的值，而不是自己编一个 `reported`。
        "confidence": str(args.get("confidence") or "").strip().lower() or "reported",
    }

    # `severity` 只可能来自 `cvss_breakdown`（如果调用方传了），
    # 而它算出来的是 CVSS 分档。取不到就**不放这个键** ——
    # 编一个默认值会让人以为 agent 判过严重度。
    breakdown = args.get("cvss_breakdown")
    if isinstance(breakdown, dict) and breakdown:
        detail["cvssBreakdown"] = {str(k): str(v) for k, v in breakdown.items()}

    if isinstance(result, dict) and result.get("report_id"):
        detail["reportId"] = str(result["report_id"])
    if isinstance(result, dict) and result.get("severity"):
        detail["severity"] = str(result["severity"]).strip().lower()
    return detail


# ── agent 名册 ────────────────────────────────────────────


def role_for_name(name: str) -> str:
    lowered = (name or "").lower()
    for needle, role in _ROLE_BY_NAME:
        if needle in lowered:
            return role.value
    return AgentRole.SCANNER.value


def status_for(status: str) -> str:
    return _STATUS_MAP.get((status or "").lower(), AgentStatus.IDLE).value


def stable_agent_id(strix_agent_id: str) -> str:
    """把 strix 的 agent id 映成我们的 `Agent.id`（≤64 字符）。

    strix 的 id 是它的 session id，长度不保证。我们的列是 `String(64)`，
    超长会直接写库失败。所以超长时用稳定的 sha1 截断 ——
    **稳定很重要**：同一次扫描反复导入必须命中同一行。
    """
    candidate = f"sa_{strix_agent_id}"
    if len(candidate) <= 64:
        return candidate
    digest = hashlib.sha1(strix_agent_id.encode("utf-8")).hexdigest()
    return f"sa_{digest[:32]}"


# ── 小工具 ────────────────────────────────────────────────


def _phase_for(tool_name: str) -> str:
    for needle, phase in _PHASE_BY_TOOL:
        if needle in tool_name:
            return phase.value
    return AgentPhase.SCAN.value


def _is_internal_turn(content: str) -> bool:
    return content.lstrip().startswith(_INTERNAL_TURN_PREFIXES)


def _content_text(content: Any) -> str:
    """把 `content` 压成纯文本。照抄 `live_view._message_content_text`：
    它可能是字符串，也可能是 `[{type, text}, ...]` 这样的块列表。"""
    parts: list[str] = []
    for part in content if isinstance(content, list) else [content]:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict) and isinstance(part.get("text"), str):
            parts.append(part["text"])
    return "".join(parts)


def _parse_json_object(value: Any) -> dict:
    parsed = _parse_json_value(value)
    return parsed if isinstance(parsed, dict) else {}


def _parse_json_value(value: Any) -> Any:
    """JSON 字符串就解析，否则原样返回。照抄 `live_view._parse_json_value`。"""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _render(value: Any) -> str:
    """把任意载荷压成一行可读文本。

    `ensure_ascii=False` 是必须的：目标地址与工具输出里大量是中文与非 ASCII，
    转义之后活动流里会是一片 ``\\uXXXX`` 反斜杠转义，读不了。

    （这一行注释本身也踩过那个坑：写 `\\uXXXX` 时不转义反斜杠，
    Python 会把它当成真的 Unicode 转义去解 —— 而后面跟的不是合法十六进制，
    于是**整个模块 import 失败**。脚本里用普通字符串写正则、
    写转义示例时都要留意这一点。）
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def _clip(text: str, limit: int = MAX_TEXT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"…（已截断，原文 {len(text)} 字符）"


def _parse_at(timestamp: str) -> datetime:
    """ISO 时间 → tz-aware datetime。坏值退回"现在"。

    退回"现在"而不是报错：一条时间戳坏掉的事件不该让整次导入失败 ——
    而排序靠 `at`，给个坏值会让它在活动流里乱跳。
    """
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.now(UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
