"""替身后端（`FakeRunner`）能覆盖到什么 —— 以及覆盖不到什么。

## 这个文件守的是什么

`FakeRunner` 是**测试与 CI 唯一会跑的扫描后端**（`SUPEX_SCAN_RUNNER=fake`，
见 `tests/conftest.py`）。它做出来的产物决定了"整条链路有没有被测过"：

* 它写出来的 `agents.db` / `agents.json` 决定**解析层**（`strix_events`）
  能不能被喂到真数据；
* 它写出来的 `vulnerabilities.json` 决定**落库层**（`findings`）能不能被喂到
  真数据；
* 它的 agent 树与 `errors` 决定**导入层**（`importer`）的分支有没有人走。

所以这个文件断言的是**替身产物的覆盖面**，不是某一段代码的逻辑 ——
逻辑有各自的测试文件。这里的问题是：**那条链路到底有没有被真实数据走过。**

## ⚠️ 它证不到什么（如实记）

替身是"照 strix 源码的形状"造出来的，**不是**真实 strix 跑出来的。
所以它能证明"我们的解析层能处理这个形状"，**不能**证明
"真实 strix 写出来的就是这个形状" —— 那要一次真实运行（在 Kali 上，
用真 strix 跑一次受控目标）。这条边界是 `strix_events` 模块 docstring
里那条边界的另一半，两处都要留着。

## 为什么真的起子进程

`FakeRunner` 的 docstring 写着"它**不是 mock**"。这里的测试照那个纪律写：
走 `FakeRunner().launch()` 这条**生产路径**（它自己拼 env、自己写脚本、
自己 popen），然后读它真的落在磁盘上的产物。用 `_FAKE_SCRIPT` 直接
`exec` 会绕开 env 拼装，而那正是"凭据走 env 而不进 argv"那条判据的所在。
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.vocab import AgentStatus, Severity
from app.events.models import ActivityEvent, Agent
from app.findings import service as findings_service
from app.findings.models import Finding
from app.scans import importer, strix_events
from app.scans.runner import FakeRunner, LaunchSpec

# 契约 §2.5 的 8 种事件类型里，**strix 侧**能提供来源的那几种。
# 第 8 种 `completed` 由扫描生命周期发出（`scans/service.py`），
# 替身不负责它 —— 所以它不在这张表里，而不是"漏了"。
#
# ⚠️ `blocked` 也**不在**这里（2026-10-16 修正）：它此前挂在本不存在的
# `wait_for_user` 上，而在 `strix-agent` 1.6.2 上全包搜 `wait_for_user`
# 零命中，真名 `respond_to_user` 又受 `if interactive:` 门控、本项目跑的是
# `--non-interactive` —— 双重不可达。所以它在 strix 侧**无来源**，
# 替身也喂不出来（见 `test_respond_to_user_does_not_become_blocked`）。
STRIX_SOURCED_TYPES = {
    "tool_call",
    "reasoning",
    "discovery",
    "poc_generated",
    "verified",
    "failed",
}


def _spec(run_dir: Path, **overrides) -> LaunchSpec:  # noqa: ANN003
    base = {
        "scan_id": "scan_fake",
        "target": "api.example.com",
        "target_kind": "domain",
        "skills": ("web",),
        "run_dir": run_dir,
        "log_path": run_dir / "scan.log",
        "llm_model": "openai/test",
        "llm_api_key": "sk-test",
    }
    base.update(overrides)
    return LaunchSpec(**base)  # type: ignore[arg-type]


def _run_fake(run_dir: Path, *, run_id: str = "coverage") -> Path:
    """真的起一次替身进程，等它跑完，返回**扫描的 run_dir**。

    走 `FakeRunner.launch()` 而不是直接 `subprocess.run(_FAKE_SCRIPT)` ——
    见模块 docstring。

    ⚠️ 返回的是 `scan.run_dir`（`spec.run_dir`），不是 strix 那个
    `strix_runs/<run_id>` 子目录 —— 解析层与落库层要的都是前者
    （`strix_events.find_agents_db` / `strix_vulns.read_vulnerabilities`
    都从它往下找）。用错这一层，读到的会是"空"而不是报错。
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    result = FakeRunner().launch(_spec(run_dir, scan_id=run_id))
    try:
        code = result.process.wait(timeout=120)
    finally:
        result.process.kill()
    assert code == 0, f"替身进程退出码 {code}，日志：{result.log_path.read_text(encoding='utf-8')}"
    return run_dir


def _strix_root(run_dir: Path) -> Path:
    """替身写的那个 strix run 目录。

    `FakeRunner.env` 里 run id 是 `cfg.fake_scan_run_id or f"fake-{spec.scan_id}"`，
    而测试里 `fake_scan_run_id` 是空串 ⇒ 名字就是 `fake-coverage`。
    """
    return run_dir / "strix_runs" / "fake-coverage"


