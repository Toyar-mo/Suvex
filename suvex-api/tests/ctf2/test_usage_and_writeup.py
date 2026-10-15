"""CTF2 的 token 统计与题解落库/端点（v0.36）。

## 这组用例守的是什么

`app/ctf2/service.py` 里新增的四个函数（`record_solve_usages` /
`save_writeup` / `get_writeup` / `token_usage_summary` /
`challenge_usage_entries`）在补这批用例之前**一条都没被测试引用**：
上一轮会话把它们写完就断了。它们的失败模式都很安静 —— 统计记错、
题解存了两份、端点返回的字段与契约不符，都不会让任何既有用例变红。

## ★ 反复守的一条纪律：`calls` 与 `recordedCalls` 不能合成一个

接入点没给用量时 token 记 0。如果把这两个数合成一个，
"消耗 0"与"没记录"在界面上就一样了 —— 与 `hasKey`/`apiKey`
分两列是同一条纪律。所以这里有专门一条用例盯着它们**不相等**。

## 题解为什么是 upsert 而不是每次插一行

题解是给赛后提交用的，一题一份。多次尝试产多份会让"提交哪一份"
变成新问题。所以这里验证"第二次解的题解**覆盖**第一份"。

## 与 `test_writeup.py` 的分工

那边测 `build_writeup` 的**纯拼装**（给定输入必得同一输出）；
这边测**落库与读取**（谁能写进去、谁读得出来、口径对不对）。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.ids import new_id
from app.ctf2 import runner, service
from app.ctf2.models import Ctf2SolveUsage, Ctf2Writeup

# ══════════════════════════════════════════════════════════
# 夹具：一条策略快照 + 一个 outcome
# ══════════════════════════════════════════════════════════


def _attempt(db: Session, challenge_id: str, *, llm_model: str = "test-model"):
    """造一条最小可用的解题尝试（usage 表有非空外键指向它）。

    ⚠️ `llm_model` **默认给一个非空值**，与生产一致 —— 受理时
    `service._snapshot_provider_model` 会填它。留空的话，
    "`byAttempt` 的 model 来自 attempt 快照"这条断言就永远为真
    （空串也是假值，测不出 JOIN 有没有接上）。
    """
    from app.core.vocab import Ctf2SubmitMode
    from app.ctf2.models import Ctf2SolveAttempt

    row = Ctf2SolveAttempt(
        id=new_id("ctf2try"),
        challenge_id=challenge_id,
        mode=Ctf2SubmitMode.SAFE.value,
        max_turns=40,
        sandbox_profile="ctf2-sandbox",
        llm_model=llm_model,
    )
    db.add(row)
    db.commit()
    return row


def _usage(*, prompt=10, completion=20, total=0, reported=True):
    return runner.TokenUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        reported=reported,
    )


def _outcome(*, usages=None, transcript="", turns=3, candidates=None):
    from types import SimpleNamespace

    return SimpleNamespace(
        usages=usages if usages is not None else [],
        transcript=transcript,
        turns=turns,
        candidates=candidates if candidates is not None else [],
        error=None,
    )


# ══════════════════════════════════════════════════════════
# 一、逐轮用量落库
# ══════════════════════════════════════════════════════════


def test_record_solve_usages_writes_one_row_per_call(
    db_session: Session, ctf2_challenge
) -> None:
    """每次 LLM 调用一行 —— 这正是"细分到每一次任务"的数据来源。

    如果改成在 attempt 上加累计列，这条会红（行数变成 1 或 0）。
    """
    attempt = _attempt(db_session, ctf2_challenge.id)
    outcome = _outcome(
        usages=[
            (1, "m1", "label-a", _usage(prompt=10, completion=20, total=30)),
            (2, "m1", "label-a", _usage(prompt=5, completion=5, total=10)),
            (3, "m2", "label-b", _usage(prompt=1, completion=2, total=3)),
        ]
    )

    n = service.record_solve_usages(
        db_session, attempt_id=attempt.id, challenge_id=ctf2_challenge.id, outcome=outcome
    )

    assert n == 3
    rows = service.challenge_usage_entries(db_session, ctf2_challenge.id)
    assert len(rows) == 3
    assert [r.turn for r in rows] == [1, 2, 3]
    # ★ 模型与接入点标签都记下来了 —— 换模后要能算出"换模前后各花多少"
    assert rows[2].model == "m2"
    assert rows[2].provider_label == "label-b"


def test_usage_reported_flag_survives_the_round_trip(
    db_session: Session, ctf2_challenge
) -> None:
    """★ `usage_reported` 必须真的落库 —— "0"与"没记录"靠它区分。

    它是这一整套统计里最容易在落库时被漏掉的一列：漏了之后
    token 全是 0，而界面上看不出"没记录"。
    """
    attempt = _attempt(db_session, ctf2_challenge.id)
    outcome = _outcome(
        usages=[
            (1, "m", "l", _usage(prompt=10, completion=20, total=30, reported=True)),
            (2, "m", "l", _usage(prompt=0, completion=0, total=0, reported=False)),
        ]
    )

    service.record_solve_usages(
        db_session, attempt_id=attempt.id, challenge_id=ctf2_challenge.id, outcome=outcome
    )

    rows = service.challenge_usage_entries(db_session, ctf2_challenge.id)
    assert [r.usage_reported for r in rows] == [True, False]


def test_record_solve_usages_skips_without_an_attempt(
    db_session: Session, ctf2_challenge
) -> None:
    """★ 拿不到 attempt 时**跳过**（而不是抛）—— 统计缺失可以接受，
    因为没快照就让解题失败不行。

    ⚠️ 它必须**出声**（见下面那条 caplog 用例），否则
    "为什么这道题没有 token 记录"会变成一个查不出来的谜。
    """
    outcome = _outcome(usages=[(1, "m", "l", _usage())])

    n = service.record_solve_usages(
        db_session, attempt_id=None, challenge_id=ctf2_challenge.id, outcome=outcome
    )

    assert n == 0
    # 一条行都不该写进去
    assert db_session.execute(select(func.count(Ctf2SolveUsage.id))).scalar() == 0


def test_record_solve_usages_logs_a_warning_when_skipping(
    db_session: Session, ctf2_challenge, caplog
) -> None:
    """跳过时要留下 warning —— 静默跳过是最坏的选择。"""
    import logging

    outcome = _outcome(usages=[(1, "m", "l", _usage())])
    with caplog.at_level(logging.WARNING):
        service.record_solve_usages(
            db_session, attempt_id=None, challenge_id=ctf2_challenge.id, outcome=outcome
        )

    assert any("无处可挂" in r.getMessage() for r in caplog.records)


def test_record_solve_usages_with_no_rows_is_a_noop(
    db_session: Session, ctf2_challenge
) -> None:
    """一轮都没跑（沙箱起不来）时是正常情况 —— 不写行、也不刷 warning。"""
    attempt = _attempt(db_session, ctf2_challenge.id)

    n = service.record_solve_usages(
        db_session,
        attempt_id=attempt.id,
        challenge_id=ctf2_challenge.id,
        outcome=_outcome(usages=[]),
    )

    assert n == 0
    assert db_session.execute(select(func.count(Ctf2SolveUsage.id))).scalar() == 0


def test_usage_rows_are_removed_with_their_attempt(
    db_session: Session, ctf2_challenge
) -> None:
    """外键是 CASCADE —— 删掉尝试，用量明细一并消失（不留孤儿行）。"""
    attempt = _attempt(db_session, ctf2_challenge.id)
    service.record_solve_usages(
        db_session,
        attempt_id=attempt.id,
        challenge_id=ctf2_challenge.id,
        outcome=_outcome(usages=[(1, "m", "l", _usage())]),
    )

    db_session.delete(attempt)
    db_session.commit()

    assert db_session.execute(select(func.count(Ctf2SolveUsage.id))).scalar() == 0


# ══════════════════════════════════════════════════════════
# 二、聚合口径：calls 与 recordedCalls 必须分开
# ══════════════════════════════════════════════════════════


def test_summary_separates_calls_from_recorded_calls(
    db_session: Session, ctf2_challenge
) -> None:
    """★★ 三次调用里两次有用量 → `calls=3`、`recordedCalls=2`。

    合成一个字段的症状很具体：接入点没给 usage 时界面显示"消耗 0"，
    与"没跑过"混淆 —— 这正是这一对字段要消除的歧义。
    """
    attempt = _attempt(db_session, ctf2_challenge.id)
    service.record_solve_usages(
        db_session,
        attempt_id=attempt.id,
        challenge_id=ctf2_challenge.id,
        outcome=_outcome(
            usages=[
                (1, "m", "l", _usage(prompt=10, completion=20, total=30)),
                (2, "m", "l", _usage(prompt=0, completion=0, total=0, reported=False)),
                (3, "m", "l", _usage(prompt=5, completion=5, total=10)),
            ]
        ),
    )

    data = service.token_usage_summary(db_session)

    assert data["calls"] == 3
    assert data["recordedCalls"] == 2
    # ★ 40 而不是 40+0 的"假完整"：total 只累计真给了用量的那些
    assert data["totalTokens"] == 40


def test_summary_since_is_the_earliest_row(db_session: Session, ctf2_challenge) -> None:
    """★ `since` 说明"统计从哪一刻开始" —— 不给它，用户会以为总共就花了这么点。"""
    attempt = _attempt(db_session, ctf2_challenge.id)
    service.record_solve_usages(
        db_session,
        attempt_id=attempt.id,
        challenge_id=ctf2_challenge.id,
        outcome=_outcome(usages=[(1, "m", "l", _usage(prompt=10, completion=20, total=30))]),
    )

    data = service.token_usage_summary(db_session)

    assert data["since"] is not None


def test_empty_summary_is_all_zeros_not_an_error(db_session: Session) -> None:
    """一条记录都没有时返回零值结构（而不是抛）—— 总览页要能正常渲染。"""
    data = service.token_usage_summary(db_session)

    assert data["calls"] == 0
    assert data["recordedCalls"] == 0
    assert data["totalTokens"] == 0
    assert data["since"] is None
    assert data["byModel"] == []
    assert data["byChallenge"] == []
    assert data["byAttempt"] == []


# ══════════════════════════════════════════════════════════
# ★ v0.37：按「每次解题」分组（需求要的"细分到每个任务"）
# ══════════════════════════════════════════════════════════


def test_summary_groups_by_attempt(db_session: Session, ctf2_challenge) -> None:
    """★★ 同一次解题的多轮调用聚成一行，带上 mode / model / 受理时刻。

    需求原话是"细分到**每个任务**的消耗"，而"任务"= 一次解题，不是一道题。
    只按题聚合会把"哪一次跑得贵"糊掉。
    """
    attempt = _attempt(db_session, ctf2_challenge.id)
    service.record_solve_usages(
        db_session,
        attempt_id=attempt.id,
        challenge_id=ctf2_challenge.id,
        outcome=_outcome(
            usages=[
                (1, "m1", "l1", _usage(prompt=10, completion=20, total=30)),
                (2, "m1", "l1", _usage(prompt=5, completion=5, total=10)),
            ]
        ),
    )

    data = service.token_usage_summary(db_session)

    assert len(data["byAttempt"]) == 1
    row = data["byAttempt"][0]
    assert row["attemptId"] == attempt.id
    assert row["challengeId"] == ctf2_challenge.id
    assert row["calls"] == 2
    assert row["totalTokens"] == 40
    # ★ 这三样来自 attempt 表（JOIN 拿的）—— 只给一个 id 的话用户读不出
    #   "哪次是 safe、哪次用哪个模型"。
    assert row["mode"] == "safe"
    assert row["model"] == "test-model"
    assert row["startedAt"] is not None


def test_two_attempts_of_one_challenge_are_separate_rows(
    db_session: Session, ctf2_challenge
) -> None:
    """★★ 同一道题解两次 → `byAttempt` 两行，而 `byChallenge` 只有一行。

    这正是加 `byAttempt` 的理由：实测 `[ACTF2020 新生赛]Upload` 解过 4 次，
    按题聚合会把它们糊成一行。
    """
    first = _attempt(db_session, ctf2_challenge.id)
    service.record_solve_usages(
        db_session,
        attempt_id=first.id,
        challenge_id=ctf2_challenge.id,
        outcome=_outcome(usages=[(1, "m1", "l1", _usage(prompt=10, completion=20, total=30))]),
    )
    second = _attempt(db_session, ctf2_challenge.id)
    service.record_solve_usages(
        db_session,
        attempt_id=second.id,
        challenge_id=ctf2_challenge.id,
        outcome=_outcome(
            usages=[(1, "m1", "l1", _usage(prompt=100, completion=200, total=300))]
        ),
    )

    data = service.token_usage_summary(db_session)

    assert len(data["byAttempt"]) == 2
    assert len(data["byChallenge"]) == 1
    # 最贵的排最前
    assert data["byAttempt"][0]["attemptId"] == second.id
    assert data["byAttempt"][0]["totalTokens"] == 300


def test_attempt_totals_sum_to_the_grand_total(db_session: Session, ctf2_challenge) -> None:
    """★★ 逐次之和 == 总量 —— 这条盯的是 INNER JOIN 静默丢行。

    `byAttempt` 要 JOIN `ctf2_solve_attempts` 拿 mode/model。若哪天
    `usage.attempt_id` 变成可空（或有孤儿行），INNER JOIN 会**静默丢行**：
    界面上"总消耗 5000"而逐次相加只有 3000，而没有任何报错。
    这条断言让那种情况变红。
    """
    first = _attempt(db_session, ctf2_challenge.id)
    second = _attempt(db_session, ctf2_challenge.id)
    service.record_solve_usages(
        db_session,
        attempt_id=first.id,
        challenge_id=ctf2_challenge.id,
        outcome=_outcome(usages=[(1, "m1", "l1", _usage(prompt=10, completion=20, total=30))]),
    )
    service.record_solve_usages(
        db_session,
        attempt_id=second.id,
        challenge_id=ctf2_challenge.id,
        outcome=_outcome(
            usages=[(1, "m1", "l1", _usage(prompt=100, completion=200, total=300))]
        ),
    )

    data = service.token_usage_summary(db_session)

    assert sum(r["totalTokens"] for r in data["byAttempt"]) == data["totalTokens"]
    assert sum(r["calls"] for r in data["byAttempt"]) == data["calls"]


def test_attempt_breakdown_respects_the_challenge_filter(
    db_session: Session, ctf2_challenge, ctf2_reverse_challenge
) -> None:
    """筛选参数对 `byAttempt` 同样生效（传了不生效比没有这个参数更坏）。"""
    a1 = _attempt(db_session, ctf2_challenge.id)
    a2 = _attempt(db_session, ctf2_reverse_challenge.id)
    service.record_solve_usages(
        db_session,
        attempt_id=a1.id,
        challenge_id=ctf2_challenge.id,
        outcome=_outcome(usages=[(1, "m", "l", _usage(prompt=10, completion=20, total=30))]),
    )
    service.record_solve_usages(
        db_session,
        attempt_id=a2.id,
        challenge_id=ctf2_reverse_challenge.id,
        outcome=_outcome(usages=[(1, "m", "l", _usage(prompt=1, completion=2, total=3))]),
    )

    scoped = service.token_usage_summary(db_session, challenge_id=ctf2_challenge.id)

    assert len(scoped["byAttempt"]) == 1
    assert scoped["byAttempt"][0]["attemptId"] == a1.id


def test_summary_groups_by_model_for_fallback(db_session: Session, ctf2_challenge) -> None:
    """★ 按模型分组 —— 换模（v0.35）后"换模前后各花多少"靠它回答。"""
    attempt = _attempt(db_session, ctf2_challenge.id)
    service.record_solve_usages(
        db_session,
        attempt_id=attempt.id,
        challenge_id=ctf2_challenge.id,
        outcome=_outcome(
            usages=[
                (1, "primary", "pa", _usage(prompt=10, completion=20, total=30)),
                (2, "backup", "pb", _usage(prompt=100, completion=200, total=300)),
            ]
        ),
    )

    data = service.token_usage_summary(db_session)
    by_model = {m["model"]: m for m in data["byModel"]}

    assert by_model["primary"]["calls"] == 1
    assert by_model["primary"]["totalTokens"] == 30
    assert by_model["backup"]["totalTokens"] == 300
    # 最贵的排最前（界面按这个顺序展示）
    assert data["byModel"][0]["model"] == "backup"


def test_summary_filters_by_challenge(db_session: Session, ctf2_challenge, ctf2_reverse_challenge) -> None:
    """按题筛选：`challengeId` 只统计这道题的调用。"""
    a1 = _attempt(db_session, ctf2_challenge.id)
    a2 = _attempt(db_session, ctf2_reverse_challenge.id)
    service.record_solve_usages(
        db_session, attempt_id=a1.id, challenge_id=ctf2_challenge.id,
        outcome=_outcome(usages=[(1, "m", "l", _usage(prompt=10, completion=20, total=30))]),
    )
    service.record_solve_usages(
        db_session, attempt_id=a2.id, challenge_id=ctf2_reverse_challenge.id,
        outcome=_outcome(usages=[(1, "m", "l", _usage(prompt=1, completion=2, total=3))]),
    )

    scoped = service.token_usage_summary(db_session, challenge_id=ctf2_challenge.id)

    assert scoped["calls"] == 1
    assert scoped["totalTokens"] == 30


def test_summary_filters_by_competition(db_session: Session, ctf2_challenge, ctf2_competition) -> None:
    """按赛事筛选走 challenge 关联（usage 表没有 competition_id 列）。"""
    attempt = _attempt(db_session, ctf2_challenge.id)
    service.record_solve_usages(
        db_session, attempt_id=attempt.id, challenge_id=ctf2_challenge.id,
        outcome=_outcome(usages=[(1, "m", "l", _usage(prompt=10, completion=20, total=30))]),
    )

    hit = service.token_usage_summary(db_session, competition_id=ctf2_competition.id)
    miss = service.token_usage_summary(db_session, competition_id="comp_does_not_exist")

    assert hit["calls"] == 1
    assert miss["calls"] == 0


def test_summary_by_challenge_lists_the_most_expensive_first(
    db_session: Session, ctf2_challenge, ctf2_reverse_challenge
) -> None:
    """总览页要回答"哪道题最烧钱" —— 所以按 token 降序。"""
    a1 = _attempt(db_session, ctf2_challenge.id)
    a2 = _attempt(db_session, ctf2_reverse_challenge.id)
    service.record_solve_usages(
        db_session, attempt_id=a1.id, challenge_id=ctf2_challenge.id,
        outcome=_outcome(usages=[(1, "m", "l", _usage(prompt=10, completion=20, total=30))]),
    )
    service.record_solve_usages(
        db_session, attempt_id=a2.id, challenge_id=ctf2_reverse_challenge.id,
        outcome=_outcome(usages=[(1, "m", "l", _usage(prompt=500, completion=500, total=1000))]),
    )

    data = service.token_usage_summary(db_session)

    assert data["byChallenge"][0]["challengeId"] == ctf2_reverse_challenge.id
    assert data["byChallenge"][0]["totalTokens"] == 1000


# ══════════════════════════════════════════════════════════
# 三、题解落库：一题一份（upsert）
# ══════════════════════════════════════════════════════════


def test_save_writeup_inserts_once(db_session: Session, ctf2_challenge) -> None:
    attempt = _attempt(db_session, ctf2_challenge.id)

    service.save_writeup(
        db_session,
        challenge=ctf2_challenge,
        outcome=_outcome(transcript="[turn 1] result: hi", turns=1),
        attempt_id=attempt.id,
        elapsed_sec=42,
    )

    rows = db_session.execute(select(Ctf2Writeup)).scalars().all()
    assert len(rows) == 1
    assert rows[0].challenge_id == ctf2_challenge.id
    assert rows[0].turns == 1
    assert rows[0].elapsed_sec == 42
    assert "# CTF Writeup" in rows[0].markdown


def test_save_writeup_updates_on_a_second_solve(db_session: Session, ctf2_challenge) -> None:
    """★ 重解时**覆盖**那一份，而不是再插一份 —— 一题一份。

    多份会让"赛后提交哪一份"变成新问题。
    """
    first = _attempt(db_session, ctf2_challenge.id)
    service.save_writeup(
        db_session, challenge=ctf2_challenge,
        outcome=_outcome(transcript="[turn 1] result: first", turns=1),
        attempt_id=first.id, elapsed_sec=10,
    )
    second = _attempt(db_session, ctf2_challenge.id)
    service.save_writeup(
        db_session, challenge=ctf2_challenge,
        outcome=_outcome(transcript="[turn 1] result: second", turns=7),
        attempt_id=second.id, elapsed_sec=99,
    )

    rows = db_session.execute(select(Ctf2Writeup)).scalars().all()
    assert len(rows) == 1
    assert rows[0].attempt_id == second.id
    assert rows[0].turns == 7
    assert rows[0].elapsed_sec == 99


def test_save_writeup_records_whether_it_was_solved(
    db_session: Session, ctf2_challenge
) -> None:
    """★ 未解出**也产出**题解 —— "我们试过什么、卡在哪"本身就是提交材料。

    所以 `solved` 只影响措辞，不影响产不产出。这条盯着"未解出也写了库"。
    """
    attempt = _attempt(db_session, ctf2_challenge.id)
    # 夹具的题是 unsolved
    assert ctf2_challenge.status != "solved"

    service.save_writeup(
        db_session, challenge=ctf2_challenge,
        outcome=_outcome(transcript="[turn 1] result: nothing", turns=1),
        attempt_id=attempt.id, elapsed_sec=None,
    )

    row = service.get_writeup(db_session, ctf2_challenge.id)
    assert row is not None
    assert row.solved is False
    assert "未解出" in row.markdown


def test_get_writeup_returns_none_when_never_solved(db_session: Session, ctf2_challenge) -> None:
    """没解过 → `None`（由路由决定报哪句话）。"""
    assert service.get_writeup(db_session, ctf2_challenge.id) is None


def test_writeup_survives_attempt_deletion(db_session: Session, ctf2_challenge) -> None:
    """题解的 attempt 外键是 SET NULL —— 删掉尝试不该毁掉题解。

    ⚠️ 与 usage 表的 CASCADE **刻意不同**：用量是尝试的附属明细，
    而题解是**独立产物**（赛后提交用的），尝试没了它也该留着。
    """
    attempt = _attempt(db_session, ctf2_challenge.id)
    service.save_writeup(
        db_session, challenge=ctf2_challenge,
        outcome=_outcome(transcript="[turn 1] result: hi", turns=1),
        attempt_id=attempt.id, elapsed_sec=None,
    )

    db_session.delete(attempt)
    db_session.commit()

    row = service.get_writeup(db_session, ctf2_challenge.id)
    assert row is not None
    assert row.attempt_id is None


def test_writeups_are_cascaded_with_their_challenge(db_session: Session, ctf2_challenge) -> None:
    """题随题解一起删（challenge 外键是 CASCADE）—— 不留孤儿题解。

    ⚠️ 与上一条的方向相反，这是刻意的：
    challenge 是题解的**归属**，尝试只是它的**出处**。
    """
    from app.ctf2.models import Ctf2Challenge

    attempt = _attempt(db_session, ctf2_challenge.id)
    service.save_writeup(
        db_session, challenge=ctf2_challenge,
        outcome=_outcome(transcript="[turn 1] result: hi", turns=1),
        attempt_id=attempt.id, elapsed_sec=None,
    )

    db_session.delete(db_session.get(Ctf2Challenge, ctf2_challenge.id))
    db_session.commit()

    assert db_session.execute(select(func.count(Ctf2Writeup.id))).scalar() == 0
