"""Token 按天序列（v1.82）。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.ids import new_id
from app.core.timeutil import now_utc
from app.kernel.models import (
    KernelChallenge,
    KernelCompetition,
    KernelSolveAttempt,
    KernelSolveUsage,
)
from app.overview.token_usage import token_series


def _usage(db: Session, *, prompt: int, completion: int, reported: bool = True) -> None:
    from app.core.vocab import KernelSubmitMode

    competition = KernelCompetition(
        id=new_id("ctf2comp"),
        account_id=None,
        remote_id=new_id("local"),
        kind="local",
        name="token 测试",
        remote_status="ongoing",
        challenge_count=1,
        synced_at=now_utc(),
    )
    db.add(competition)
    db.flush()
    challenge = KernelChallenge(
        id=new_id("ctf2chal"),
        competition_id=competition.id,
        remote_id=new_id("c"),
        stage_id=None,
        title="t",
        category="misc",
        description=None,
        attachments=[],
        score=100,
        status="unsolved",
        candidate_flag=None,
        confirmed_flag=None,
        wrong_submission_count=0,
        max_wrong_submissions=3,
        candidate_count=0,
        sandbox_profile="suvex-sandbox",
    )
    db.add(challenge)
    db.flush()
    attempt = KernelSolveAttempt(
        id=new_id("ctf2try"),
        challenge_id=challenge.id,
        mode=KernelSubmitMode.SAFE.value,
        max_turns=40,
        sandbox_profile="suvex-sandbox",
        llm_model="deepseek-v4.1-flash",
    )
    db.add(attempt)
    db.flush()
    db.add(
        KernelSolveUsage(
            id=new_id("ctf2usage"),
            attempt_id=attempt.id,
            challenge_id=challenge.id,
            turn=1,
            model="deepseek-v4.1-flash",
            provider_label="test",
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
            reasoning_tokens=0,
            usage_reported=reported,
        )
    )
    db.commit()


def test_series_fills_missing_days_and_totals(db_session: Session) -> None:
    _usage(db_session, prompt=100, completion=200)
    _usage(db_session, prompt=0, completion=0, reported=False)

    out = token_series(db_session, days=7)

    assert out["days"] == 7
    assert len(out["series"]) == 7
    assert out["totals"]["prompt"] == 100
    assert out["totals"]["completion"] == 200
    assert out["totals"]["total"] == 300
    assert out["totals"]["unreportedCalls"] == 1
    assert out["cacheTracked"] is False
    assert out["cacheRate"] is None
    assert out["series"][-1]["total"] == 300


def test_unsupported_days_falls_back_to_30(db_session: Session) -> None:
    assert len(token_series(db_session, days=13)["series"]) == 30
