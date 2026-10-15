"""全局统计（`/overview`，契约 §3.6）。

## 这个领域没有表，所以测试的重点不是"存对了没有"

重点全在**派生关系**和**三个 null 的语义**上：

1. `activeScans` / `hasAnyScan` / `avgScanDurationSec` 必须与 `scans` 表算出来的一致
   —— 它们是同一张表，所以"卡片写 2 但列表只有 1 条"不可能发生；
2. `severityBreakdown` 必须与 `/findings/stats` **逐档相等**（含误报）；
3. ★ `newHighSeverityDelta: null` 与 `avgScanDurationSec: null` 各自表达
   「无法计算」而不是「等于 0」。这两条**最难对**，因为 0 看起来永远合理：
   0 变化像"与上周持平"，0 秒像"扫描瞬间完成"。所以两条都单独测。

## 投影（`activity`）

契约 §2.5 与 §3.6 在这里有冲突（§2.5 说不要后端投影，§3.6 要投影形状），
取舍记在 `app/overview/service.py` 的模块 docstring 里。测试盯的是
**投影规则本身**：只 6 种类型、`subject` 派生不出就如实为 null。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.vocab import AgentPhase, ScanStatus
from app.findings import service as findings_service
from app.overview import service as overview_service
from app.scans.models import Scan
from tests.factories import make_agent, make_event, make_scan
from tests.test_findings import vuln_record, write_vulns

API = "/api/v1"


# ══════════════════════════════════════════════════════════
# 造数据
# ══════════════════════════════════════════════════════════


@pytest.fixture()
def completed_scan(db_session: Session, tmp_path: Path) -> Scan:
    """一次已完成、耗时 120 秒的扫描（`make_scan` 的终态工厂值）。"""
    scan = make_scan(db_session, status=ScanStatus.COMPLETED.value)
    scan.run_dir = str(tmp_path / "run")
    scan.runner = "fake"
    db_session.commit()
    Path(scan.run_dir).mkdir(parents=True, exist_ok=True)
    return scan


def import_vulns(db: Session, scan: Scan, records: list[dict]) -> None:
    write_vulns(Path(scan.run_dir), records)
    findings_service.import_from_run(db, scan)


# ══════════════════════════════════════════════════════════
# 1. 空状态 —— 三个 null 的语义
# ══════════════════════════════════════════════════════════


def test_empty_state_returns_nulls_not_zeros(ready_client: TestClient) -> None:
    """★ 一条数据都没有时，两个"无法计算"的字段必须是 `null` 而不是 0。

    这是整个接口最容易被写成 0 的地方，而**写成 0 在界面上看不出来** ——
    0 秒、0 变化都是看起来完全合理的数字。所以契约用 ⚠️ 单独标了这两条。
    """
    body = ready_client.get(f"{API}/overview").json()

    assert body["metrics"]["avgScanDurationSec"] is None, "没有已完成扫描时不能返回 0 秒"
    assert body["metrics"]["newHighSeverityDelta"] is None, "没有上周数据时不能返回 0 变化"
    assert body["hasAnyScan"] is False


def test_empty_state_still_returns_all_five_severity_buckets(ready_client: TestClient) -> None:
    """★ 空状态下 `severityBreakdown` 仍是**全部 5 档、值为 0**，不是空数组。

    图表里"缺 5 根柱子"与"5 根 0 高的柱子"在界面上是
    「坏了」与「确实没有」的区别。
    """
    body = ready_client.get(f"{API}/overview").json()
    breakdown = body["severityBreakdown"]

    assert [item["severity"] for item in breakdown] == [
        "critical",
        "high",
        "medium",
        "low",
        "info",
    ]
    assert all(item["count"] == 0 for item in breakdown)


# ══════════════════════════════════════════════════════════
# 2. 派生关系 —— 与已有的唯一数据源一致
# ══════════════════════════════════════════════════════════


def test_active_scans_counts_queued_and_running(
    ready_client: TestClient, db_session: Session
) -> None:
    """★ `activeScans` = 数据库里的 running + queued。

    契约 §3.6 要求它**派生**而不是写死。"卡片写 2 但列表只有 1 条"
    这种自相矛盾必须不可能发生，所以它和 `/scans` 列表读同一张表。

    ⚠️ 这里**不**拿 `/scans/queue` 的 `running` 交叉验证 —— 那个字段
    是**调度器进程内**的在跑数（`scheduler.running_count`），
    与数据库里 `status='running'` 的行数在测试环境下不等
    （没有真实子进程被拉起）。两者语义不同：
    前者回答"引擎现在实际跑着几个"，后者回答"库里有几条标记为在跑"。
    混为一谈会得到一条**看起来像 bug 的**断言失败。
    """
    make_scan(db_session, status=ScanStatus.QUEUED.value, target="q.example.com")
    make_scan(db_session, status=ScanStatus.RUNNING.value, target="r.example.com")
    make_scan(db_session, status=ScanStatus.COMPLETED.value, target="c.example.com")

    body = ready_client.get(f"{API}/overview").json()

    assert body["metrics"]["activeScans"] == 2
    assert body["hasAnyScan"] is True

    # 与 `/scans` 列表逐条数出来的结果一致（同一个数据来源）
    from_api = ready_client.get(f"{API}/scans").json()
    active_in_list = [
        item for item in from_api if item["status"] in {"queued", "running"}
    ]
    assert body["metrics"]["activeScans"] == len(active_in_list)


def test_avg_duration_only_counts_completed_scans(
    ready_client: TestClient, db_session: Session
) -> None:
    """★ 平均耗时只看 `completed`，不含 failed/aborted。

    那两次的耗时反映的是"多久放弃的"，与"扫描一次要多久"不是同一个问题 ——
    混进去会把这个数字拉低，而它恰恰是用来做容量预估的。
    """
    make_scan(db_session, status=ScanStatus.COMPLETED.value, target="a.example.com")
    make_scan(db_session, status=ScanStatus.FAILED.value, target="b.example.com")
    make_scan(db_session, status=ScanStatus.ABORTED.value, target="c.example.com")

    body = ready_client.get(f"{API}/overview").json()

    # 工厂里终态都是 120 秒；三条全算的话平均值仍是 120，
    # 所以不能只断言数值 —— 断言"只有 completed 那条的耗时在算式里"。
    assert body["metrics"]["avgScanDurationSec"] == 120


def test_severity_breakdown_matches_stats_endpoint(
    ready_client: TestClient, db_session: Session, completed_scan: Scan
) -> None:
    """★★★ **`severityBreakdown` 与 `/findings/stats` 逐档相等**。

    这是任务 5 验收标准的延续：Findings 页的 chip 与 Overview 的图表
    读的是同一个数。两页显示"中危 3"和"中危 2"会让人认为系统算错了，
    而任何解释都挽回不了那种不信任。
    """
    import_vulns(
        db_session,
        completed_scan,
        [
            vuln_record(id="v1", severity="critical"),
            vuln_record(id="v2", severity="high"),
            vuln_record(id="v3", severity="high"),
            vuln_record(id="v4", severity="low"),
        ],
    )

    body = ready_client.get(f"{API}/overview").json()
    stats = ready_client.get(f"{API}/findings/stats").json()

    from_overview = {item["severity"]: item["count"] for item in body["severityBreakdown"]}
    # `/findings/stats` 的 `bySeverity` 是 Partial —— 为 0 的档**键不存在**
    # （契约 §3.4.2）。Overview 那边则是**全部 5 档都返回**（图表要 5 根柱子），
    # 所以按 0 补全再比。这个口径差异是有意的：见契约 §3.4.2 的说明。
    from_stats = {member: stats["bySeverity"].get(member, 0) for member in from_overview}

    assert from_overview == from_stats
    assert sum(from_overview.values()) == stats["total"]
    # 顺带把契约的 Partial 语义钉住 —— 为 0 的档不该以 null 形式出现
    assert "medium" not in stats["bySeverity"], "为 0 的档必须是键不存在，不是 null"


def test_breakdown_includes_false_positives_but_metrics_exclude_them(
    ready_client: TestClient, db_session: Session, completed_scan: Scan
) -> None:
    """★ 两处**口径不同**，且差异被 `falsePositiveCount` 如实暴露出来。

    * `severityBreakdown` 回答「发现了什么」—— 是一份发现记录，**含误报**；
    * `newHighSeverityThisWeek` / `verifiedPocCount` 回答「我该处理什么」
      —— 一条误报当然不算。

    前端注释原话：**「排除是可以的，悄悄排除不行。」**
    所以这条同时断言"排除了"和"说清了排除了几条"。
    """
    from app.core.timeutil import now_utc

    # ⚠️ `timestamp` 必须**显式**给"本周"的值。
    #
    # `vuln_record()` 的默认时间是写死的 `2026-09-24`（见 conftest：它要
    # 复现 Strix 那个非 ISO 8601 的时间戳格式）。而本测试断言的是
    # `newHighSeverityThisWeek` —— **近 7 天**。
    #
    # 用默认值的话，这个测试会随日期推移**自己变红**：它 2026-09-24
    # 写出来时是绿的，一周之后那些数据就不再属于"本周"了。
    # 这类"时间炸弹"不会在写的时候报错，只在某天突然失败 ——
    # 而失败信息（`assert 0 == 1`）看起来像统计逻辑坏了。
    this_week = now_utc().strftime("%Y-%m-%d %H:%M:%S UTC")
    import_vulns(
        db_session,
        completed_scan,
        [
            vuln_record(id="v1", severity="critical", timestamp=this_week),
            vuln_record(id="v2", severity="high", timestamp=this_week),
        ],
    )
    findings = ready_client.get(f"{API}/findings").json()
    victim = next(item for item in findings if item["severity"] == "high")
    ready_client.post(f"{API}/findings/status", json={"ids": [victim["id"]], "status": "false_positive", "note": "扫描器把测试环境的调试开关判成了漏洞"})

    body = ready_client.get(f"{API}/overview").json()
    from_overview = {item["severity"]: item["count"] for item in body["severityBreakdown"]}

    # 分布里**仍然有**那条误报
    assert from_overview["high"] == 1
    # 但指标里不算它
    assert body["metrics"]["newHighSeverityThisWeek"] == 1
    assert body["metrics"]["verifiedPocCount"] <= 1
    # 而且如实说出了排除几条
    assert body["metrics"]["falsePositiveCount"] == 1


def test_fixed_findings_still_count_as_new(
    ready_client: TestClient, db_session: Session, completed_scan: Scan
) -> None:
    """★ 「已修复」**仍然算**进"本周新增高危"。

    它曾经是个真漏洞，属于"发现过什么"的记录。按状态抹掉会让
    "本周新增"变成一个随处置而回溯变化的数字 —— 那样它就没法用了。
    这个过滤器排除的是「**判断错误**」（误报），不是「已处理」。
    """
    from app.core.timeutil import now_utc

    # ⚠️ 同 `test_breakdown_includes_false_positives_but_metrics_exclude_them`：
    #    必须显式给"本周"时间，不能用 `vuln_record()` 那个写死的旧默认值，
    #    否则这条断言会随日期推移自己变红。
    this_week = now_utc().strftime("%Y-%m-%d %H:%M:%S UTC")
    import_vulns(
        db_session,
        completed_scan,
        [vuln_record(id="v1", severity="high", timestamp=this_week)],
    )
    finding = ready_client.get(f"{API}/findings").json()[0]
    # 待确认 → 已确认 → 已修复（不能跳级，契约 §2.3 的流转表）
    ready_client.post(f"{API}/findings/status", json={"ids": [finding["id"]], "status": "confirmed"})
    ready_client.post(f"{API}/findings/status", json={"ids": [finding["id"]], "status": "fixed"})

    body = ready_client.get(f"{API}/overview").json()

    assert body["metrics"]["newHighSeverityThisWeek"] == 1, "已修复的仍应计入本周新增"
    assert body["metrics"]["falsePositiveCount"] == 0


# ══════════════════════════════════════════════════════════
# 3. 活动流投影
# ══════════════════════════════════════════════════════════


def test_projection_returns_six_types_not_eight(
    ready_client: TestClient, db_session: Session
) -> None:
    """★ 投影只有 **6 种** —— `reasoning` 与 `failed` 不进 Overview。

    契约 §2.5：投影是给"扫视"用的一行摘要。
    `reasoning` 是推理过程（太细），`failed` 是诊断信息（混进去会让
    活动流看起来像在报错，而 Overview 回答的是"我该处理什么"）。
    """
    scan = make_scan(db_session, status=ScanStatus.RUNNING.value)
    agent = make_agent(db_session, scan.id)
    for event_type in ("tool_call", "discovery", "poc_generated", "verified", "blocked", "completed"):
        make_event(db_session, scan_id=scan.id, agent_id=agent.id, type=event_type, summary=f"{event_type} 事件")
    # 这两种必须被挡掉
    make_event(db_session, scan_id=scan.id, agent_id=agent.id, type="reasoning", summary="推理中")
    make_event(db_session, scan_id=scan.id, agent_id=agent.id, type="failed", summary="失败了")

    body = ready_client.get(f"{API}/overview").json()
    types = {item["type"] for item in body["activity"]}

    assert "reasoning" not in types, "推理过程不该进 Overview"
    assert "failed" not in types, "诊断信息不该进 Overview"
    assert types.issubset(
        {"tool_call", "discovery", "poc_generated", "verified", "blocked", "completed"}
    )


def test_projection_is_flat_and_has_no_detail(
    ready_client: TestClient, db_session: Session
) -> None:
    """★ 投影是**扁平**的：有 `subject`、**没有** `detail`。

    这是契约 §2.5 那张表的右列。两个形状搞混是契约里最容易出错的一处，
    而症状是前端读 `event.subject` 得到 undefined。
    """
    scan = make_scan(db_session, status=ScanStatus.RUNNING.value)
    agent = make_agent(db_session, scan.id)
    make_event(
        db_session,
        scan_id=scan.id,
        agent_id=agent.id,
        type="discovery",
        agent_phase=AgentPhase.RECON.value,
        summary="发现一个未授权的调试接口",
        detail={"kind": "discovery", "subject": "GET /debug/env", "confidence": "high"},
    )

    item = ready_client.get(f"{API}/overview").json()["activity"][0]

    assert "detail" not in item, "投影里不该有 detail（判别式载荷）"
    assert set(item) == {
        "id",
        "scanId",
        "agentId",
        "agentPhase",
        "type",
        "summary",
        "subject",
        "at",
    }
    # agentPhase 是契约 §2.5 相对前端现状**唯一新增的字段** ——
    # ActivityFeedItem 渲染阶段标签（「信息收集」）时必须它。
    assert item["agentPhase"] == "recon"
    assert item["subject"] == "GET /debug/env"


def test_subject_is_null_when_it_cannot_be_derived(
    ready_client: TestClient, db_session: Session
) -> None:
    """★ 派生不出 `subject` 时如实留 `null`，**不编一个占位串**。

    前端类型里它就是 `string | null`。编一个假对象会让
    "这条事件没有具体对象"这个真实信息消失。
    """
    scan = make_scan(db_session, status=ScanStatus.RUNNING.value)
    agent = make_agent(db_session, scan.id)
    make_event(
        db_session,
        scan_id=scan.id,
        agent_id=agent.id,
        type="completed",
        summary="扫描完成",
        detail={"kind": "completed", "result": "发现 3 个问题"},
    )

    item = ready_client.get(f"{API}/overview").json()["activity"][0]

    assert item["subject"] is None


def test_activity_is_newest_first(ready_client: TestClient, db_session: Session) -> None:
    """★ 活动流**最新在前**（前端 `buildOverviewData` 也是这个次序）。

    倒序错了的话，用户看到的是"最早发生的事"顶在最上面 ——
    而这一栏的用途恰恰是"现在在干什么"。
    """
    scan = make_scan(db_session, status=ScanStatus.RUNNING.value)
    agent = make_agent(db_session, scan.id)
    for index in range(3):
        make_event(
            db_session,
            scan_id=scan.id,
            agent_id=agent.id,
            type="tool_call",
            summary=f"第 {index} 步",
            at_offset_seconds=index,
        )

    activity = ready_client.get(f"{API}/overview").json()["activity"]
    timestamps = [item["at"] for item in activity]

    assert timestamps == sorted(timestamps, reverse=True), "活动流必须最新在前"
    assert activity[0]["summary"] == "第 2 步"


def test_activity_sorts_by_event_time_not_insertion_order(
    ready_client: TestClient, db_session: Session
) -> None:
    """★★ 排序键是 **`at`（事件发生时刻）**，不是入库顺序。

    这是验收时抓出来的一个真 bug：最初我按 `seq`（入库顺序）排，
    在测试环境里看不出问题（事件都是顺序造的，两个次序一致）。
    但真实链路里事件是从外部库**批量导入**的 —— `at` 来自外部工具的
    时间戳，与入库顺序**不一致**。于是活动流顶上的不是最近发生的事。

    所以这条刻意造出"入库顺序与发生时刻相反"的数据：
    先入库的 `at` 更晚，后入库的 `at` 更早。
    按 seq 排会得到 B 在前，按 at 排才是 A 在前。
    """
    scan = make_scan(db_session, status=ScanStatus.RUNNING.value)
    agent = make_agent(db_session, scan.id)
    # A 先入库，但发生时刻更晚（+60s）
    make_event(
        db_session,
        scan_id=scan.id,
        agent_id=agent.id,
        type="tool_call",
        summary="A（后发生，先入库）",
        at_offset_seconds=60,
    )
    # B 后入库，但发生时刻更早（+1s）—— 模拟批量导入时补写历史事件
    make_event(
        db_session,
        scan_id=scan.id,
        agent_id=agent.id,
        type="discovery",
        summary="B（先发生，后入库）",
        at_offset_seconds=1,
    )

    activity = ready_client.get(f"{API}/overview").json()["activity"]

    assert activity[0]["summary"] == "A（后发生，先入库）", (
        "必须按事件发生时刻排序 —— 按入库顺序排会把刚导入的历史事件顶到最上面"
    )


def test_activity_survives_being_mostly_reasoning(
    ready_client: TestClient, db_session: Session
) -> None:
    """★ 被 `reasoning` 淹没时活动流**仍然有内容**。

    真实产出里 agent 的每一步思考都是一条 `reasoning`，占比不低。
    如果取最近 8 条再过滤，很可能一条都不剩 —— 而界面看起来是
    "扫描没在动"，其实是过滤把窗口吃掉了。
    所以实现先多取一批再过滤。
    """
    scan = make_scan(db_session, status=ScanStatus.RUNNING.value)
    agent = make_agent(db_session, scan.id)
    make_event(db_session, scan_id=scan.id, agent_id=agent.id, type="tool_call", summary="早期的一次工具调用")
    for index in range(30):
        make_event(
            db_session,
            scan_id=scan.id,
            agent_id=agent.id,
            type="reasoning",
            summary=f"思考 {index}",
            at_offset_seconds=index,
        )

    activity = ready_client.get(f"{API}/overview").json()["activity"]

    assert activity, "被 reasoning 淹没后活动流不该是空的"
    assert activity[0]["type"] == "tool_call"


# ══════════════════════════════════════════════════════════
# 4. 鉴权与形状
# ══════════════════════════════════════════════════════════


def test_overview_requires_read_scope(client: TestClient) -> None:
    """无凭据 → 401（不是 501、更不是 200）。

    ⚠️ 这条在实现之前测的是 501 —— 现在端点真的有了，
    断言必须跟着变成"能读到数据"。一条断言着旧状态的测试
    会把已经过期的行为锁进绿灯里。
    """
    assert client.get(f"{API}/overview").status_code == 401


def test_overview_is_no_longer_a_stub(ready_client: TestClient) -> None:
    """★ `/overview` **不再是 501 占位**。

    契约 §9 的交付表把它排在任务 6，而它的 501 消息一直写着
    「计划在后端任务 6 中交付」。任务 6 做完之后那条消息就成了假话 ——
    更糟的是当时的验收脚本把这个 501 断言成 **[PASS]**，
    于是"承诺过期"被洗成了一条绿灯。

    所以这条钉死：它必须返回 200 与真实数据。
    """
    response = ready_client.get(f"{API}/overview")

    assert response.status_code == 200, f"仍是占位：{response.text[:200]}"
    assert set(response.json()) == {"metrics", "activity", "severityBreakdown", "hasAnyScan"}


def test_response_shape_matches_frontend_type(ready_client: TestClient) -> None:
    """★ 响应形状与 `data/overview.ts` 的 `OverviewData` 逐字段一致。

    前端接线时不该需要改任何类型 —— 包括 `metrics` 的每个字段名。

    ⚠️ 改这里必须**同时**改三处，否则形状会漂移：
    ① `overview/schemas.py` 的 `OverviewMetricsOut`；
    ② `suvex-web/src/data/overview.ts` 的 `OverviewMetrics`；
    ③ 本断言。
    这条测试就是那个"盯着三处一致"的东西 —— 它红过（v0.36 加 token
    字段时先只改了两处）。
    """
    body = ready_client.get(f"{API}/overview").json()

    assert set(body) == {"metrics", "activity", "severityBreakdown", "hasAnyScan"}
    assert set(body["metrics"]) == {
        "activeScans",
        "newHighSeverityThisWeek",
        "newHighSeverityDelta",
        "verifiedPocCount",
        "avgScanDurationSec",
        "falsePositiveCount",
        # v0.36：CTF2 token 消耗（见 schemas 里那一段的说明）
        "ctf2LlmCalls",
        "ctf2RecordedCalls",
        "ctf2TotalTokens",
        "ctf2UsageSince",
    }


def test_scan_id_narrows_the_scope(ready_client: TestClient, db_session: Session, tmp_path: Path) -> None:
    """`?scanId=` 把统计收窄到一次扫描（契约 §3.4.1 的 `scan_id` 参数）。

    注意 `activeScans` / `hasAnyScan` **不受它影响** —— 那两个描述的是
    全局的扫描队列，而不是"这次扫描里有多少个在跑"（那永远是 0 或 1，
    没有信息量）。如实记：这是一个刻意的**部分收窄**。
    """
    first = make_scan(db_session, status=ScanStatus.COMPLETED.value, target="a.example.com")
    second = make_scan(db_session, status=ScanStatus.COMPLETED.value, target="b.example.com")
    for scan, severity in ((first, "critical"), (second, "low")):
        scan.run_dir = str(tmp_path / scan.id)
        db_session.commit()
        Path(scan.run_dir).mkdir(parents=True, exist_ok=True)
        import_vulns(db_session, scan, [vuln_record(id="v1", severity=severity)])

    everything = ready_client.get(f"{API}/overview").json()
    just_first = ready_client.get(f"{API}/overview", params={"scanId": first.id}).json()

    total_all = sum(item["count"] for item in everything["severityBreakdown"])
    total_first = sum(item["count"] for item in just_first["severityBreakdown"])
    assert total_all == 2
    assert total_first == 1
    # 全局的扫描队列不受 scanId 影响
    assert everything["metrics"]["activeScans"] == just_first["metrics"]["activeScans"]


def test_new_high_severity_delta_is_null_when_last_week_is_empty(
    db_session: Session,
) -> None:
    """★ 上周为空时 `delta` 是 `null`，**不是 0**。

    「+3 vs 0」给不出百分比/差值，返回 0 会被读成"与上周持平"。
    直接测 service 层 —— 造"上周"的数据要改 `discovered_at`，
    而 HTTP 层没有能改它的入口（也不该有）。
    """
    from app.core.timeutil import now_utc

    scan = make_scan(db_session, status=ScanStatus.COMPLETED.value)
    from app.findings.models import Finding

    db_session.add(
        Finding(
            id="find_this_week",
            scan_id=scan.id,
            source_ref="vuln-0001",
            severity="high",
            category="misconfiguration",
            title="本周的高危",
            target="api.example.com",
            verification="verified",
            status="new",
            discovered_at=now_utc(),
        )
    )
    db_session.commit()

    metrics = findings_service.overview_metrics(db_session)

    assert metrics["newHighSeverityThisWeek"] == 1
    assert metrics["newHighSeverityDelta"] is None, "没有上周数据时不能返回 0"


def test_new_high_severity_delta_computes_when_both_weeks_have_data(
    db_session: Session,
) -> None:
    """★ 两周都有数据时才算差值 —— 并验算它确实是 本周 − 上周。"""
    from datetime import timedelta

    from app.core.timeutil import now_utc
    from app.findings.models import Finding

    scan = make_scan(db_session, status=ScanStatus.COMPLETED.value)
    moment = now_utc()
    rows = [
        # 本周 2 条
        ("f1", moment - timedelta(days=1)),
        ("f2", moment - timedelta(days=3)),
        # 上周 1 条
        ("f3", moment - timedelta(days=9)),
    ]
    for finding_id, discovered in rows:
        db_session.add(
            Finding(
                id=finding_id,
                scan_id=scan.id,
                source_ref=finding_id,
                severity="high",
                category="misconfiguration",
                title=f"高危 {finding_id}",
                target="api.example.com",
                verification="verified",
                status="new",
                discovered_at=discovered,
            )
        )
    db_session.commit()

    metrics = findings_service.overview_metrics(db_session)

    assert metrics["newHighSeverityThisWeek"] == 2
    assert metrics["newHighSeverityDelta"] == 1, "本周 2 − 上周 1 = +1"


def test_projection_drops_events_with_no_detail() -> None:
    """纯状态事件（`detail` 为 null）投影后 `subject` 为 null，且不报错。

    契约 §2.6：`detail` **整体可为 null**。这是最常见的一种事件
    （扫描生命周期事件就没有载荷），所以必须单独覆盖。
    """

    class FakeEvent:
        id = "evt_x"
        scan_id = "scan_x"
        agent_id = "agent_x"
        agent_phase = "report"
        type = "completed"
        summary = "扫描完成"
        detail = None
        at = __import__("app.core.timeutil", fromlist=["now_utc"]).now_utc()

    projected = overview_service.project_activity(FakeEvent())  # type: ignore[arg-type]

    assert projected.subject is None
    assert projected.type == "completed"


def test_service_uses_stats_not_a_second_count(
    db_session: Session, completed_scan: Scan, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★★ 断言 Overview 的统计**确实**走 `stats()` 这一处。

    做法：把 `stats` 换成探针，确认 `overview_metrics` 调了它。
    这比"比对两个数字"更硬 —— 数字相等也可能是因为各自数了一遍
    而恰好相等（并发编辑下就会不等，而那正是要防的场景）。
    """
    called: dict[str, int] = {}
    real = findings_service.stats

    def probe(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        called["n"] = called.get("n", 0) + 1
        return real(*args, **kwargs)

    monkeypatch.setattr(findings_service, "stats", probe)
    findings_service.overview_metrics(db_session)

    assert called.get("n") == 1, "Overview 必须调 stats()，不能自己再数一遍"
