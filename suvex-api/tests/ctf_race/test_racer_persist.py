"""赛跑落库的状态口径（2026-11-08 真库事故）。

## 事故

整场托管跑完之后，5 道题在界面上是「未开始」，候选列里却摆着
`DASCTF{...}` —— 而它们的 `last_error` 都是「已取消。」。

链路是这样的：

1. `_persist_single_result` 给产出了候选的那道题写 `status=solving`
   （而那一轮其实**已经结束**）；
2. 界面把它显示成「解题中」，行内还挂着「取消」；
3. 用户（或批量取消）点了取消 → `unsolved` + 「已取消。」，候选**没清**。

于是一道跑完、有候选的题，最后变成一行谁也读不懂的记录。

三条必须钉住的口径：

* 有候选 → `candidate_ready`（不是 `solving`，也不是 `failed`）；
* 没拿到 flag 的兄弟 solver **不能**把已有候选的题降级；
* 取消要连候选一起作废（否则又回到"未开始却有 flag"）。
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from app.core.vocab import KernelChallengeStatus
from app.ctf_race import competition_racer as cr


class _FakeChallenge:
    def __init__(self, status: str = KernelChallengeStatus.SOLVING.value) -> None:
        self.status = status
        self.candidate_flag: str | None = None
        self.confirmed_flag: str | None = None
        self.last_error: str | None = None


class _FakeDB:
    def __init__(self, challenge: _FakeChallenge) -> None:
        self._challenge = challenge
        self.committed = 0

    def get(self, _model, _id):  # noqa: ANN001
        return self._challenge

    def commit(self) -> None:
        self.committed += 1


def _racer_for(challenge: _FakeChallenge) -> cr.CompetitionRacer:
    db = _FakeDB(challenge)

    @contextmanager
    def factory():
        yield db

    racer = cr.CompetitionRacer(factory)
    racer._test_db = db  # type: ignore[attr-defined]
    return racer


async def test_candidate_lands_as_candidate_ready() -> None:
    """★★ 有候选 → `candidate_ready`。写 `solving` 会让这道题看起来永远在跑。"""
    challenge = _FakeChallenge()
    racer = _racer_for(challenge)

    await racer._persist_single_result(
        "chal_1", flag="flag{x}", confirmed=False, message=""
    )

    assert challenge.status == KernelChallengeStatus.CANDIDATE_READY.value
    assert challenge.candidate_flag == "flag{x}"
    assert challenge.last_error is None


async def test_empty_solver_does_not_downgrade_a_sibling_candidate() -> None:
    """★ fan-out：兄弟 solver 已落候选，这一路空手而归不能把它降级成 failed。"""
    challenge = _FakeChallenge(status=KernelChallengeStatus.CANDIDATE_READY.value)
    challenge.candidate_flag = "flag{from_sibling}"
    racer = _racer_for(challenge)

    await racer._persist_single_result(
        "chal_1", flag=None, confirmed=False, message="没找到"
    )

    assert challenge.status == KernelChallengeStatus.CANDIDATE_READY.value
    assert challenge.candidate_flag == "flag{from_sibling}"


async def test_empty_solver_marks_failed_when_nothing_was_found() -> None:
    challenge = _FakeChallenge()
    racer = _racer_for(challenge)

    await racer._persist_single_result(
        "chal_1", flag=None, confirmed=False, message="没找到"
    )

    assert challenge.status == KernelChallengeStatus.FAILED.value
    assert challenge.last_error == "没找到"


async def test_confirmed_flag_goes_through_advance(monkeypatch) -> None:
    """确认路径走 `_advance_challenge` —— 那里管着两条 CHECK 约束。"""
    challenge = _FakeChallenge()
    racer = _racer_for(challenge)
    calls: list[tuple] = []

    def fake_advance(ch, status, flag):  # noqa: ANN001
        calls.append((status, flag))
        ch.status = KernelChallengeStatus.SOLVED.value
        ch.confirmed_flag = flag

    monkeypatch.setattr("app.kernel.runner._advance_challenge", fake_advance)

    await racer._persist_single_result(
        "chal_1", flag="flag{ok}", confirmed=True, message=""
    )

    assert calls == [("correct", "flag{ok}")]
    assert challenge.status == KernelChallengeStatus.SOLVED.value
