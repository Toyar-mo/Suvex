"""新题自动作业：**手动同步也要生效**（2026-11-09）。

## 事故

「新题自动优先」「新题自动开始解题」两个开关只在**自动同步**线程里读。
用户手动点「同步题目」发现 20 道新题时：一道不会被优先、一道不会开跑，
而界面上没有任何地方说这件事 —— 开关看起来是坏的。

现在两条路径共用一个函数（`autosync.run_new_challenge_automation`），
行为只有一份。

⚠️ `_enqueue` 被换成记录器：绝不让真 worker 起来。
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.vocab import CTF2_AUTO_PRIORITY_BASE as AUTO_PRIORITY_BASE
from app.core.vocab import KernelChallengeStatus
from app.kernel import autosync
from app.kernel.models import KernelChallenge


@pytest.fixture(autouse=True)
def _never_enqueue(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    calls: list[tuple] = []

    def _record(challenge_id: str, *, attempt_id: str | None = None) -> None:
        calls.append((challenge_id, attempt_id))

    monkeypatch.setattr("app.kernel.service._enqueue", _record)
    return calls


def _make(db: Session, template: KernelChallenge, **overrides) -> KernelChallenge:
    from app.core.ids import new_id

    kwargs = {
        "id": new_id("ctf2chal"),
        "competition_id": template.competition_id,
        "remote_id": new_id("r"),
        "stage_id": None,
        "title": "新题",
        "category": "web",
        "description": None,
        "attachments": [],
        "score": 300,
        "status": KernelChallengeStatus.UNSOLVED.value,
        "candidate_flag": None,
        "confirmed_flag": None,
        "wrong_submission_count": 0,
        "max_wrong_submissions": 3,
        "candidate_count": 0,
        "sandbox_profile": template.sandbox_profile,
    }
    kwargs.update(overrides)
    row = KernelChallenge(**kwargs)
    db.add(row)
    db.commit()
    return row


def test_priority_and_start_run_for_the_given_ids(
    db_session: Session,
    ctf2_challenge: KernelChallenge,
    provider_id: str,
    _never_enqueue: list[tuple],
) -> None:
    fresh = _make(db_session, ctf2_challenge, title="刚同步进来的题", score=300)

    prioritized, started = autosync.run_new_challenge_automation(
        [fresh.id], auto_priority=True, auto_start=True
    )

    assert prioritized == 1
    assert started == 1
    db_session.expire_all()
    row = db_session.get(KernelChallenge, fresh.id)
    assert row.priority == AUTO_PRIORITY_BASE + 300
    assert [call[0] for call in _never_enqueue] == [fresh.id]


def test_both_off_is_a_noop(
    db_session: Session,
    ctf2_challenge: KernelChallenge,
    _never_enqueue: list[tuple],
) -> None:
    fresh = _make(db_session, ctf2_challenge, title="安静地进来")

    prioritized, started = autosync.run_new_challenge_automation(
        [fresh.id], auto_priority=False, auto_start=False
    )

    assert (prioritized, started) == (0, 0)
    assert _never_enqueue == []
    db_session.expire_all()
    assert db_session.get(KernelChallenge, fresh.id).priority == 0


def test_empty_list_does_nothing(_never_enqueue: list[tuple]) -> None:
    assert autosync.run_new_challenge_automation([], auto_priority=True, auto_start=True) == (0, 0)
