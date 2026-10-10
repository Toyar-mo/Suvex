"""按漏洞类型分组（任务 10 P4）。

最要紧的两条：

1. **`/findings/groups` 不能被 `/{findingId}` 抢先匹配** —— 这个文件里
   已经踩过两次同一个坑（`/stats`、`/assign`）。
2. **分组按标题**，与扫描对比模块同判据。判据不一致的话，
   会出现"对比说这条消失了、分组视图里还在"这种自相矛盾。
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.findings.models import Finding

API = "/api/v1"


def _add(scan, db_session: Session, **kwargs) -> Finding:
    """直接插一条漏洞 —— 分组测的是聚合逻辑，不必每次都走导入器。"""
    from app.core.timeutil import now_utc

    finding = Finding(
        id=kwargs["id"],
        scan_id=scan.id,
        source_ref=kwargs.get("source_ref", kwargs["id"]),
        severity=kwargs.get("severity", "high"),
        category=kwargs.get("category", "misconfiguration"),
        title=kwargs["title"],
        target=kwargs.get("target", "api.example.com"),
        verification="verified",
        status=kwargs.get("status", "new"),
        discovered_at=now_utc(),
    )
    db_session.add(finding)
    db_session.commit()
    return finding


def test_groups_route_is_not_shadowed_by_the_id_route(ready_client: TestClient) -> None:
    """★ 分组接口不能被 `/{findingId}` 抢先匹配。

    路由顺序错了的症状是「404 找不到叫 groups 的漏洞」——
    一个看起来像"数据不存在"、实际是路由声明顺序问题的错。
    """
    response = ready_client.get(f"{API}/findings/groups")
    assert response.status_code == 200, response.text
    assert isinstance(response.json(), list)


def test_same_title_across_targets_collapses_into_one_group(
    ready_client: TestClient, scan, db_session: Session
) -> None:
    """★ 同一个漏洞出现在两个目标上 → **一组**，但仍是两条独立记录。

    这正是需求里描述的场景：不同目标各自的实例（处置状态也各自独立），
    但人能一眼看出"这是同一类问题"。
    """
    _add(scan, db_session, id="f_a", title="调试接口未授权可读环境变量", target="a.example.com")
    _add(scan, db_session, id="f_b", title="调试接口未授权可读环境变量", target="b.example.com")
    _add(scan, db_session, id="f_c", title="会话 Cookie 缺少 SameSite 属性", target="a.example.com")

    groups = ready_client.get(f"{API}/findings/groups").json()
    assert len(groups) == 2

    by_title = {g["title"]: g for g in groups}
    collapsed = by_title["调试接口未授权可读环境变量"]
    assert collapsed["instanceCount"] == 2
    assert collapsed["targetCount"] == 2
    assert set(collapsed["targets"]) == {"a.example.com", "b.example.com"}
    # 带 id 是为了展开时能直接筛列表 / 对整组做批量操作
    assert set(collapsed["findingIds"]) == {"f_a", "f_b"}

    # 组内状态各自独立，所以 byStatus 是按条计的
    assert collapsed["byStatus"] == {"new": 2}


def test_group_severity_is_the_highest_in_the_group(
    ready_client: TestClient, scan, db_session: Session
) -> None:
    """组内最高严重度决定这一组长什么样。

    取平均或取第一条会把"这组里有个 critical"藏起来 ——
    而那是决定先处理哪一组的信息。
    """
    _add(scan, db_session, id="f_low", title="同一类问题", severity="low", target="a")
    _add(scan, db_session, id="f_crit", title="同一类问题", severity="critical", target="b")

    groups = ready_client.get(f"{API}/findings/groups").json()
    assert len(groups) == 1
    assert groups[0]["severity"] == "critical"


def test_groups_sorted_by_severity_then_impact(
    ready_client: TestClient, scan, db_session: Session
) -> None:
    """排序：严重度优先，同档按**影响面**（影响目标多的先看）。"""
    _add(scan, db_session, id="g1", title="低危但面广", severity="low", target="a")
    _add(scan, db_session, id="g2", title="低危但面广", severity="low", target="b")
    _add(scan, db_session, id="g3", title="低危但面广", severity="low", target="c")
    _add(scan, db_session, id="h1", title="高危但单点", severity="high", target="a")

    groups = ready_client.get(f"{API}/findings/groups").json()
    assert [g["title"] for g in groups] == ["高危但单点", "低危但面广"]


def test_groups_honour_the_same_filters_as_the_list(
    ready_client: TestClient, scan, db_session: Session
) -> None:
    """★ 筛选参数与 `/findings` 完全一致。

    分组视图不是另一套数据，它是同一批数据换个切法。参数不一致的话，
    用户切到分组视图会看到与列表不同的范围，而他会以为筛选失效了。
    """
    _add(scan, db_session, id="s1", title="只在高危里", severity="high")
    _add(scan, db_session, id="s2", title="只在低危里", severity="low")

    all_groups = ready_client.get(f"{API}/findings/groups").json()
    assert len(all_groups) == 2

    high_only = ready_client.get(f"{API}/findings/groups?severity=high").json()
    assert [g["title"] for g in high_only] == ["只在高危里"]

    # 搜索词同样生效（列表页那个搜索框在分组视图里也要有用）
    searched = ready_client.get(f"{API}/findings/groups?q=低危").json()
    assert [g["title"] for g in searched] == ["只在低危里"]


def test_group_by_status_reflects_per_instance_states(
    ready_client: TestClient, scan, db_session: Session
) -> None:
    """同一组里各条状态不同 —— 这正是"处置状态各自独立"的体现。

    组内不会因为折叠而只显示一个状态：`byStatus` 让界面能说
    "3 个实例里 1 个已确认、2 个待确认"。
    """
    _add(scan, db_session, id="m1", title="多状态一类", status="new", target="a")
    _add(scan, db_session, id="m2", title="多状态一类", status="confirmed", target="b")
    _add(scan, db_session, id="m3", title="多状态一类", status="confirmed", target="c")

    groups = ready_client.get(f"{API}/findings/groups").json()
    assert len(groups) == 1
    assert groups[0]["byStatus"] == {"new": 1, "confirmed": 2}
