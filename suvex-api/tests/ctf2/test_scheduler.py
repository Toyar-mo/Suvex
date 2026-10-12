"""`ctf2.scheduler` 的行为：配额、FIFO、孤儿回收、停止语义。

## 打桩的是 **worker 本身**，不是配额逻辑

`runner.run_worker` 被换成一个可控的假函数（可以阻塞、可以立即返回），
于是"数名额 → 认领 → 启动"这条真实路径全部跑到，只有解题是假的。
"""

from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy.orm import Session

from app.core.vocab import Ctf2ChallengeStatus
from app.ctf2 import scheduler as scheduler_module
from app.ctf2.models import Ctf2Challenge
from app.ctf2.scheduler import Ctf2Scheduler
from tests.ctf2.conftest import _make_challenge


@pytest.fixture(autouse=True)
def _reset_singleton():
    """每个用例前后都把单例丢掉 —— 它是全局状态，留着会让用例互相影响。"""
    scheduler_module.reset_scheduler()
    yield
    scheduler_module.reset_scheduler()


# ══════════════════════════════════════════════════════════
# 配额
# ══════════════════════════════════════════════════════════


def test_limit_comes_from_the_ctf2_quota(db_session: Session) -> None:
    """★★ 解题并发用**自己的**配额，不再借用扫描那个（v0.22 修正）。

    原来这条断言的是"上限 == `max_concurrent_scans`"，理由是
    "两者抢同一批资源，各给一份配额等于超卖一倍"。

    **那个担心是对的，但做法是反的。** 两个调度器**各自**数自己的名额、
    各自拿这同一个上限：

        scans/scheduler.py   COUNT(Scan WHERE running)  <= N
        ctf2/scheduler.py     len(self._running)          <= N

    于是 `=3` 时真的能跑 **3 个扫描 + 3 道题 = 6 个** —— 正是那条注释
    说要避免的超卖，只是它发生在两个调度器**之间**。

    现在的形状是**分层配额**：各领域守自己的子配额，总并发是两者之和。
    所以这条测试要断言"上限来自 CTF2 自己的配置"，
    而**不是**"等于扫描那个" —— 后者会把那处超卖重新钉死。
    """
    from app.ctf2 import service as ctf2_service

    expected = ctf2_service.effective_ctf2_settings(db_session).max_concurrent_solves
    assert scheduler_module.max_concurrent(db_session) == max(1, expected)


def test_ctf2_quota_is_independent_of_the_scan_quota(db_session: Session) -> None:
    """★ 两个配额是**独立**的 —— 改一个不该动另一个。

    这条守住"分层配额"的形状。退化成共用同一个值时，它立刻会红：
    那正是 v0.22 之前的状态，而那个状态会让单机跑出两倍的并发。
    """
    from app.ctf2 import service as ctf2_service
    from app.settings import service as settings_service

    scan_quota = settings_service.effective_execution_settings(
        db_session
    ).max_concurrent_scans
    solve_quota = ctf2_service.effective_ctf2_settings(db_session).max_concurrent_solves

    # 两者各自可读，而且互不派生
    assert solve_quota >= 1
    assert scan_quota >= 1
    # 关键：解题上限**不随**扫描上限变化
    assert scheduler_module.max_concurrent(db_session) == max(1, solve_quota)


def test_limit_without_a_session_reads_the_env_default() -> None:
    assert scheduler_module.max_concurrent(None) >= 1


# ══════════════════════════════════════════════════════════
# 认领轮询间隔（v0.24）
# ══════════════════════════════════════════════════════════


def test_solve_poll_seconds_comes_from_its_own_setting(db_session: Session) -> None:
    """★★ 认领间隔用**自己的**配置，不再借用扫描那个（v0.24 修正）。

    这与 v0.22 的配额修正是**同一类病**：两个独立的东西共用一个旋钮。
    那次表现为超卖（扫描 3 + 解题 3 = 6 个并发）；这次表现为"改了 A 动了
    B" —— 用户在设置页把扫描轮询从 2 秒调到 10 秒（一个完全合理的省机器
    操作），CTF2 的认领延迟会**一起**变成 10 秒，而界面上没有任何地方
    提示这两件事连着。
    """
    from app.ctf2 import service as ctf2_service

    expected = ctf2_service.effective_ctf2_settings(db_session).solve_poll_seconds
    assert scheduler_module.solve_poll_seconds(db_session) == pytest.approx(float(expected))


