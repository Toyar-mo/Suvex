"""解题 scan 的状态要跟着「这道题到底解出没有」走（v0.48）。

## 这组用例守的是那次实测的界面错报

Kali 生产库，SimpleRev（`remote_id=d79788be-b7e5-40bb-b79c-4595694646d4`）：

    16:43:25  自动提交 flag{efxkwoxzti} → 平台判错
    16:53:09  worker 收尾：challenge 还是 candidate_ready → scan=failed (582s)
    16:53:50  人工提交 flag{KLDQCUDFZO} → 平台判对 → challenge=solved

「失败」比确认早了 41 秒，而**此后没有任何代码回头改那行 scan** ——
界面上它永远停在那里。用户看到的结论是「suvex 把这题搞失败了」，
事实是这题已经解出来了。

## 为什么不能只靠 `finalize_solve_scan`

那个函数只在 **worker 收尾那一刻**看一眼题目状态。safe 模式下那一刻
候选还在等人确认，题目必然还不是 `solved` —— 所以它**必然**把 scan
记成 `failed`。这不是它的 bug，是它看的时间点太早。

## 四条承诺

| # | 承诺 | 由哪个用例守 |
| --- | --- | --- |
| 1 | 已收尾的 scan 会被翻成 `completed` | `test_failed_scan_is_promoted_to_completed` |
| 2 | **不动** `finished_at` / `duration_sec` | `test_promotion_keeps_the_real_duration` |
| 3 | 还在 `running` 的 scan **不许**提前翻 | `test_running_scan_is_left_alone` |
| 4 | 重复调用是幂等的 | `test_promotion_is_idempotent` |

外加一条源码级断言：`submit_candidate` 的「判对」分支确实调了它 ——
否则上面四条全绿而真实路径一次都不会走到（`test_submit_candidate_
promotes_on_correct`）。

为什么第 2 条要单独守：把 `finished_at` 按确认时刻重算，会把用户**等待
确认**的那段时间算进解题耗时。那是一个更难发现的错 —— 它看上去只是
一个偏大的数字，不会报错、不会红，只会让「这道题花了多久」系统性说谎，
而那个数还喂给「疑似卡死」判定。
"""

from __future__ import annotations

import inspect

import pytest
from sqlalchemy.orm import Session

from app.core.vocab import TERMINAL_SCAN_STATUSES, AgentStatus, ScanStatus
from app.ctf2 import events as ctf2_events
from app.ctf2 import runner
from tests.factories import make_scan


def _challenge_with_scan(db: Session, *, status: str, **overrides):
    """造一道挂着某条 scan 的题。

    ⚠️ `challenge.scan_id` 是**单值列** —— 一条 scan 只能属于一道题。
    所以每次都造新 scan，不复用。
    """
    from tests.ctf2.conftest import _make_challenge  # noqa: PLC2701

    competition = overrides.pop("competition", None)
    assert competition is not None, "调用方必须给 competition"
    scan = make_scan(db, status=status, target="ctf2:scan-status-test")
    return _make_challenge(
        db, competition, remote_id="scan-status", scan_id=scan.id, **overrides
    ), scan


@pytest.fixture()
def _pair(db_session: Session, ctf2_competition):
    """一条 `failed` 的收尾 scan + 一道只有候选的题（就是生产里的形状）。"""

    def _make(*, status: str = ScanStatus.FAILED.value):
        return _challenge_with_scan(
            db_session, status=status, competition=ctf2_competition
        )

    return _make


def test_failed_scan_is_promoted_to_completed(db_session: Session, _pair) -> None:
    """★★ 收尾时记成 `failed` 的 scan，在题目确认解出后要变成 `completed`。

    这**就是**用户报的那条：界面显示失败，而题其实解出来了。
    """
    challenge, scan = _pair(status=ScanStatus.FAILED.value)
    assert scan.status == ScanStatus.FAILED.value

    # solver agent 在收尾时也被记成了 failed（同一个判据），一并核对。
    agent = ctf2_events.ensure_solver_agent(db_session, scan.id)
    agent.status = AgentStatus.FAILED.value
    db_session.commit()

    ctf2_events.promote_solve_scan_to_solved(db_session, challenge)
    db_session.commit()
    db_session.refresh(scan)
    db_session.refresh(agent)

    assert scan.status == ScanStatus.COMPLETED.value
    assert agent.status == AgentStatus.DONE.value, (
        "solver agent 与 scan 同源，只翻 scan 会让智能体页继续显示失败"
    )


