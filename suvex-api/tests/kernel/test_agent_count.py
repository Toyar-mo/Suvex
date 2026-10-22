"""解题进行中，扫描列表里的「智能体数」必须是真的（v0.49）。

## 这组用例守的是用户实测的那个界面错报

    解题中 · TCP 服务 · **0 个智能体**

而实际上 `ctf2-solver` 已经在活动流里干活（`agents` 表里
`role=exploiter`、`status=running`、`event_count=11`）。用户看到这个
数字会以为「根本没启动」，于是去查日志、去怀疑模型没连上。

## 根因

`ensure_solve_scan` 建 scan 行时 `agent_count` 走模型默认值 **0**，而
`refresh_scan_rollup`（唯一刷这个数的地方）**只在收尾时**被调用。
于是整个解题过程中它一直是 0。

更迷惑的是**重跑复用旧 scan 行**（`ensure_solve_scan` 的复用分支只重置
`status` / `finished_at` / `duration_sec`）—— 那个数会留着上一轮收尾时
刷出的 1。同一个界面上的数字于是忽对忽错：新题显示 0，重跑的题显示 1。

## 三条承诺

| # | 承诺 | 由哪个用例守 |
| --- | --- | --- |
| 1 | 建出 solver agent 后，`agent_count` 立刻是 1 | `test_creating_solver_agent_updates_the_count` |
| 2 | 不 commit（事务边界归调用方） | `test_it_does_not_commit` |
| 3 | 复用分支不再跑 `COUNT(*)`（热路径） | `test_reuse_path_does_not_recount` |

为什么第 2 条要单独守：`ensure_solver_agent` 在 `_emit` 的事务中间被
调用，而 `refresh_scan_rollup` **会** commit（它服务的是另一个场景）。
在那里提交会把调用方未提交的半截工作提前落地 —— 见 `app/core/db.py`
里那条「内层函数替外层定事务边界」的告诫。这一条用"外层还有未提交改动
时它还在"来验，不是看代码里有没有 `commit()` 这个词。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.kernel import events as ctf2_events
from app.scans.models import Scan
from tests.factories import make_scan


def _scan(db: Session, *, status: str = "running") -> Scan:
    return make_scan(db, status=status, target="ctf2:agent-count-test")


def test_creating_solver_agent_updates_the_count(db_session: Session) -> None:
    """★★ 建出 solver agent 的那一刻，`agent_count` 就该是 1。

    这就是用户报的那条：以前它要等到收尾才被刷，整个解题过程显示 0。
    """
    scan = _scan(db_session)
    # `make_scan` 默认给 1（工厂是为了别的用例设的）—— 这里要 0 才有意义，
    # 因为"新 scan 的计数是 0"正是这个 bug 的起点。
    scan.agent_count = 0
    db_session.commit()
    assert scan.agent_count == 0

    ctf2_events.ensure_solver_agent(db_session, scan.id)
    db_session.commit()
    db_session.refresh(scan)

    assert scan.agent_count == 1, (
        "建出 solver agent 之后计数还是 0 —— 界面上会显示「0 个智能体」"
    )


def test_it_does_not_commit(db_session: Session) -> None:
    """★ 它只写列、不提交 —— 事务边界归调用方。

    怎么验「没提交」：在本函数之前先改一个**别的**对象且不 commit，
    调用之后把会话回滚。如果它偷偷 commit 了，那个改动就落盘了。
    只看源码里有没有 `commit()` 这个词是不够的（可能藏在被调的
    函数里，`refresh_scan_rollup` 就是这样）。
    """
    from app.core.ids import new_id

    scan = _scan(db_session)
    scan.agent_count = 0

    # 一个明确的、未提交的改动作为探针
    probe = Scan(
        id=new_id("scan"),
        name="commit-probe",
        target="ctf2:probe",
        target_kind="service",
        status="running",
        skills=[],
        goal="captureFlag",
        provider_id=scan.provider_id,
        started_at=scan.started_at,
        authorized_by="ctf2",
        authorization_statement="探针",
        authorization_confirmed_at=scan.started_at,
        authorization_received_at=scan.started_at,
        authorization_ip="",
        authorization_user_agent="pytest",
    )
    db_session.add(probe)
    db_session.flush()
    probe_id = probe.id

    ctf2_events.ensure_solver_agent(db_session, scan.id)

    db_session.rollback()

    assert db_session.get(Scan, probe_id) is None, (
        "ensure_solver_agent 提前提交了调用方的事务 —— "
        "它会替调用方把半截工作落盘"
    )


def test_reuse_path_does_not_recount(db_session: Session, monkeypatch) -> None:
    """★ 复用分支不跑 `COUNT(*)` —— 它是 `_emit` 的热路径。

    一次解题能发几百条事件，每条都要过这里。为了一个"创建时就该对"的
    数，每条事件都数一遍表是拿得到的东西换不划算的代价。

    怎么验：把 `count_scan_agents` 换成会记账的替身，然后第二次调用
    （agent 已存在 → 走复用分支）时检查它一次都没被调。
    """
    from app.scans import service as scans_service

    scan = _scan(db_session)
    ctf2_events.ensure_solver_agent(db_session, scan.id)
    db_session.commit()

    calls: list[str] = []
    original = scans_service.count_scan_agents

    def _counting(db, scan_id):  # noqa: ANN001, ANN202
        calls.append(scan_id)
        return original(db, scan_id)

    # ★ 换模块属性就够：`ensure_solver_agent` 是在**函数体内**
    #   `from app.scans.service import count_scan_agents`，每次调用都
    #   重新取一次属性（这也是它能在 ctf2 里延迟导入的原因）。
    monkeypatch.setattr(scans_service, "count_scan_agents", _counting)

    ctf2_events.ensure_solver_agent(db_session, scan.id)

    assert calls == [], (
        "复用分支也去数表了 —— 那是每条事件都要走的路，代价落在热路径上"
    )