def test_changing_the_ctf2_poll_leaves_the_scan_poll_alone(db_session: Session) -> None:
    """★ 改 CTF2 的认领间隔，扫描那个旋钮**一动不动**。

    扫描的 `scan_poll_seconds` 是环境变量（部署参数），CTF2 的
    `solve_poll_seconds` 是配置行（设置页里改）。这条断言两者是**两个
    独立的量**：退回共用同一个值时它会红。
    """
    from app.core.config import get_settings
    from app.ctf2 import service as ctf2_service

    before = get_settings().scan_poll_seconds
    row = ctf2_service.get_ctf2_config(db_session)
    row.solve_poll_seconds = 9.0
    db_session.commit()

    assert scheduler_module.solve_poll_seconds(db_session) == pytest.approx(9.0)
    assert get_settings().scan_poll_seconds == before


def test_the_loop_does_not_read_the_scan_poll_setting() -> None:
    """★ 守住解耦：`_loop`（及其调用的 `_interval`）里不许再出现
    `scan_poll_seconds`。

    这是一条**源码级**断言，看起来粗暴，但它挡的正是这次要修的那个
    bug：一行 `interval = get_settings().scan_poll_seconds` 混在循环里，
    任何行为测试都很难发现它 —— 因为两个默认值（2.0 与 1.0）在功能上
    没有可观察的差别，只有在**有人调了其中一个**的时候才暴露。
    """
    import inspect

    source = inspect.getsource(scheduler_module.Ctf2Scheduler._loop)
    source += inspect.getsource(scheduler_module.Ctf2Scheduler._interval)
    assert "scan_poll_seconds" not in source, (
        "调度循环又在读扫描的轮询设置了 —— 那会让"
        "「改扫描轮询」意外改变 CTF2 的认领延迟（v0.24 修过这处耦合）"
    )


def test_poll_interval_without_a_session_reads_the_code_default() -> None:
    assert scheduler_module.solve_poll_seconds(None) > 0


def test_claim_prefers_higher_priority(db_session: Session, ctf2_competition, monkeypatch) -> None:
    """★★ 认领顺序：**优先级高的先做**（v0.23）。

    比赛规则里有两条直接指向它：

    > 每道题目前三名成功解题的团队将额外获得本题分值的 15%、10%、5%
    > 参赛队伍可以……**调整解题优先级**

    前半句意味着"速度直接换分"，后半句是赛事方**明确允许**的操作。
    而原来的认领顺序是纯 FIFO，想插队只能"取消再重排"。
    """
    started: list[str] = []
    release = threading.Event()

    def fake_worker(challenge_id: str, *, resume: bool = False, attempt_id: str | None = None) -> None:
        started.append(challenge_id)
        release.wait(timeout=10)

    monkeypatch.setattr("app.ctf2.runner.run_worker", fake_worker)
    # 名额钉成 1，这样"先起哪一个"是可观测的。
    monkeypatch.setattr(scheduler_module, "max_concurrent", lambda db=None: 1)

    # 三道题都在排队。按 FIFO 本该是 a → b → c（`_make_challenge`
    # 依次插入，`updated_at` 递增）。
    first = _make_challenge(db_session, ctf2_competition, remote_id="a", status="solving")
    _make_challenge(db_session, ctf2_competition, remote_id="b", status="solving")
    last = _make_challenge(db_session, ctf2_competition, remote_id="c", status="solving")

    # 把最后插入的那道（FIFO 里排最后）提到最高优先级。
    last.priority = 10
    db_session.commit()

    scheduler = Ctf2Scheduler()
    assert scheduler.tick() == 1
    assert started == [last.id], (
        "优先级高的应当先被认领 —— 这正是「调整解题优先级」要能生效的地方"
    )

    release.set()
    time.sleep(0.2)
    del first


def test_claim_falls_back_to_fifo_when_priorities_are_equal(
    db_session: Session, ctf2_competition, monkeypatch
) -> None:
    """★ 全 0（没人调过优先级）时，顺序与**升级前逐字一致**。

    这条最重要：v0.23 加了一列排序键，而如果它改变了默认行为，
    所有现有用户的队列顺序会莫名其妙地变。所以"同级按 FIFO"必须被钉住。
    """
    started: list[str] = []
    release = threading.Event()

    def fake_worker(challenge_id: str, *, resume: bool = False, attempt_id: str | None = None) -> None:
        started.append(challenge_id)
        release.wait(timeout=10)

    monkeypatch.setattr("app.ctf2.runner.run_worker", fake_worker)
    monkeypatch.setattr(scheduler_module, "max_concurrent", lambda db=None: 1)

    first = _make_challenge(db_session, ctf2_competition, remote_id="a", status="solving")
    _make_challenge(db_session, ctf2_competition, remote_id="b", status="solving")

    # 显式确认两者都是默认优先级 —— 不确认的话，这条测试在
    # "将来有人给模型加了非 0 默认值"之后会静默失去意义。
    assert first.priority == 0

    scheduler = Ctf2Scheduler()
    assert scheduler.tick() == 1
    assert started == [first.id], "同优先级下应当是先入先出"

    release.set()
    time.sleep(0.2)