@pytest.fixture(scope="module")
def fake_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """跑**一次**替身，给本模块所有只读它产物的用例共用。返回扫描的 run_dir。

    替身每次要睡 `SUPEX_FAKE_SCAN_SECONDS`（conftest 设 1.0 秒），
    再算上起孙进程 —— 每个用例各跑一次会让这个文件慢得没人愿意跑。
    产物的形状在一次运行里是确定的，所以共用是安全的。

    ⚠️ 用 `tmp_path_factory` 而不是 `tmp_path`：后者是函数级的，
    module 级 fixture 拿不到。
    """
    return _run_fake(tmp_path_factory.mktemp("fake_run"))


def _rows(run_dir: Path) -> list[tuple[int, str, str]]:
    """读替身写的 agents.db，返回 (行号, agent_id, 消息类型)。"""
    db = strix_events.find_agents_db(run_dir)
    assert db is not None, "替身没有写出 agents.db"
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [
            (int(row[0]), str(row[1]), json.loads(row[2]).get("type", ""))
            for row in conn.execute("SELECT id, session_id, message_data FROM agent_messages")
        ]
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════
# 产物覆盖面（读替身真的落在磁盘上的东西）
# ══════════════════════════════════════════════════════════


def test_the_run_directory_has_the_real_strix_layout(fake_run: Path) -> None:
    """产物路径要照 strix 的真实布局，否则解析层找不到它们。

    这不是形式检查：`strix_events.find_agents_db` 先看
    `strix_runs/.state/agents.db`、再 glob `strix_runs/*/.state/agents.db`。
    替身如果把库放在别处，解析层会**静默**地什么都读不到（返回空列表），
    而测试会以为"这次扫描没有事件"。
    """
    root = _strix_root(fake_run)
    assert (root / "run.json").is_file()
    assert (root / ".state" / "agents.json").is_file()
    assert (root / ".state" / "agents.db").is_file()
    assert (root / "vulnerabilities.json").is_file()

    assert strix_events.find_agents_db(fake_run) is not None


def test_the_fake_run_covers_a_three_level_agent_tree(fake_run: Path) -> None:
    """★ 三层 agent 树：root → recon → recon-crawler，另有 exploit / verify。

    旧替身的名册里只有两个 agent，而且只有 `agent-root` 有消息行 ——
    子 agent 是**摆设**。于是"事件属于不同的 agent"这条路径从没被走过，
    而它正是名册页、按 agent 过滤、`agent_count` 三处的地基。
    """
    roster = strix_events.read_agents_roster(fake_run)

    assert set(roster) == {
        "agent-root",
        "agent-recon",
        "agent-exploit",
        "agent-verify",
        "agent-failed",
    }
    # 父子链：root 是根，recon/exploit/verify 挂在它下面，
    # recon-crawler 挂在 recon 下面 —— 那才是"三层"。
    assert roster["agent-root"]["parentId"] is None
    assert roster["agent-recon"]["parentId"] == "agent-root"
    assert roster["agent-exploit"]["parentId"] == "agent-root"
    assert roster["agent-verify"]["parentId"] == "agent-root"
    assert roster["agent-failed"]["parentId"] == "agent-recon"


def test_the_fake_run_has_a_genuinely_failed_agent(fake_run: Path) -> None:
    """★ 名册的 `errors` 里有东西。

    这是"agent 失败了但没有后续事件"那条路径的唯一来源：
    `importer._ensure_agent` 里有一句
    `if info.get("error"): agent.status = AgentStatus.FAILED.value`。
    旧替身的 `errors` 恒为 `{}`，那句**没有任何测试走过** ——
    它会一直躺在代码里，直到线上第一次真失败。
    """
    roster = strix_events.read_agents_roster(fake_run)

    assert roster["agent-failed"]["status"] == "failed"
    assert roster["agent-failed"]["error"], "失败 agent 必须在 errors 里带一句话"
    # 反面：别的 agent 不该被顺带标成失败
    assert not roster["agent-root"]["error"]
    assert not roster["agent-recon"]["error"]


def test_the_fake_run_touches_every_severity_bucket(fake_run: Path) -> None:
    """★ 5 档 severity 各一条。

    汇总口径里 `bySeverity` 是 **Partial**（为 0 的档键不存在）。
    替身只产出 high / medium 的话，"`low` 与 `info` 的键该不该出现"
    这件事就永远没被真实数据验过 —— 而那正是 Partial 口径最容易错的地方。
    """
    records = json.loads((_strix_root(fake_run) / "vulnerabilities.json").read_text(encoding="utf-8"))
    severities = {record.get("severity") for record in records}

    assert severities == {"critical", "high", "medium", "low", "info"}
    assert len({record["id"] for record in records}) == len(records), "id 不能重"
    # 每条都要有 title —— 没有 title 的记录会被落库层**跳过**（记 warning），
    # 于是"5 档都覆盖了"会在导入那一步悄悄变成 4 条。
    assert all(record.get("title") for record in records)


