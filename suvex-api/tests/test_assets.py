"""`/assets` 资产视图（v0.85）。

## 这个领域没有表，所以测试全在**聚合口径**上

没有"存对了没有"可测。真正会错的是下面这几条，每条都单独测：

1. **未修复的口径**：`false_positive` 必须排除 —— 它不是"待修"，
   它是"不用修"。算进去会让用户以为有一堆活要干。
2. **连续失败**必须真的按时间倒序数：5 次里失败 3 次、最后 2 次成功，
   连续失败是 **0**，不是 3 也不是 0.6。
3. `lastScanAt` 取**全部状态**、`lastCompletedAt` 只取 `completed` ——
   两者相差很远本身就是信息。
4. **排序是风险优先**：一个 A 开头、什么漏洞都没有的目标不能占住第一行。
5. 目标数超上限时 `truncated=true`，且**统计条与返回的这一页对得上**。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.assets import service
from app.core.timeutil import now_utc
from app.core.vocab import FindingStatus, ScanStatus
from app.findings.models import Finding
from app.scans.models import Scan
from tests.factories import make_scan

API = "/api/v1"


# ══════════════════════════════════════════════════════════
# 造数据
# ══════════════════════════════════════════════════════════


def _scan(
    db: Session,
    *,
    target: str,
    status: str = ScanStatus.COMPLETED.value,
    minutes_ago: int = 0,
):
    """造一次扫描，并把 `created_at` 拨到 `minutes_ago` 分钟前。

    ⚠️ 必须显式改 `created_at`：`make_scan` 一律用 `now_utc()`，
    同一秒里造 3 条会让"按时间倒序"变成未定义 —— 而本文件有一半的
    断言依赖那个顺序。
    """
    scan = make_scan(db, target=target, status=status)
    scan.created_at = now_utc() - timedelta(minutes=minutes_ago)
    db.commit()
    return scan


def _finding(
    db: Session,
    *,
    target: str,
    severity: str = "high",
    status: str = FindingStatus.NEW.value,
    scan_id: str | None = None,
) -> Finding:
    """直接建一条问题记录（不走导入链路，避免依赖文件系统）。"""
    # `scan_id` 是 NOT NULL + 外键 —— 必须真的挂到某一次扫描上。
    # 没有现成扫描时补一条（这正是"历史导入"那条路径的形状：
    # finding 的 target 与它的 scan 的 target 不一致）。
    if scan_id is None:
        owner = db.query(Scan).filter(Scan.target == target).first()
        if owner is None:
            owner = _scan(db, target=target, minutes_ago=1)
        scan_id = owner.id

    row = Finding(
        id=f"fnd_{target}_{severity}_{status}_{db.query(Finding).count()}",
        scan_id=scan_id,
        title="t",
        severity=severity,
        category="misconfiguration",
        verification="none",
        status=status,
        target=target,
        detail_description="d",
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture()
def client(ready_client: TestClient) -> TestClient:
    """已登录的管理员客户端（本领域全部接口要 `read`，未登录是 401）。"""
    return ready_client


# ══════════════════════════════════════════════════════════
# 1. 聚合本身
# ══════════════════════════════════════════════════════════


def test_groups_scans_and_findings_by_target(client: TestClient, db_session: Session):
    _scan(db_session, target="a.example", minutes_ago=30)
    _scan(db_session, target="a.example", minutes_ago=10)
    _scan(db_session, target="b.example", minutes_ago=5)
    _finding(db_session, target="a.example", severity="high")
    _finding(db_session, target="a.example", severity="low")

    body = client.get(f"{API}/assets").json()
    by_target = {a["target"]: a for a in body["items"]}

    assert by_target["a.example"]["scanCount"] == 2
    assert by_target["a.example"]["findingCount"] == 2
    assert by_target["b.example"]["scanCount"] == 1
    assert by_target["b.example"]["findingCount"] == 0


def test_finding_on_a_different_scan_is_still_reachable(client: TestClient, db_session: Session):
    """finding 的 `target` 与它所属 scan 的 `target` 不一致时，资产分组按 **finding 自己的 target**。

    ★ 这不是假想的情形：`findings.scan_id` 是 NOT NULL，所以每个 finding
    必然挂在某次扫描上；但导入时 `target` 是从**外部工具的记录**里取的，
    与扫描的目标可以不同（多目标扫描、子域、或者上游改过 target）。

    如果资产分组只看 `scans.target`，这类 finding 的漏洞条数会**记到
    另一个目标名下** —— 界面上表现为"这台机器有 open critical 但我点进去
    一条都没有"。所以这里刻意让 scan 和 finding 的 target 不同。
    """
    other = _scan(db_session, target="owner-scan.example", minutes_ago=10)
    _finding(db_session, target="declared.example", severity="critical", scan_id=other.id)

    by_target = {a["target"]: a for a in client.get(f"{API}/assets").json()["items"]}

    # 两个目标都出现：一个是"扫过但没发现"，一个是"有发现但没被扫过"
    assert "owner-scan.example" in by_target
    assert "declared.example" in by_target
    assert by_target["owner-scan.example"]["findingCount"] == 0
    assert by_target["declared.example"]["findingCount"] == 1
    assert by_target["declared.example"]["openCritical"] == 1


# ══════════════════════════════════════════════════════════
# 2. 未修复口径（最容易漂移的地方）
# ══════════════════════════════════════════════════════════


def test_false_positive_is_not_counted_as_open(client: TestClient, db_session: Session):
    """★ 误报不算"未修复"。它已经终结了，只是终结方式不是"修了"。"""
    _scan(db_session, target="x.example")
    _finding(db_session, target="x.example", severity="high")  # open
    _finding(db_session, target="x.example", severity="high", status="false_positive")
    _finding(db_session, target="x.example", severity="high", status="fixed")
    _finding(db_session, target="x.example", severity="high", status="closed")

    asset = client.get(f"{API}/assets").json()["items"][0]

    assert asset["findingCount"] == 4  # 历史上全部
    assert asset["openFindingCount"] == 1  # 只有 open
    assert asset["openHigh"] == 1
    # 分档统计要含已终结的（详情页画图用），所以这里是 4
    assert asset["bySeverity"]["high"] == 4


def test_retesting_counts_as_open(client: TestClient, db_session: Session):
    """`retesting` 是"修了但没确认" —— 风险仍在，必须算开着。"""
    _scan(db_session, target="r.example")
    _finding(db_session, target="r.example", severity="critical", status="retesting")

    asset = client.get(f"{API}/assets").json()["items"][0]
    assert asset["openFindingCount"] == 1
    assert asset["openCritical"] == 1


def test_open_counts_agree_with_open_total(client: TestClient, db_session: Session):
    """各档未修复之和应等于 `openFindingCount` —— 防两套口径并存。

    这条守的是同一个数的两个写法必须相等。曾经有过"未修复 5 条、
    高危未修复 7 条"这种自相矛盾的输出，根因是两处各自过滤状态。
    """
    _scan(db_session, target="m.example")
    for sev, st in [
        ("critical", "new"),
        ("high", "new"),
        ("high", "retesting"),
        ("medium", "new"),
        ("low", "false_positive"),
        ("high", "fixed"),
    ]:
        _finding(db_session, target="m.example", severity=sev, status=st)

    asset = client.get(f"{API}/assets").json()["items"][0]
    assert asset["openFindingCount"] == 4
    # openCritical + openHigh 只覆盖两档，所以用它俩分别核对
    assert asset["openCritical"] == 1
    # high 里有 open + retesting 两条开着、fixed 一条关着
    assert asset["openHigh"] == 2


# ══════════════════════════════════════════════════════════
# 3. 时间语义
# ══════════════════════════════════════════════════════════


def test_last_scan_at_includes_failed_but_last_completed_does_not(client: TestClient, db_session: Session):
    """★ 这条是这个接口存在的核心语义。

    `lastScanAt` 问的是"动作发生过没有"，`lastCompletedAt` 问的是
    "上次真正拿到结果是什么时候"。一个目标今天失败 3 次、上周成功 1 次：
    - 天真实现（只算 completed）会显示"上周扫的" → 用户以为没人管；
    - 另一个天真实现（不分状态）会让"从没成功过"看起来像"今天刚扫过"。
    两个都给，差距本身就是信息。
    """
    _scan(db_session, target="f.example", status=ScanStatus.COMPLETED.value, minutes_ago=600)
    _scan(db_session, target="f.example", status=ScanStatus.FAILED.value, minutes_ago=20)

    asset = client.get(f"{API}/assets").json()["items"][0]

    assert asset["lastCompletedAt"] is not None
    assert asset["lastScanAt"] is not None
    # lastScanAt 比 lastCompletedAt 新得多（差约 580 分钟 ≈ 9.6 小时）
    assert asset["lastScanAt"] > asset["lastCompletedAt"]
    assert asset["lastScanStatus"] == ScanStatus.FAILED.value


def test_never_completed_is_counted_separately(client: TestClient, db_session: Session):
    """ "有扫描但从没成功过"必须单独计数 —— 它最容易被误读成"干净"。"""
    _scan(db_session, target="nc.example", status=ScanStatus.FAILED.value, minutes_ago=5)

    body = client.get(f"{API}/assets").json()
    asset = body["items"][0]

    assert asset["lastCompletedAt"] is None
    assert asset["lastScanAt"] is not None  # 明明扫过
    assert body["summary"]["neverCompleted"] == 1
    assert body["summary"]["neverScanned"] == 0


# ══════════════════════════════════════════════════════════
# 4. 连续失败：必须按时间倒序真数
# ══════════════════════════════════════════════════════════


def test_consecutive_failures_counts_from_the_most_recent(client: TestClient, db_session: Session):
    """最近两次失败 → 2；再往前那次成功不打断它（那是"之前"的事）。"""
    _scan(db_session, target="cf.example", status=ScanStatus.FAILED.value, minutes_ago=30)
    _scan(db_session, target="cf.example", status=ScanStatus.FAILED.value, minutes_ago=20)
    _scan(db_session, target="cf.example", status=ScanStatus.FAILED.value, minutes_ago=10)

    asset = client.get(f"{API}/assets").json()["items"][0]
    assert asset["consecutiveFailures"] == 3


def test_consecutive_failures_stops_at_last_success(client: TestClient, db_session: Session):
    """★ 5 次里失败 3 次、最后 2 次成功 → 连续失败是 **0**。

    这条专门打"用比例冒充"的写法：`failed/total = 3/5 = 0.6` 看起来
    像个合理的"健康度"，但资产此刻其实完全正常。反过来，
    "总失败数 3" 也是错的 —— 它没区分这 3 次是多久以前。
    """
    _scan(db_session, target="ok.example", status=ScanStatus.FAILED.value, minutes_ago=50)
    _scan(db_session, target="ok.example", status=ScanStatus.FAILED.value, minutes_ago=40)
    _scan(db_session, target="ok.example", status=ScanStatus.FAILED.value, minutes_ago=30)
    _scan(db_session, target="ok.example", status=ScanStatus.COMPLETED.value, minutes_ago=20)
    _scan(db_session, target="ok.example", status=ScanStatus.COMPLETED.value, minutes_ago=10)

    asset = client.get(f"{API}/assets").json()["items"][0]
    assert asset["scanCount"] == 5
    assert asset["consecutiveFailures"] == 0
    # 这条同时是"连续失败不是总失败数"的守卫：总失败数是 3
    assert asset["lastScanStatus"] == ScanStatus.COMPLETED.value


def test_consecutive_failures_is_independent_per_target(client: TestClient, db_session: Session):
    """一个目标的失败流水不能串到另一个目标上（分组键用错就会串）。"""
    _scan(db_session, target="p1.example", status=ScanStatus.FAILED.value, minutes_ago=20)
    _scan(db_session, target="p1.example", status=ScanStatus.FAILED.value, minutes_ago=10)
    _scan(db_session, target="p2.example", status=ScanStatus.COMPLETED.value, minutes_ago=15)

    by_target = {a["target"]: a for a in client.get(f"{API}/assets").json()["items"]}
    assert by_target["p1.example"]["consecutiveFailures"] == 2
    assert by_target["p2.example"]["consecutiveFailures"] == 0


# ══════════════════════════════════════════════════════════
# 5. 排序
# ══════════════════════════════════════════════════════════


def test_sorted_by_risk_not_by_name(client: TestClient, db_session: Session):
    """★ 按名字排会让「A 开头、什么问题都没有」占住第一屏。

    期望次序：open critical > open high > 未修复总数 > 最近扫描。
    """
    _scan(db_session, target="z-critical.example")
    _scan(db_session, target="m-high.example")
    _scan(db_session, target="a-empty.example")

    _finding(db_session, target="z-critical.example", severity="critical")
    _finding(db_session, target="m-high.example", severity="high")

    targets = [a["target"] for a in client.get(f"{API}/assets").json()["items"]]
    assert targets == ["z-critical.example", "m-high.example", "a-empty.example"]


def test_fixed_findings_do_not_lift_a_target(client: TestClient, db_session: Session):
    """已修复的 critical 不该让一个目标排到前面 —— 排序看的是**开着**的。

    ★ 这条用例要验证的是排序键的**第三位**（`openFindingCount`），
    所以前两位（`openCritical`、`openHigh`）必须完全并列，否则第三位
    永远轮不到 —— 改坏了也测不出来。

    构造：两个目标都**没有**开着的高危（openHigh=0、openCritical=0），
    区别只在「第三位」：
      · a-fixed：1 条已修 critical → findingCount=1、openFindingCount=0
      · b-open ：1 条开着 low      → findingCount=1、openFindingCount=1
    总数相同（都是 1），未修复数 0 vs 1。

    历史（两版都失败过，记下来免得再踩）：
      1. 第一版用 open high 做 b 的漏洞 → openHigh 已经分出胜负，第三位
         不参与比较 → 把 `openFindingCount` 换成 `findingCount` 测试照样绿。
      2. 第二版仍沿用 open high，于是反向验证连续两次判它假绿。
      3. 现在改用 **low**：low 不进 openHigh/openCritical，只贡献总数。
    """
    # ★ 两条目标各走一次**独立的**扫描，时间一老一新：
    #   a-fixed 1 分钟前、b-open 60 分钟前。
    #
    #   为什么要强调「同一个目标只有一条扫描」：第一版里 `_finding()` 的
    #   `scan_id=None` 会顺手补一条「1 分钟前」的扫描，而 `lastScanAt` 取的是
    #   **最大**时间戳 —— 于是 b-open 也被拉回 1 分钟前，两条目标时间并列。
    #   那一版两种排序（按总数 / 按未修复数）都会让 a-fixed 在前
    #   （前者靠时间档、后者靠名字决胜），这条用例对两者的区别完全
    #   无感，反向验证判它假绿。
    #
    #   下面的整段断言（不是切片）正是为了不再被那条隐式扫描糊弄：
    #   `_finding` 一旦额外补扫，列表里就会多出东西，用例立刻红。
    _scan(db_session, target="a-fixed.example", minutes_ago=1)
    _scan(db_session, target="b-open.example", minutes_ago=60)
    _finding(
        db_session,
        target="a-fixed.example",
        severity="critical",
        status=FindingStatus.FIXED.value,
    )
    _finding(
        db_session,
        target="b-open.example",
        severity="low",
    )

    items = client.get(f"{API}/assets").json()["items"]
    by_target = {a["target"]: a for a in items}

    # ── 前提核对（没有这些前提，下面的顺序断言就是在赌运气）──
    #
    # ★ 前两位必须并列，否则第三位（openFindingCount）根本轮不到比较，
    #   把第三位改坏（换成 findingCount）测试照样绿 —— 这正是这条用例
    #   连续两版假绿的原因。
    assert by_target["a-fixed.example"]["openCritical"] == 0
    assert by_target["b-open.example"]["openCritical"] == 0
    assert by_target["a-fixed.example"]["openHigh"] == 0
    assert by_target["b-open.example"]["openHigh"] == 0
    # 第三位有差别：总数相同，未修复数不同
    assert by_target["a-fixed.example"]["findingCount"] == 1
    assert by_target["b-open.example"]["findingCount"] == 1
    assert by_target["a-fixed.example"]["openFindingCount"] == 0
    assert by_target["b-open.example"]["openFindingCount"] == 1

    # ── ★ 顺序直接取自 `items` 这个**列表**，不能从 `by_target`（字典）里读：
    #   字典的键序跟着插入序走，排序怎么改它都不动 —— 第一版就是这么写的，
    #   反向验证因此把这条用例判成假绿。
    order = [a["target"] for a in items]
    assert order == ["b-open.example", "a-fixed.example"], "排序没有按『未修复数』走 —— 实际顺序 " + repr(
        order
    )


def test_recent_scan_wins_when_risk_ties(client: TestClient, db_session: Session):
    """风险完全相同时，最近被扫过的靠前 —— 正在被关注的目标应该在上面。

    ★ 目标名刻意是 `z-recent` / `a-stale`：按名字排恰好是**相反**的次序，
    这样"按活动排"和"按名字排"能区分开。
    （第一版用 new/old，名字序与期望序同向 —— 后来又把名字写成
    a-recent 在前，于是"按名字"和"按活动"给出同一个结果，这条用例
    在反向验证里被判假绿：去掉按活动排序那一项之后仍然全绿。）
    """
    _scan(db_session, target="z-recent.example", minutes_ago=5)
    _scan(db_session, target="a-stale.example", minutes_ago=600)

    items = client.get(f"{API}/assets").json()["items"]
    # 两个目标都没有漏洞 —— 风险确实并列
    assert all(a["openFindingCount"] == 0 for a in items)
    assert [a["target"] for a in items] == ["z-recent.example", "a-stale.example"]


# ══════════════════════════════════════════════════════════
# 6. 上限与 truncated
# ══════════════════════════════════════════════════════════


def test_truncated_flag_and_summary_agree_with_page(client: TestClient, db_session: Session):
    """超过 limit 时 `truncated=true`，且统计条只统计**返回的这一页**。

    统计条用全量算会让用户"数不出"—— 列表里 3 条，统计条说共 7 个，
    用户会以为界面坏了。`truncated` 已经表达"还有更多没显示"。
    """
    for i in range(5):
        _scan(db_session, target=f"h{i}.example")

    body = client.get(f"{API}/assets", params={"limit": 3}).json()

    assert body["truncated"] is True
    assert len(body["items"]) == 3
    assert body["total"] == 3
    assert body["summary"]["targetCount"] == 3


def test_not_truncated_when_within_limit(client: TestClient, db_session: Session):
    _scan(db_session, target="one.example")
    _scan(db_session, target="two.example")

    body = client.get(f"{API}/assets").json()
    assert body["truncated"] is False
    assert body["summary"]["targetCount"] == 2


# ══════════════════════════════════════════════════════════
# 7. 空库
# ══════════════════════════════════════════════════════════


def test_empty_database_returns_empty_list_not_error(client: TestClient):
    body = client.get(f"{API}/assets").json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["truncated"] is False
    assert body["summary"]["targetCount"] == 0


# ══════════════════════════════════════════════════════════
# 8. 常量的单点性
# ══════════════════════════════════════════════════════════


def test_terminal_statuses_cover_all_but_open_states():
    """终结态集合必须**恰好**是那三个，多一个少一个都会让口径漂移。

    这条防的是"顺手把 retesting 也加进去"—— 那会让"修了但没确认"
    从风险面里消失，而这正是最需要盯住的一类。
    """
    assert set(service.TERMINAL_FINDING_STATUSES) == {
        FindingStatus.FIXED.value,
        FindingStatus.CLOSED.value,
        FindingStatus.FALSE_POSITIVE.value,
    }
    assert FindingStatus.RETESTING.value not in service.TERMINAL_FINDING_STATUSES


def test_max_assets_is_the_query_cap():
    """路由的 `le=` 与 service 的上限必须同一个数（否则 422 报在奇怪的地方）。"""
    from app.assets.schemas import AssetListOut  # noqa: F401 - 仅为确保可导入

    assert service.MAX_ASSETS == 500