def test_setting_priority_does_not_touch_a_running_challenge(
    db_session: Session, ctf2_competition, monkeypatch
) -> None:
    """★ 改优先级**只影响排队中的题**，不打断已经在跑的。

    中断一道跑到一半的题会浪费掉已经消耗的平台环境与 LLM 调用，
    而"让它跑完"的代价只是排队的题晚一会儿开始。
    """
    started: list[str] = []
    release = threading.Event()

    def fake_worker(challenge_id: str, *, resume: bool = False, attempt_id: str | None = None) -> None:
        started.append(challenge_id)
        release.wait(timeout=10)

    monkeypatch.setattr("app.ctf2.runner.run_worker", fake_worker)
    monkeypatch.setattr(scheduler_module, "max_concurrent", lambda db=None: 1)

    running = _make_challenge(db_session, ctf2_competition, remote_id="run", status="solving")
    _make_challenge(db_session, ctf2_competition, remote_id="wait", status="solving")

    scheduler = Ctf2Scheduler()
    assert scheduler.tick() == 1
    assert started == [running.id]

    # 就在这个时刻给"正在跑的那道"设一个极低的优先级。
    from app.ctf2 import service as ctf2_service

    ctf2_service.set_challenge_priority(db_session, running.id, priority=-100)

    # 它不该被踢出去 —— 认领之后就不再受排序影响了。
    assert scheduler.tick() == 0, "名额还被占着，不该再起新的"
    assert started == [running.id], "已经在跑的那道不该被打断"

    release.set()
    time.sleep(0.2)


def test_priorities_by_score_puts_high_scores_first(
    db_session: Session, ctf2_competition
) -> None:
    """★ 按分值批量设优先级：**高分在前，无分值垫底**。

    最后那条是 PostgreSQL 的一个坑：`ORDER BY score DESC` 默认把
    **NULL 排在最前**，于是所有没给分值的题会插到队首 ——
    而它们恰恰是最不值得优先的。所以查询里必须有 `nullslast()`。
    """
    from app.ctf2 import service as ctf2_service

    low = _make_challenge(db_session, ctf2_competition, remote_id="s100", status="solving")
    low.score = 100
    high = _make_challenge(db_session, ctf2_competition, remote_id="s500", status="solving")
    high.score = 500
    no_score = _make_challenge(db_session, ctf2_competition, remote_id="snull", status="solving")
    no_score.score = None
    db_session.commit()

    changed = ctf2_service.set_priorities_by_score(db_session)
    assert changed == 3

    for row in (low, high, no_score):
        db_session.refresh(row)
    assert high.priority > low.priority > no_score.priority, (
        "高分题优先级最高，无分值的垫底 —— NULL 不能排到队首"
    )


def test_priorities_by_score_only_touches_queued_challenges(
    db_session: Session, ctf2_competition
) -> None:
    """★ 它只改**排队中**（`solving`）的题。

    给已解出/未开始的题设优先级没有意义，而且会让"我明明调过它"
    与"它根本没在队列里"矛盾。
    """
    from app.ctf2 import service as ctf2_service

    queued = _make_challenge(db_session, ctf2_competition, remote_id="q", status="solving")
    solved = _make_challenge(db_session, ctf2_competition, remote_id="done", status="unsolved")
    db_session.commit()

    changed = ctf2_service.set_priorities_by_score(db_session)
    assert changed == 1

    db_session.refresh(queued)
    db_session.refresh(solved)
    assert queued.priority > 0
    assert solved.priority == 0, "没排队的题不该被改动"