def test_the_fake_poc_script_is_stored_in_a_markdown_fence(fake_run: Path) -> None:
    """★ `poc_script_code` 要**包在带语言标签的 markdown 围栏里**。

    依据是 strix 自己的 `report/writer.py`：agent 生成的代码字段
    （`poc_script_code` 等）存的就是围栏形式，它有一个
    `parse_fenced_code(raw) -> (language, code)` 专门拆它。

    旧替身存的是**裸代码字符串** —— 形状与真实产物不符。而"形状不符"
    最难发现：`_poc_language` 靠启发式，裸代码与围栏它都能猜出 python，
    于是替身与解析层**共享同一个错误假设**，测试永远发现不了。
    """
    records = json.loads((_strix_root(fake_run) / "vulnerabilities.json").read_text(encoding="utf-8"))
    with_poc = [record for record in records if record.get("poc_script_code")]
    assert with_poc, "至少有一条要带验证脚本，否则 poc_generated 那条链没有数据"

    for record in with_poc:
        code = record["poc_script_code"]
        assert code.startswith("```"), f"{record['id']} 的 poc_script_code 不是围栏形式"
        assert code.rstrip().endswith("```")
        assert code.startswith("```python"), "围栏要带语言标签，否则拆不出语言"


def test_the_fake_script_writes_messages_for_every_agent(fake_run: Path) -> None:
    """每个 agent 都要有消息行 —— 不然名册里的人只是名字，没有事件。"""
    agents = {agent_id for _row, agent_id, _kind in _rows(fake_run)}

    assert agents == {
        "agent-root",
        "agent-recon",
        "agent-exploit",
        "agent-verify",
        "agent-failed",
    }


def test_the_fake_run_yields_every_strix_sourced_event_type(fake_run: Path) -> None:
    """★★ 这是本文件的核心断言：替身能喂出 strix 侧**全部**有来源的类型。

    它把三件事一起钉住：解析层认得这些工具名、替身真的调了它们、
    以及**每条调用只产一条事件**（否则 `poc_generated` 与 `verified`
    会撞 `(scan_id, source_row_id)` 的唯一索引，被静默丢掉）。

    ⚠️ 判据是"替身产出的类型集合**恰好等于** `STRIX_SOURCED_TYPES`"，
    不是"包含它"。差集只在漏的时候会红，而**多出来的**类型同样要红：
    `blocked` 就是这么冒出来的 —— 它挂在一条真实扫描永远走不到的分支上
    （`wait_for_user` 在 1.6.2 里不存在），替身却因为调了那个不存在的工具名
    而产出了它，于是"8 种全有来源"这个假象一直没人戳破。
    """
    rows = strix_events.read_events(fake_run)[0]
    events = strix_events.translate(rows)
    types = {event.type for event in events}

    missing = STRIX_SOURCED_TYPES - types
    assert not missing, f"替身没喂出这些事件类型：{sorted(missing)}；实际有 {sorted(types)}"
    # 反面：不许冒出契约外的类型，也不许冒出**没有来源**的类型（`blocked`）。
    assert types <= STRIX_SOURCED_TYPES | {"completed"}, (
        f"替身产出了不该有的类型：{sorted(types - STRIX_SOURCED_TYPES - {'completed'})}"
    )

    # 每条事件的 detail.kind 必须与 type 同名（契约 §2.5 的判别式）
    for event in events:
        assert event.detail is not None, f"{event.type} 缺 detail"
        assert event.detail["kind"] == event.type

    # 幂等键的唯一性 —— 一次调用只能产一条，靠行号去重
    row_ids = [event.source_row_id for event in events]
    assert len(row_ids) == len(set(row_ids)), "有两条事件共用了同一个 source_row_id"


def test_the_fake_run_shows_the_new_events_have_real_payloads(fake_run: Path) -> None:
    """新出现的两种事件要有**能渲染的载荷**，不能只是类型对了。

    前端 `EventRow.tsx` 对未知形状是静默的（`default: return null`），
    所以"类型对了但字段名不对"的表现是**一片空白且不报错**。

    ⚠️ 这里**不再**断言 `blocked` 的载荷：`blocked` 在 strix 侧无来源，
    替身也不该产出它（见 `test_respond_to_user_does_not_become_blocked`）。
    留一条断言去验一个不该存在的事件，等于替那条假声明站台。
    """
    events = strix_events.translate(strix_events.read_events(fake_run)[0])
    by_type = {event.type: event for event in events}

    assert "blocked" not in by_type, "blocked 在 strix 侧没有来源，替身不该产出它"

    poc = by_type["poc_generated"].detail
    assert poc["language"] == "python", "围栏里的语言标签要被拆出来"
    assert "import requests" in poc["code"], "代码体里不该留着围栏行"
    assert not poc["code"].startswith("```")
    assert poc["target"], "poc_generated 的折叠行要显示 target"

    verified = by_type["verified"].detail
    assert verified["method"], "verified 的展开要显示复现方式"
    assert verified["evidence"], "verified 的展开要显示证据"


