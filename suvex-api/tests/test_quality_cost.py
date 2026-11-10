"""质量快照的成本桶（v1.81）。"""

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


def _challenge(db: Session) -> KernelChallenge:
    competition = KernelCompetition(
        id=new_id("ctf2comp"),
        account_id=None,
        remote_id=new_id("local"),
        kind="local",
        name="成本测试",
        remote_status="ongoing",
        challenge_count=1,
        synced_at=now_utc(),
    )
    db.add(competition)
    db.flush()
    challenge = KernelChallenge(
        id=new_id("ctf2chal"),
        competition_id=competition.id,
        remote_id="cost-1",
        stage_id=None,
        title="成本题",
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
    db.commit()
    return challenge


def _attempt(db: Session, challenge: KernelChallenge) -> KernelSolveAttempt:
    from app.core.vocab import KernelSubmitMode

    row = KernelSolveAttempt(
        id=new_id("ctf2try"),
        challenge_id=challenge.id,
        mode=KernelSubmitMode.SAFE.value,
        max_turns=40,
        sandbox_profile="suvex-sandbox",
        llm_model="deepseek-v4.1-flash",
    )
    db.add(row)
    db.commit()
    return row


def _usage(
    db: Session,
    *,
    attempt_id: str,
    challenge_id: str,
    model: str,
    prompt: int,
    completion: int,
    total: int,
    reported: bool = True,
) -> None:
    db.add(
        KernelSolveUsage(
            id=new_id("ctf2usage"),
            attempt_id=attempt_id,
            challenge_id=challenge_id,
            turn=1,
            model=model,
            provider_label="test",
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
            reasoning_tokens=0,
            usage_reported=reported,
        )
    )
    db.commit()


def test_quality_cost_aggregates_tokens_and_usd(db_session: Session) -> None:
    from app.overview.quality import quality_snapshot

    challenge = _challenge(db_session)
    attempt = _attempt(db_session, challenge)
    _usage(
        db_session,
        attempt_id=attempt.id,
        challenge_id=challenge.id,
        model="deepseek-v4.1-flash",
        prompt=100,
        completion=200,
        total=300,
    )
    _usage(
        db_session,
        attempt_id=attempt.id,
        challenge_id=challenge.id,
        model="unknown-model",
        prompt=0,
        completion=0,
        total=50,
        reported=False,
    )

    cost = quality_snapshot(db_session)["cost"]

    assert cost["totalTokens"] == 350
    assert cost["promptTokens"] == 100
    assert cost["completionTokens"] == 200
    assert cost["calls"] == 2
    assert cost["unreportedCalls"] == 1
    assert cost["unpricedTokens"] == 50
    # 100 * 0.10/M + 200 * 0.40/M = 0.00009
    assert cost["usd"] == 0.0001