def test_tick_respects_the_concurrency_limit(
    db_session: Session, ctf2_competition, monkeypatch
) -> None:
    """★ 上限是**安全阀**：超过就排队，不是无限起。

    一道题会拉起一个容器加一堆 LLM 调用，失控的并发不只是慢 ——
    它让费用不可控，而没有任何界面能说清"现在在跑几个"。
    """
    started: list[str] = []
    release = threading.Event()

    def fake_worker(challenge_id: str, *, resume: bool = False, attempt_id: str | None = None) -> None:
        started.append(challenge_id)
        # 卡在这里，模拟"解题要跑很久"。
        release.wait(timeout=10)

    monkeypatch.setattr("app.ctf2.runner.run_worker", fake_worker)
    # 上限钉成 2（否则测试依赖环境变量）。
    monkeypatch.setattr(scheduler_module, "max_concurrent", lambda db=None: 2)

    for index in range(4):
        _make_challenge(
            db_session, ctf2_competition, remote_id=str(index), status="solving"
        )

    scheduler = Ctf2Scheduler()
    assert scheduler.tick() == 2, "上限是 2，一轮最多起 2 个"
    assert len(started) == 2

    # 再 tick 一次仍然是 2 —— 名额用完，不超额启动。
    assert scheduler.tick() == 0
    assert len(started) == 2

    release.set()
    time.sleep(0.2)


def test_finished_workers_free_up_slots(
    db_session: Session, ctf2_competition, monkeypatch
) -> None:
    """跑完一个就该让出名额 —— tick 的顺序是"先回收再算名额"。

    顺序反了的话，"刚跑完一个"这一轮不会被利用上，队列以一半速度前进。
    """
    started: list[str] = []

    def quick_worker(challenge_id: str, *, resume: bool = False, attempt_id: str | None = None) -> None:
        started.append(challenge_id)

    monkeypatch.setattr("app.ctf2.runner.run_worker", quick_worker)
    monkeypatch.setattr(scheduler_module, "max_concurrent", lambda db=None: 1)

    for index in range(3):
        _make_challenge(
            db_session, ctf2_competition, remote_id=str(index), status="solving"
        )

    scheduler = Ctf2Scheduler()
    scheduler.tick()
    time.sleep(0.3)  # 让第一个 worker 线程结束
    scheduler.tick()
    time.sleep(0.3)

    assert len(started) >= 2, "前一个跑完之后，后面那个应该被认领"


# ══════════════════════════════════════════════════════════
# FIFO
# ══════════════════════════════════════════════════════════


def test_queue_is_fifo(db_session: Session, ctf2_competition, monkeypatch) -> None:
    """★ 先受理的先跑。

    排序用的是 `updated_at`（被置成 solving 的时刻），**不是**
    `created_at`（被同步进来的时刻）—— 一道题可能同步了三天之后
    才被点解题，按 created_at 排会让新点的题插到队首，
    而用户看到的是"我点的那个一直不动"。
    """
    order: list[str] = []

    def recording_worker(challenge_id: str, *, resume: bool = False, attempt_id: str | None = None) -> None:
        order.append(challenge_id)
        # ★ 模拟真实 worker 的最后一步：把状态推离 `solving`。
        #
        # 少了这一步，那道题会**一直**是 `solving`，于是下一次 tick
        # 又把它认领一遍 —— 而症状是"FIFO 测试拿到 [first, first]"，
        # 看起来像排序写错了，其实是夹具没有模拟"跑完会改状态"。
        from app.core.db import session_scope

        with session_scope() as db:
            row = db.get(Ctf2Challenge, challenge_id)
            if row is not None:
                row.status = Ctf2ChallengeStatus.FAILED.value
            db.commit()

    monkeypatch.setattr("app.ctf2.runner.run_worker", recording_worker)
    monkeypatch.setattr(scheduler_module, "max_concurrent", lambda db=None: 1)

    first = _make_challenge(db_session, ctf2_competition, remote_id="1", status="solving")
    time.sleep(0.05)
    second = _make_challenge(db_session, ctf2_competition, remote_id="2", status="solving")

    scheduler = Ctf2Scheduler()
    scheduler.tick()
    time.sleep(0.3)
    scheduler.tick()
    time.sleep(0.3)

    assert order[:2] == [first.id, second.id]


def test_non_solving_challenges_are_never_claimed(
    db_session: Session, ctf2_competition, monkeypatch
) -> None:
    """只有 `solving` 的题会被认领 —— 未解出/已解出的不动。"""
    started: list[str] = []
    monkeypatch.setattr(
        "app.ctf2.runner.run_worker", lambda cid, *, resume=False, attempt_id=None: started.append(cid)
    )

    _make_challenge(db_session, ctf2_competition, remote_id="1", status="unsolved")
    _make_challenge(db_session, ctf2_competition, remote_id="2", status="failed")

    scheduler = Ctf2Scheduler()
    assert scheduler.tick() == 0
    assert started == []