# ══════════════════════════════════════════════════════════
# 解析层：三条新分支的判据（不起进程，直接喂库）
# ══════════════════════════════════════════════════════════


def _make_agents_db(run_dir: Path, rows: list[tuple[str, dict]], *, run_name: str = "run-1") -> None:
    """按 SDK 的建表语句造一个 agents.db。

    与 `test_strix_events.py` / `test_event_import.py` 里那份**同源**。
    三份各自留着而不是抽公共：三个文件的关注点不同（解析 / 导入 / 替身
    覆盖面），抽成一个会让它们的演进互相牵扯 —— 这是既有文件里
    已经写下的判断，这里沿用。
    """
    state = run_dir / "strix_runs" / run_name / ".state"
    state.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(state / "agents.db")
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


def _one_event(run_dir: Path, name: str, args: dict, output: object) -> strix_events.StrixEvent:
    """造一次工具调用，返回它翻出来的那一条事件。"""
    _make_agents_db(
        run_dir,
        [
            ("a", {"type": "function_call", "call_id": "c1", "name": name,
                   "arguments": json.dumps(args, ensure_ascii=False)}),
            ("a", {"type": "function_call_output", "call_id": "c1",
                   "output": output if isinstance(output, str)
                   else json.dumps(output, ensure_ascii=False)}),
        ],
    )
    events = strix_events.translate(strix_events.read_events(run_dir)[0])
    assert len(events) == 1
    return events[0]


def test_update_with_a_poc_script_becomes_poc_generated(tmp_path: Path) -> None:
    """`update_vulnerability_report` 带**扁平** `poc_script_code` → `poc_generated`。

    依据（2026-10-16 在 `strix-agent` 1.6.2 上核对）：该工具的
    `params_json_schema` 里 `required = ['report_id','update_reason']`，
    顶层 26 个 properties，`poc_script_code` 就在顶层，**没有 `fields`**。

    ⚠️ 这条用例以前喂的是 `fields: {poc_script_code: ...}`，而解析层那时
    也读 `fields` —— 两边**共享同一个错误假设**，于是用例一直绿，而真实
    扫描里这个分支从来没命中过（真调用传的是扁平字段，会掉进 `tool_call`
    兜底）。所以这条现在必须喂**真形状**：替身与用例都按
    `params_json_schema` 写，而不是按"看起来合理"写。

    语义：给**已有**发现补上验证脚本 —— 那正是"产出了 PoC"这个动作。
    而 `create_vulnerability_report` 是"第一次登记发现"，它的映射是
    `discovery`，两者不能混。
    """
    event = _one_event(
        tmp_path,
        "update_vulnerability_report",
        {
            "report_id": "vuln-7",
            "update_reason": "补上可复现的验证脚本。",
            "poc_script_code": "```python\nimport requests\nprint(requests.get('https://x').status_code)\n```",
            "target": "api.example.com",
        },
        {"success": True, "action": "updated", "report_id": "vuln-7",
         "updated_fields": ["poc_script_code", "target"]},
    )

    assert event.type == "poc_generated"
    assert event.detail["language"] == "python"
    assert "import requests" in event.detail["code"]
    assert event.detail["code"].rstrip().endswith("status_code)"), "围栏行不该混进代码体"
    assert event.detail["target"] == "api.example.com"
    assert event.agent_phase == "verify"