def test_promotion_keeps_the_real_duration(db_session: Session, _pair) -> None:
    """★ 只翻状态，**不动** `finished_at` 与 `duration_sec`。

    那两列记的是 worker **实际跑了多久**。按确认时刻重算会把用户等待
    确认的时间算进解题耗时 —— 一个不报错、只让数字系统性说谎的错。
    """
    challenge, scan = _pair(status=ScanStatus.FAILED.value)
    finished_at = scan.finished_at
    duration = scan.duration_sec
    assert finished_at is not None and duration is not None, (
        "前置数据错了：failed 是终态，必须带 finished_at/duration_sec"
    )

    ctf2_events.promote_solve_scan_to_solved(db_session, challenge)
    db_session.commit()
    db_session.refresh(scan)

    assert scan.finished_at == finished_at, "收尾时刻不该被确认时刻覆盖"
    assert scan.duration_sec == duration, "耗时记的是 worker 跑了多久，不是等到确认"


def test_running_scan_is_left_alone(db_session: Session, _pair) -> None:
    """★★ 还在跑的 scan **不许**提前翻成 completed。

    auto 模式在 worker 还在干活的时候就提交候选。那时 scan 是 `running`，
    把它标成「已完成」会让界面显示完成而进程还在跑 —— 比原来的错报更糟。
    """
    challenge, scan = _pair(status=ScanStatus.RUNNING.value)
    assert scan.status == ScanStatus.RUNNING.value

    ctf2_events.promote_solve_scan_to_solved(db_session, challenge)
    db_session.commit()
    db_session.refresh(scan)

    assert scan.status == ScanStatus.RUNNING.value


def test_promotion_is_idempotent(db_session: Session, _pair) -> None:
    """★ 已经 `completed` 就什么都不做。

    人工确认路径会被反复触发（同一个 flag 可以再交一次）。重复翻状态
    没有意义，但重复 commit 有代价。
    """
    challenge, scan = _pair(status=ScanStatus.COMPLETED.value)
    ctf2_events.promote_solve_scan_to_solved(db_session, challenge)
    ctf2_events.promote_solve_scan_to_solved(db_session, challenge)
    db_session.commit()
    db_session.refresh(scan)

    assert scan.status == ScanStatus.COMPLETED.value


def test_aborted_scan_is_also_promoted(db_session: Session, _pair) -> None:
    """用户中途 abort 过、后来又手动确认对 —— 也要翻。

    `TERMINAL_SCAN_STATUSES` 里三个都是「已经结束」，而「结束」与
    「这道题解没解出」是两件事。
    """
    challenge, scan = _pair(status=ScanStatus.ABORTED.value)
    ctf2_events.promote_solve_scan_to_solved(db_session, challenge)
    db_session.commit()
    db_session.refresh(scan)

    assert scan.status == ScanStatus.COMPLETED.value


def test_challenge_without_scan_is_a_no_op(db_session: Session, ctf2_challenge) -> None:
    """题目没挂 scan（淘汰赛题目、或从没解过）时安静返回，不抛。"""
    assert ctf2_challenge.scan_id is None
    ctf2_events.promote_solve_scan_to_solved(db_session, ctf2_challenge)
    db_session.commit()


def test_terminal_set_covers_every_end_state() -> None:
    """`TERMINAL_SCAN_STATUSES` 必须是「翻状态」判据的唯一来源。

    这条本身很小，但它钉住了一个容易漂的地方：如果有人往 `ScanStatus`
    加了第五个终态而忘了加进这个集合，`promote` 就会对它静默失效 ——
    而那正是「界面永远显示失败」的同一个症状。
    """
    assert {s.value for s in TERMINAL_SCAN_STATUSES} == {
        ScanStatus.COMPLETED.value,
        ScanStatus.FAILED.value,
        ScanStatus.ABORTED.value,
    }


def test_submit_candidate_promotes_on_correct() -> None:
    """源码级断言：`submit_candidate` 的「判对」分支确实调了 promote。

    为什么需要这一条：上面六个用例测的是 `promote_solve_scan_to_solved`
    本身，而它**可能一次都没被调用**。`submit_candidate` 是自动与人工
    两条提交路径唯一的汇合处，接在这里才两路都修好。
    """
    source = inspect.getsource(runner.submit_candidate)
    assert "promote_solve_scan_to_solved" in source, (
        "submit_candidate 没有调用 promote_solve_scan_to_solved —— "
        "人工确认之后 scan 会永远停在 failed"
    )
    assert "Ctf2SubmissionStatus.CORRECT.value" in source
    # 必须在「判对」分支里，不是在随便某个地方。
    correct_idx = source.index("Ctf2SubmissionStatus.CORRECT.value")
    promote_idx = source.index("promote_solve_scan_to_solved")
    assert promote_idx > correct_idx, (
        "调用点出现在判对分支之前，那它对 failed/wrong 也会触发"
    )