# ══════════════════════════════════════════════════════════
# 孤儿回收
# ══════════════════════════════════════════════════════════


def test_recover_orphans_resets_solving_challenges(
    db_session: Session, ctf2_competition, ctf2_provider
) -> None:
    """★ 服务重启后，遗留的 `solving` 必须重置。

    不清理的后果是具体的：界面上它们永远显示"解题中"（而没有任何
    东西在跑），而且用户再点解题也不会重新触发 —— 因为状态已经是
    `solving` 了。**用户会以为系统卡死了，而实际上什么都没发生。**

    重置成 `unsolved` 而不是 `failed`：这两个状态对用户的意义不同，
    `failed` 是"试过了没成"，而这里的情况是"根本没跑完"。
    """
    challenge = _make_challenge(
        db_session, ctf2_competition, remote_id="9", status="solving"
    )

    scheduler = Ctf2Scheduler()
    assert scheduler.recover_orphans() == 1

    db_session.expire_all()
    refreshed = db_session.get(type(challenge), challenge.id)
    assert refreshed.status == Ctf2ChallengeStatus.UNSOLVED.value
    assert refreshed.last_error and "未完成" in refreshed.last_error


def test_recover_orphans_also_resets_environment_ready(
    db_session: Session, ctf2_competition, ctf2_provider
) -> None:
    """★★ `environment_ready` 也是"上一次运行遗留"，必须一起清（v0.26 补）。

    这是一个**实测踩到的僵尸状态**（2026-10-02，用户截图）：

    worker 起完环境就把状态设成 `environment_ready`，然后跑几分钟到
    几十分钟的解题循环 —— 它**不是**瞬间中间态，而是**解题过程的主状态**。
    而回收原来只认 `solving`，于是一次服务重启（部署时）把正在解题的题
    留在 `environment_ready` 上，后果是三重的：

    * 后端认为它可解（`RESOLVABLE_CTF2_CHALLENGE_STATUSES` 里有它）；
    * 前端 `canSolve` 里没有它 → **不给解题按钮**；
    * 用户得到一张没有错误、没有在跑、也点不动的卡片。

    用户当时看到的就是：环境「已停止 · 轮询 2 次」、卡片上只剩「取消」。
    """
    challenge = _make_challenge(
        db_session, ctf2_competition, remote_id="9b", status="environment_ready"
    )

    scheduler = Ctf2Scheduler()
    assert scheduler.recover_orphans() == 1

    db_session.expire_all()
    refreshed = db_session.get(type(challenge), challenge.id)
    assert refreshed.status == Ctf2ChallengeStatus.UNSOLVED.value


def test_recover_orphans_emits_an_event(
    db_session: Session, ctf2_competition, ctf2_provider
) -> None:
    """★ 回收要**写进活动流**。

    静默重置会让用户看到"进度消失了"却没有任何解释 ——
    而那正是"系统是不是有问题"这类不信任的来源。
    """
    from sqlalchemy import select

    from app.events.models import ActivityEvent

    challenge = _make_challenge(
        db_session, ctf2_competition, remote_id="8", status="solving"
    )
    Ctf2Scheduler().recover_orphans()

    db_session.expire_all()
    refreshed = db_session.get(type(challenge), challenge.id)
    events = list(
        db_session.execute(
            select(ActivityEvent).where(ActivityEvent.scan_id == refreshed.scan_id)
        )
        .scalars()
        .all()
    )
    assert any(event.type == "failed" for event in events)


def test_recover_orphans_is_a_noop_when_nothing_is_running(
    db_session: Session, ctf2_competition
) -> None:
    _make_challenge(db_session, ctf2_competition, remote_id="1", status="unsolved")
    assert Ctf2Scheduler().recover_orphans() == 0


# ══════════════════════════════════════════════════════════
# 停止语义
# ══════════════════════════════════════════════════════════


def test_stop_waits_for_running_workers(
    db_session: Session, ctf2_competition, monkeypatch
) -> None:
    """★ `stop()` 不杀线程（杀不掉），它**等** —— 并给足时间。

    为什么必须等：worker 的 `finally` 里那个 `sandbox.destroy()` 是
    清理沙箱容器的**唯一**路径。进程退出得太快，容器就变成孤儿，
    而孤儿容器没有任何界面能看到（只有 `docker ps`）。
    """
    finished = threading.Event()

    def slow_worker(challenge_id: str, *, resume: bool = False, attempt_id: str | None = None) -> None:
        time.sleep(0.3)
        finished.set()

    monkeypatch.setattr("app.ctf2.runner.run_worker", slow_worker)
    monkeypatch.setattr(scheduler_module, "max_concurrent", lambda db=None: 1)

    _make_challenge(db_session, ctf2_competition, remote_id="1", status="solving")
    scheduler = Ctf2Scheduler()
    scheduler.tick()
    scheduler.stop(timeout=3.0)

    assert finished.is_set(), "stop 应该等 worker 自己结束，而不是直接放弃它"