def test_a_nested_fields_payload_still_works_as_a_fallback(tmp_path: Path) -> None:
    """嵌套 `fields` 作为**兜底**仍然能命中 —— 扁平优先，但别把旧形状丢掉。

    ⚠️ 兜底不是"历史包袱"，是**防静默退化**：如果 strix 将来把可替换字段
    收进一个 `fields` 字典（1.6.2 不是这样，但形状由上游单方面决定），
    读扁平的代码会读不到值，一路掉进 `tool_call` 兜底 —— 而那个退化在
    活动流里看起来和"这次确实没改什么"一模一样，没人会发现。
    留这条兜底，退化就只是"少了一条语义"，不是"事件凭空消失"。

    优先级也要钉住：**扁平优先**。两边都有值时读扁平的，因为那是真 schema。
    """
    nested_only = _one_event(
        tmp_path / "nested-only",
        "update_vulnerability_report",
        {"report_id": "vuln-14", "update_reason": "补脚本。",
         "fields": {"poc_script_code": "```bash\ncurl -s https://x/debug/env\n```"}},
        {"success": True, "report_id": "vuln-14", "updated_fields": ["poc_script_code"]},
    )
    assert nested_only.type == "poc_generated"
    assert nested_only.detail["language"] == "bash"

    # 两边都有：扁平那份胜出。
    # ⚠️ 必须换一个目录：`_make_agents_db` 是往同一个 agents.db 里追加行，
    # 而 `read_events` 读的是**全部**行 —— 共用 tmp_path 会把上一次调用
    # 也翻出来，`len(events) == 1` 当场变 2。
    both = _one_event(
        tmp_path / "both-shapes",
        "update_vulnerability_report",
        {"report_id": "vuln-15", "update_reason": "补脚本。",
         "poc_script_code": "```python\nprint('flat')\n```",
         "fields": {"poc_script_code": "```bash\ncurl -s https://x/\n```"}},
        {"success": True, "report_id": "vuln-15", "updated_fields": ["poc_script_code"]},
    )
    assert both.type == "poc_generated"
    assert both.detail["language"] == "python", "扁平（真 schema）优先于嵌套兜底"
    assert "flat" in both.detail["code"]


def test_update_without_a_target_falls_back_to_the_report_id(tmp_path: Path) -> None:
    """参数里没有 `target` 时退回报告 id —— 而不是编一个目标名。

    前端 `poc_generated` 的折叠行显示 `detail.target`；空串会让那一行
    看起来像渲染坏了，而编一个目标名是**编造**。报告 id 至少能让人知道
    这段脚本属于哪条发现。
    """
    event = _one_event(
        tmp_path,
        "update_vulnerability_report",
        {"report_id": "vuln-8", "update_reason": "补脚本。",
         "poc_script_code": "```bash\ncurl -s https://x/debug/env\n```"},
        {"success": True, "report_id": "vuln-8", "updated_fields": ["poc_script_code"]},
    )

    assert event.type == "poc_generated"
    assert event.detail["language"] == "bash"
    assert event.detail["target"] == "vuln-8"


def test_update_with_high_confidence_becomes_verified(tmp_path: Path) -> None:
    """**扁平** `confidence == "high"` → `verified`。

    ⚠️ 语义近似，如实记：strix **没有**"复现"这个独立信号。
    这里用的是**本项目自己的**口径 `_CONFIDENCE_TO_VERIFICATION`
    （`app/findings/strix_vulns.py`：`high` → `verified`），不另立一套。
    strix 自己的 `skills/analysis/counterevidence.md` 也写 `high` 要求
    "可执行复现"，所以这个近似不是我们编的。

    ⚠️ 与上面那条同因：旧用例喂 `fields.confidence`，解析层也读 `fields`，
    两边一起错。`confidence` 在真 schema 里是**顶层**参数。
    """
    event = _one_event(
        tmp_path,
        "update_vulnerability_report",
        {"report_id": "vuln-9", "update_reason": "在预发环境复现了同一问题。",
         "confidence": "high", "evidence": "HTTP/1.1 200 OK"},
        {"success": True, "report_id": "vuln-9", "updated_fields": ["confidence"]},
    )

    assert event.type == "verified"
    assert event.detail["kind"] == "verified"
    assert event.detail["method"] == "在预发环境复现了同一问题。"
    assert "HTTP/1.1 200 OK" in event.detail["evidence"]


def test_update_without_script_or_high_confidence_stays_a_tool_call(tmp_path: Path) -> None:
    """反面：**不能**把每次 `update_` 都当成 poc_generated / verified。

    一次 `update_vulnerability_report` 也可能只是改了措辞、补了
    `remediation_steps`。把它翻成"产出了 PoC"或"已验证"是**编造**，
    而编造的事件在活动流里看起来和真的一样。
    """
    event = _one_event(
        tmp_path,
        "update_vulnerability_report",
        {"report_id": "vuln-10", "update_reason": "措辞调整。",
         "remediation_steps": "换一种说法。"},
        {"success": True, "report_id": "vuln-10", "updated_fields": ["remediation_steps"]},
    )

    assert event.type == "tool_call"
    assert event.detail["tool"] == "update_vulnerability_report"


def test_update_with_medium_confidence_stays_a_tool_call(tmp_path: Path) -> None:
    """只有 `high` 才算 verified —— `medium` 是我们自己的 `partial`，不是 verified。

    `_CONFIDENCE_TO_VERIFICATION`：high→VERIFIED、medium→PARTIAL、low→NONE。
    把 medium 也翻成 verified 会让"已验证"这个标签失去意义。
    """
    event = _one_event(
        tmp_path,
        "update_vulnerability_report",
        {"report_id": "vuln-11", "update_reason": "只在测试环境复现过。",
         "confidence": "medium"},
        {"success": True, "report_id": "vuln-11", "updated_fields": ["confidence"]},
    )

    assert event.type == "tool_call"


def test_a_poc_script_wins_over_high_confidence(tmp_path: Path) -> None:
    """★ 同时带脚本又带高置信度时，**`poc_generated` 胜出**（不是两条事件）。

    `source_row_id` 同时是幂等键与游标，`(scan_id, source_row_id)` 上有
    部分唯一索引 —— **一行只能产出一条事件**。所以那两个判据是
    `if / elif` 而不是两个独立的 `if`；写成两个 `if` 的话第二条会撞
    唯一索引被 `_persist` 静默丢掉（返回 False），而事件流里少的那条
    没有任何提示。

    选 `poc_generated` 的理由：脚本是用户能直接拿去复现的东西；
    置信度在 finding 上本来就有一列（`verification`），活动流里少报它损失更小。
    """
    event = _one_event(
        tmp_path,
        "update_vulnerability_report",
        {"report_id": "vuln-12", "update_reason": "补脚本并把置信度提到高。",
         "poc_script_code": "```python\nprint('x')\n```", "confidence": "high"},
        {"success": True, "report_id": "vuln-12",
         "updated_fields": ["confidence", "poc_script_code"]},
    )

    assert event.type == "poc_generated"


def test_respond_to_user_does_not_become_blocked(tmp_path: Path) -> None:
    """★★ 回归钉子：`respond_to_user` **不**映射成 `blocked`，而是 `failed`。

    这里守的是 2026-10-16 撤掉的一条**假声明**。旧代码把
    `tool_name == "wait_for_user"` 映射成 `blocked`，依据是
    "`tools/wait_for_user/tool.py` 的 docstring" —— 而那个工具
    **在 `strix-agent` 1.6.2 里不存在**：全包 `*.py` 搜 `wait_for_user`
    零命中，`strix/tools/` 下是 `respond`，真名 `respond_to_user`
    （`strix/tools/respond/tool.py:17`）。它还在
    `strix/agents/factory.py:688-690` 受 `if interactive:` 门控，
    而 `app/scans/runner.py` 传的是 `--non-interactive` —— **双重不可达**。

    所以 `blocked` 在 strix 侧**没有来源**，本项目也不该假装有。
    非交互模式下 `respond_to_user` 的真实返回是
    `{"success": False, "error": "No user is attached to an autonomous run. …"}`
    （`tools/respond/tool.py` 里那条 `if not interactive` 分支），
    所以它落到 `failed` 是对的 —— **扫描并没有"停下来等人"，而是这个动作
    在当前模式下不被允许**。把这两件事混起来，会让一次真实的模式错误
    看起来像"扫描在正常地等人"。
    """
    event = _one_event(
        tmp_path,
        "respond_to_user",
        {"message": "需要人工确认后才能继续。"},
        {"success": False,
         "error": ("No user is attached to an autonomous run. Keep working, and call "
                   "finish_scan (root) or agent_finish (subagent) when the task is done.")},
    )

    assert event.type == "failed", "respond_to_user 在非交互模式下是失败，不是 blocked"
    assert "No user is attached" in event.detail["error"]


def test_a_high_confidence_create_is_still_a_discovery(tmp_path: Path) -> None:
    """★★ 回归钉子：`create_vulnerability_report` + `confidence: high`
    **仍然**是 `discovery`，不是 `verified`。

    这是 B-2 动手前发现的一处**真实冲突**：`verified` 一度打算挂在
    `create_` 上，而那会让 `tests/test_strix_events.py` 的
    `test_vulnerability_report_becomes_discovery` 变红。

    更要紧的是它**净亏**：`create_` 的既有映射是 `discovery`
    （"第一次登记一条发现"），改成 `verified` 是拿一个已有映射换一个新映射，
    而一次调用只能产一条事件 —— 于是 `discovery` 就没了。
    而 `update_vulnerability_report` 是 agent **回去复核**已有发现的地方，
    "确认"这个动作本来就发生在那里。

    所以 `verified` 挂在 `update_` 上。这条用例守着这个决定：
    谁把它改回 `create_`，这条会红。
    """
    event = _one_event(
        tmp_path,
        "create_vulnerability_report",
        {"title": "未授权读取环境变量", "evidence": "HTTP/1.1 200 OK", "confidence": "high"},
        {"success": True, "report_id": "vuln-13", "severity": "high", "cvss_score": 7.5},
    )

    assert event.type == "discovery"
    assert event.detail["reportId"] == "vuln-13"
    assert event.detail["confidence"] == "high"


# ══════════════════════════════════════════════════════════
# 落库层：替身产物进得了库，且各档都进对
# ══════════════════════════════════════════════════════════