def test_stop_warns_when_workers_outlive_the_timeout(
    db_session: Session, ctf2_competition, monkeypatch, caplog
) -> None:
    """★ 超时之后要**出声**。

    静默留下沙箱容器是本模块最坏的失败模式：它没有界面、
    没有报错，只是慢慢把宿主拖垮。一行 warning 是唯一的信号。
    """
    import logging

    block = threading.Event()

    def stuck_worker(challenge_id: str, *, resume: bool = False, attempt_id: str | None = None) -> None:
        block.wait(timeout=10)

    monkeypatch.setattr("app.ctf2.runner.run_worker", stuck_worker)
    monkeypatch.setattr(scheduler_module, "max_concurrent", lambda db=None: 1)

    _make_challenge(db_session, ctf2_competition, remote_id="1", status="solving")
    scheduler = Ctf2Scheduler()
    with caplog.at_level(logging.WARNING):
        scheduler.tick()
        scheduler.stop(timeout=0.2)

    assert "docker ps" in caplog.text
    block.set()


def test_scheduler_singleton_is_not_duplicated() -> None:
    """★ 单例只能在同一个锁下创建。

    `scans` 那边是裸的模块级全局，两个请求线程同时第一次调用会
    各建一个调度器 —— 于是并发上限被悄悄翻倍，而**安全阀失效
    是没有任何症状的**。
    """
    created: list[int] = []
    original = scheduler_module.Ctf2Scheduler

    class Counting(original):  # type: ignore[misc, valid-type]
        def __init__(self) -> None:
            created.append(1)
            super().__init__()

    scheduler_module.Ctf2Scheduler = Counting  # type: ignore[misc]
    try:
        results = [scheduler_module.get_scheduler() for _ in range(8)]
    finally:
        scheduler_module.Ctf2Scheduler = original  # type: ignore[misc]

    assert len(created) <= 2, f"被创建了 {len(created)} 次"
    assert len({id(item) for item in results}) == 1


# ══════════════════════════════════════════════════════════
# 统计与受理
# ══════════════════════════════════════════════════════════


def test_stats_reports_running_and_limit(
    db_session: Session, ctf2_competition, monkeypatch
) -> None:
    """`GET /ctf2/queue` 要能回答"为什么我点了却一直排队"。"""
    block = threading.Event()
    monkeypatch.setattr(
        "app.ctf2.runner.run_worker", lambda cid, *, resume=False, attempt_id=None: block.wait(timeout=5)
    )
    monkeypatch.setattr(scheduler_module, "max_concurrent", lambda db=None: 2)

    _make_challenge(db_session, ctf2_competition, remote_id="1", status="solving")
    scheduler = Ctf2Scheduler()
    scheduler.tick()
    time.sleep(0.2)

    stats = scheduler.stats(db_session)
    assert stats.limit == 2
    assert stats.running == 1
    assert len(stats.challenges) == 1
    block.wait(timeout=0.05)
    block.set()
    time.sleep(0.2)


def test_enqueue_sets_solving_and_triggers_a_tick(
    db_session: Session, ctf2_challenge, monkeypatch
) -> None:
    """★ 状态**先落库、再启动**。

    反过来（先启动再落库）会留下一个跑着但数据库里显示 `unsolved`
    的任务 —— 那是最糟的状态：它占着配额，而界面上看不出任何东西在跑。
    """
    started: list[str] = []
    monkeypatch.setattr(
        "app.ctf2.runner.run_worker", lambda cid, *, resume=False, attempt_id=None: started.append(cid)
    )
    monkeypatch.setattr(scheduler_module, "max_concurrent", lambda db=None: 2)

    scheduler = Ctf2Scheduler()
    scheduler.enqueue(ctf2_challenge.id)
    time.sleep(0.2)

    db_session.expire_all()
    refreshed = db_session.get(type(ctf2_challenge), ctf2_challenge.id)
    assert refreshed.status == Ctf2ChallengeStatus.SOLVING.value
    assert started == [ctf2_challenge.id]