@pytest.fixture()
def scan_at(db_session, tmp_path: Path):
    """一条 run_dir 指向 `tmp_path` 的已完成扫描。"""
    from tests.factories import make_scan

    created = make_scan(db_session, status="completed")
    created.run_dir = str(tmp_path / "scan")
    created.runner = "fake"
    db_session.commit()
    Path(created.run_dir).mkdir(parents=True, exist_ok=True)
    return created


def test_the_whole_fake_run_imports_and_lands_in_the_database(
    db_session, scan_at, fake_run: Path
) -> None:
    """★ 端到端：替身的产物 → 解析 → 落库，strix 侧 6 种类型都进得了库。

    这条是"替身能不能代表一次真扫描"这个问题的**最直接回答**：
    它走的是 `importer.import_events` 这条生产路径。
    """
    scan_at.run_dir = str(fake_run)
    db_session.commit()

    written = importer.import_events(db_session, scan_at)
    assert written > 0

    events = list(
        db_session.execute(
            select(ActivityEvent).where(ActivityEvent.scan_id == scan_at.id)
        ).scalars()
    )
    types = {event.type for event in events}
    assert types >= STRIX_SOURCED_TYPES, f"库里少了这些类型：{sorted(STRIX_SOURCED_TYPES - types)}"
    # 契约 §2.4：每条事件都要有 agentId 与 agentPhase
    assert all(event.agent_id for event in events)
    assert all(event.agent_phase in {"recon", "scan", "exploit", "verify", "report"}
               for event in events)

    # 幂等：再导一次不该多出任何一条
    assert importer.import_events(db_session, scan_at) == 0


def test_the_failed_agent_lands_as_failed_in_our_database(db_session, scan_at, fake_run: Path) -> None:
    """★★ 替身名册里那个失败 agent，在**我们库里**必须落 `failed`。

    这条守的是 `importer._ensure_agent` 里那句
    `if info.get("error"): agent.status = AgentStatus.FAILED.value`。

    ⚠️ 它守的是"名册里 `errors` 有值"这条分支 —— 与"名册里
    `statuses` 写 failed"是**两条不同的路径**：`_STATUS_MAP` 只映射
    `statuses`，`errors` 是另一条覆盖。旧替身的 `errors` 恒为 `{}`，
    所以这条分支此前**没有任何测试走过**。
    """
    scan_at.run_dir = str(fake_run)
    db_session.commit()
    importer.import_events(db_session, scan_at)

    agent = db_session.get(Agent, strix_events.stable_agent_id("agent-failed", scope=scan_at.id))
    assert agent is not None, "失败 agent 没进名册"
    assert agent.status == AgentStatus.FAILED.value
    assert agent.role == "recon", "名字里有 recon ⇒ 角色该判成信息收集"

    # 反面：别的 agent 不该被一起标成失败。
    # 名册里 `agent-verify` 的状态是 `waiting`（不是 failed），
    # 而 `agent-root` / `agent-recon` / `agent-exploit` 是 `completed` ——
    # 这条断言守住"`errors` 的覆盖只作用于那一个 agent"。
    others = list(
        db_session.execute(select(Agent).where(Agent.scan_id == scan_at.id)).scalars()
    )
    failed = [a for a in others if a.status == AgentStatus.FAILED.value]
    assert [a.id for a in failed] == [agent.id], "只有名册里 errors 有值的那个才是失败"
    verify = db_session.get(Agent, strix_events.stable_agent_id("agent-verify", scope=scan_at.id))
    assert verify is not None and verify.status == AgentStatus.IDLE.value, (
        "名册里 `waiting` ⇒ 我们这边 IDLE：`waiting` 在 strix 里是"
        "「等子 agent 回来 / 预算暂停恢复」，不是「扫描停下等人」，"
        "所以它既不是 DONE 也不是 BLOCKED"
    )


def test_the_fake_vulnerabilities_land_in_every_severity_bucket(
    db_session, scan_at, fake_run: Path
) -> None:
    """★ 5 档 severity 从替身一路走到 `findings` 表。

    这条把"替身覆盖了 5 档"与"落库层认得 5 档"接起来：
    单看任一侧都可能是绿的，而合起来才是"用户真的会在界面上看到 5 档"。
    """
    scan_at.run_dir = str(fake_run)
    db_session.commit()

    result = findings_service.import_from_run(db_session, scan_at)
    assert result.created == 5, f"应导入 5 条，实际 {result}"

    findings = list(
        db_session.execute(select(Finding).where(Finding.scan_id == scan_at.id)).scalars()
    )
    assert {finding.severity for finding in findings} == {
        Severity.CRITICAL.value,
        Severity.HIGH.value,
        Severity.MEDIUM.value,
        Severity.LOW.value,
        Severity.INFO.value,
    }


def test_the_fenced_poc_language_survives_the_import(db_session, scan_at, fake_run: Path) -> None:
    """围栏里的语言标签要一路走到 `finding.poc_language`。

    `_poc_language` 是启发式的（strix 不存语言），它从代码体里猜。
    这条钉住"围栏形式不会把它猜错" —— 若替身改回裸代码，语言仍然
    猜得出 python，但**围栏行会混进 `poc_code`**，前端代码块里会多出
    一行 ```，那是用户能直接看见的脏东西。
    """
    scan_at.run_dir = str(fake_run)
    db_session.commit()
    findings_service.import_from_run(db_session, scan_at)

    findings = list(
        db_session.execute(select(Finding).where(Finding.scan_id == scan_at.id)).scalars()
    )
    with_poc = [finding for finding in findings if finding.poc_code]
    assert with_poc, "替身至少该有一条带 PoC 的发现"

    for finding in with_poc:
        assert finding.poc_language == "python", f"{finding.source_ref} 的语言判错了"
        assert not finding.poc_code.startswith("```"), "围栏行不该进 poc_code"


def test_two_scans_never_share_one_agent_row(db_session, fake_run: Path) -> None:
    """★ 回归：同库两次扫描导入同一批事件，绝不能共用一条 agent 行。

    这是一次真实事故（v0.47 自研引擎承接渗透之后）：我们自研引擎的
    agent_ref 是**固定**的（`agent-engine` / `agent-root`），而 strix 的
    agent id 是全局会话 id、天然不重。`stable_agent_id` 只哈希 ref，
    于是 `sa_agent-engine` 在**所有扫描**里都是同一个字符串
    → 第二次扫描的 `_ensure_agent` 先 `db.get(Agent, ...)` 命中**上一次**
    那一行、把事件挂过去，自己的 `scan_id` 却没变。症状是：名册里某条
    历史扫描的 agent 事件数一路涨，而**当前扫描永远是 0 个智能体**。
    单条扫描的单测覆盖不到它 —— 必须两条扫描同库才暴露。
    """
    from tests.factories import make_scan

    scans = []
    for _ in range(2):
        s = make_scan(db_session, status="completed")
        s.run_dir = str(fake_run)
        s.runner = "fake"
        db_session.commit()
        scans.append(s)

        importer.import_events(db_session, s)

    first, second = scans
    agents_by_scan = {
        s.id: list(
            db_session.execute(
                select(Agent).where(Agent.scan_id == s.id)
            ).scalars()
        )
        for s in scans
    }
    assert agents_by_scan[first.id], "第一条扫描一个 agent 都没有"
    assert agents_by_scan[second.id], (
        "第二条扫描一个 agent 都没有 —— 它的 agent 被并进了上一条扫描"
    )
    # 最硬的一条：两条扫描的 agent id 集合必须**不相交**。
    first_ids = {a.id for a in agents_by_scan[first.id]}
    second_ids = {a.id for a in agents_by_scan[second.id]}
    assert not (first_ids & second_ids), (
        f"两条扫描共用了 agent 行：{sorted(first_ids & second_ids)}"
    )
    # 每条事件都要挂在本扫描自己的 agent 名下（不能跨扫描串）。owner 指每条
    # 事件所属 agent 的 scan_id,必须等于事件自己的 scan_id。
    for s in scans:
        owned = {a.id for a in agents_by_scan[s.id]}
        event_agent_ids = {
            e.agent_id
            for e in db_session.execute(
                select(ActivityEvent).where(ActivityEvent.scan_id == s.id)
            ).scalars()
        }
        assert event_agent_ids <= owned, (
            f"扫描 {s.id} 里有事件挂在了别的扫描的 agent 名下：{event_agent_ids - owned}"
        )


def test_the_fake_run_stays_fast_enough_to_be_the_default_backend(fake_run: Path) -> None:
    """替身要**快**：它是测试与 CI 的默认后端。

    这条不是性能测试，是一条**契约**：`FakeRunner` 的整个存在理由就是
    "让队列、进程监督、杀进程树、退出码解释这些逻辑真的在跑，而不做真实攻击"。
    它要是慢到几十秒，人们就会开始绕开它 —— 而绕开它的下一步就是
    在测试里打桩，那正是它要防的。

    阈值取 60 秒：conftest 设 `SUPEX_FAKE_SCAN_SECONDS=1.0`，起孙进程
    与写产物加起来远低于它。给足余量是为了不让这条在负载高的机器上闪红
    （那种红会让人把整条断言删掉，比没有更坏）。
    """
    started = time.monotonic()
    run_dir = fake_run.parent / "timing_probe"
    _run_fake(run_dir, run_id="timing")
    elapsed = time.monotonic() - started

    assert elapsed < 60, f"替身跑了 {elapsed:.1f} 秒，太慢了"
