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


# ══════════════════════════════════════════════════════════
# 四、生产遥测：一次解题一行（v0.45）
# ══════════════════════════════════════════════════════════
#
# ## 这组用例守的是什么
#
# 这张表存在的唯一目的，是回答"跨题方法卡到底有没有用"—— 而那个问题
# 用人工造的基准题**测不出效应**（九道 mock 题干净基线平均 4.3 回合 /
# 5.3 次工具调用，本来就没有重复探索可省；n=1 时看到的"工具调用 −21%"
# 在 n=5 下被推翻，差异 100% 来自一道结构上两臂恒为 0 张卡的题）。
#
# 于是改从真实比赛里收样本。样本要成立，下面每一条都得对：
#   · **一次解题一行**（upsert 会把样本数永久压成 1）；
#   · `attempt_id` 为空也照写（否则遥测只覆盖"走产品路径的那部分"）；
#   · `solved` 取自 `challenge.status`（写早了会把成功记成失败）；
#   · `category` 冗余存（否则同步改分类会让历史统计漂移）。
#
# ⚠️ 这些失败模式**全都是安静的**：行数少一行、某个数是 0、分类跟着变了，
#    都不会让任何既有用例变红，而统计结论会整个错掉。


def _telemetry_outcome(*, turns: int = 3, tool_calls: int = 9, notes_count: int = 2):
    """最小的 outcome 形状（遥测只读这三个数，别的一概不碰）。"""
    from types import SimpleNamespace

    return SimpleNamespace(turns=turns, tool_calls=tool_calls, notes_count=notes_count)


def test_telemetry_writes_one_row_per_attempt(db_session: Session, ctf2_challenge) -> None:
    """★★ **一次解题一行** —— 这是与 `ctf2_writeups`（一题一份）的根本分野。

    同一道题解两次就该有两行。upsert 会让第二次覆盖第一次，
    于是"后来那次为什么更快"这个唯一想回答的问题永远没有数据。

    ⚠️ 这条是整个 v0.45 的存在理由，不是细节。
    """
    from app.ctf2.models import Ctf2SolveTelemetry

    first = _attempt(db_session, ctf2_challenge.id)
    service.record_solve_telemetry(
        db_session,
        attempt_id=first.id,
        challenge=ctf2_challenge,
        outcome=_telemetry_outcome(turns=7, tool_calls=19, notes_count=3),
    )

    second = _attempt(db_session, ctf2_challenge.id)
    service.record_solve_telemetry(
        db_session,
        attempt_id=second.id,
        challenge=ctf2_challenge,
        outcome=_telemetry_outcome(turns=4, tool_calls=5, notes_count=0),
    )

    rows = (
        db_session.execute(
            select(Ctf2SolveTelemetry).order_by(Ctf2SolveTelemetry.created_at)
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2, "两次解题必须两行 —— 覆盖就丢掉了一半样本"
    assert [(r.turns, r.tool_calls, r.notes_count) for r in rows] == [
        (7, 19, 3),
        (4, 5, 0),
    ]
    assert [r.attempt_id for r in rows] == [first.id, second.id]


def test_telemetry_is_written_even_without_an_attempt(
    db_session: Session, ctf2_challenge
) -> None:
    """★ `attempt_id` 为空时**照写**，而不是像 usage 那样整批跳过。

    历史任务与脚本直调 `run_worker` 都没有快照，它们的回合数一样是有效
    样本。为了外键好看把它们丢掉，等于让遥测只覆盖"走产品路径的那部分"
    ——而那恰好是最不需要被诊断的部分。
    """
    from app.ctf2.models import Ctf2SolveTelemetry

    ok = service.record_solve_telemetry(
        db_session,
        attempt_id=None,
        challenge=ctf2_challenge,
        outcome=_telemetry_outcome(),
    )

    assert ok is True
    row = db_session.execute(select(Ctf2SolveTelemetry)).scalars().one()
    assert row.attempt_id is None
    assert row.turns == 3


def test_telemetry_tolerates_an_outcome_without_the_new_fields(
    db_session: Session, ctf2_challenge
) -> None:
    """★ 老 outcome 没有 `tool_calls` / `notes_count` 属性 —— 记 0，不抛。

    这是升级兼容：某个还在跑的长任务，它的 `SolveOutcome` 是**升级前**
    构造的类实例。让"新字段不存在"使遥测失败，会让那段升级期整段没数据。
    """
    from types import SimpleNamespace

    from app.ctf2.models import Ctf2SolveTelemetry

    ok = service.record_solve_telemetry(
        db_session,
        attempt_id=None,
        challenge=ctf2_challenge,
        outcome=SimpleNamespace(turns=5),
    )

    assert ok is True
    row = db_session.execute(select(Ctf2SolveTelemetry)).scalars().one()
    assert (row.turns, row.tool_calls, row.notes_count) == (5, 0, 0)


def test_telemetry_records_solved_from_the_challenge_status(
    db_session: Session, ctf2_challenge
) -> None:
    """`solved` 取自 `challenge.status` —— 所以调用点必须在 `_record_outcome` 之后。

    ⚠️ 这条同时钉住了那条**顺序**约束：写早了 `status` 还是旧的，
    于是一次成功的解题被记成 `solved=False`，"解出率"从此偏低，
    而偏低不会被任何人发现。
    """
    from app.ctf2.models import Ctf2SolveTelemetry

    ctf2_challenge.status = "solved"
    ctf2_challenge.confirmed_flag = "flag{ours}"
    ctf2_challenge.candidate_flag = None
    db_session.commit()

    service.record_solve_telemetry(
        db_session,
        attempt_id=None,
        challenge=ctf2_challenge,
        outcome=_telemetry_outcome(),
    )

    row = db_session.execute(select(Ctf2SolveTelemetry)).scalars().one()
    assert row.solved is True


def test_telemetry_freezes_the_category_at_write_time(
    db_session: Session, ctf2_challenge
) -> None:
    """★ `category` 是**当时的快照** —— 之后改分类不该改写历史行。

    方法卡是**同分类**召回的，统计必须先按分类切开。而
    `challenges.category` 会在同步时被平台侧改写；不冗余一份的话，
    "Web 题不需要卡"和"Crypto 题有卡也没用"会随着一次改名搅成一团。
    """
    from app.ctf2.models import Ctf2SolveTelemetry

    assert ctf2_challenge.category == "web"

    service.record_solve_telemetry(
        db_session,
        attempt_id=None,
        challenge=ctf2_challenge,
        outcome=_telemetry_outcome(),
    )

    ctf2_challenge.category = "web-lfi"
    db_session.commit()

    row = db_session.execute(select(Ctf2SolveTelemetry)).scalars().one()
    assert row.category == "web", "历史行必须记得当时的分类，而不是现在的"


def test_telemetry_rejects_negative_numbers_without_raising(
    db_session: Session, ctf2_challenge
) -> None:
    """负数被数据库 CHECK 拦下 → `rollback` + `False`，**不抛**。

    判据是"宁可这一行写不进去，也别污染统计"，但**不能**让记账失败
    把一次成功的解题变成报错 —— 所以每条写入路径都要留住那个 False。
    """
    from app.ctf2.models import Ctf2SolveTelemetry

    ok = service.record_solve_telemetry(
        db_session,
        attempt_id=None,
        challenge=ctf2_challenge,
        outcome=_telemetry_outcome(turns=-1),
    )

    assert ok is False
    assert db_session.execute(select(func.count(Ctf2SolveTelemetry.id))).scalar() == 0
    # ★ 会话必须还能用 —— 回滚之后继续查是"不抛"的实际含义
    assert db_session.execute(select(func.count(Ctf2SolveTelemetry.id))).scalar() == 0


def test_telemetry_failure_does_not_roll_back_the_solve_result(
    db_session: Session, ctf2_challenge
) -> None:
    """☆☆ **本表最重要的一条**：遥测写失败绝不能让解题结果一起回滚。

    调用点 `_run_worker` 的 ③ 同步段只有**最后一次** `db.commit()`：

        _record_outcome(db, challenge, outcome)   # 只改内存，不 commit
        record_solve_telemetry(...)               # ← 这里
        record_solve_usages(...)
        save_writeup(...)
        db.commit()                               # ← 全段唯一的一次

    所以本函数内部一句朴素的 `db.rollback()` 会把 `_record_outcome`
    **还没提交**的 `status` / `confirmed_flag` 一并抹掉。实测（真库）：

        改内存后 status = solved
        record_solve_telemetry -> False
        失败后（同一对象）status = unsolved      ← 内存里的改动没了
        ★★ 重新从库里读 status = unsolved | flag = None

    ⇒ 一次**成功的解题**被记成未解出，而它是安静的：解题照常返回成功，
    日志里只有一条 warning，只有事后翻题目状态才会发现。
    这与本函数的整个承诺（"失败不影响解题结果"）正好相反。

    修法是 `db.begin_nested()`（SAVEPOINT）：失败只退到保存点，调用方
    未提交的改动原样保留。
    """
    from app.ctf2.models import Ctf2SolveTelemetry

    # 模拟 `_record_outcome`：改内存 + flush，**不 commit**。
    ctf2_challenge.status = "solved"
    ctf2_challenge.confirmed_flag = "flag{ours}"
    db_session.flush()

    ok = service.record_solve_telemetry(
        db_session,
        attempt_id=None,
        challenge=ctf2_challenge,
        outcome=_telemetry_outcome(turns=-1),  # 撞 CHECK ⇒ 必然失败
    )

    assert ok is False
    # ★ 内存里刚改的解出状态必须还在（不能已被 rollback 抹掉）
    assert ctf2_challenge.status == "solved"
    assert ctf2_challenge.confirmed_flag == "flag{ours}"

    # ★★ 而且必须真的提交得进去 —— 前面那句才是"没被回滚"的证明
    db_session.commit()
    db_session.expire_all()
    fresh = db_session.get(type(ctf2_challenge), ctf2_challenge.id)
    assert fresh is not None
    assert fresh.status == "solved", "★ 遥测失败把一次成功的解题回滚掉了"
    assert fresh.confirmed_flag == "flag{ours}"
    # 遥测确实没写成（这是"失败"该有的结果）
    assert db_session.execute(select(func.count(Ctf2SolveTelemetry.id))).scalar() == 0


def test_usage_failure_does_not_roll_back_the_solve_result(
    db_session: Session, ctf2_challenge
) -> None:
    """★ 与 `test_telemetry_failure_does_not_roll_back_the_solve_result` 对称。

    `record_solve_usages` 原先写的是 `db.commit()` + `except: db.rollback()`。
    它在 `_run_worker` 的 ③ 同步段里被调用，而那一段里 `_record_outcome`
    刚改过 `challenge.status` / `confirmed_flag` 却**没提交**（全段只有
    末尾一次 `db.commit()`）—— 所以那次 `rollback()` 会把解题结果一起吞掉。

    ⚠️ 判据必须是"**真的提交得进去**"，不能只断言内存里的属性还在：
    内存属性在 `rollback()` 之后由 SQLAlchemy 按需重读，看起来可能还是对的。
    """
    attempt = _attempt(db_session, ctf2_challenge.id)
    # 模拟 `_record_outcome`：改内存 + flush，**不 commit**。
    ctf2_challenge.status = "solved"
    ctf2_challenge.confirmed_flag = "flag{ours}"
    db_session.flush()

    # turn 从 1 起（CHECK 约束）⇒ turn=0 必然写库失败
    n = service.record_solve_usages(
        db_session,
        attempt_id=attempt.id,
        challenge_id=ctf2_challenge.id,
        outcome=_outcome(usages=[(0, "m1", "label-a", _usage())]),
    )

    assert n == 0
    assert ctf2_challenge.status == "solved"

    db_session.commit()
    db_session.expire_all()
    fresh = db_session.get(type(ctf2_challenge), ctf2_challenge.id)
    assert fresh is not None
    assert fresh.status == "solved", "★ token 统计失败把一次成功的解题回滚掉了"
    assert fresh.confirmed_flag == "flag{ours}"
    # 统计确实没写成（这是"失败"该有的结果）
    assert db_session.execute(select(func.count(Ctf2SolveUsage.id))).scalar() == 0


def test_writeup_failure_does_not_roll_back_the_solve_result(
    db_session: Session, ctf2_challenge
) -> None:
    """★ 同一条纪律，第三处：题解落库失败也不能连累解题结果。

    原来同样是 `db.commit()` + `except: db.rollback()`。题解是**附加产物**
    （赛后提交用的材料），它写不进去只是少份文件，绝不能把"这题解出了"
    这条事实一起抹掉 —— 那会让界面显示未解出，而实际已经解出了。
    """
    assert ctf2_challenge.status != "solved"
    ctf2_challenge.status = "solved"
    ctf2_challenge.confirmed_flag = "flag{ours}"
    db_session.flush()

    # 外键指向一条不存在的 attempt ⇒ INSERT 必然违约
    service.save_writeup(
        db_session,
        challenge=ctf2_challenge,
        outcome=_outcome(transcript="[turn 1] result: hi", turns=1),
        attempt_id="ctf2try_does_not_exist",
        elapsed_sec=5,
    )

    assert ctf2_challenge.status == "solved"

    db_session.commit()
    db_session.expire_all()
    fresh = db_session.get(type(ctf2_challenge), ctf2_challenge.id)
    assert fresh is not None
    assert fresh.status == "solved", "★ 题解落库失败把一次成功的解题回滚掉了"
    assert db_session.execute(select(func.count(Ctf2Writeup.id))).scalar() == 0


def test_telemetry_survives_attempt_deletion(db_session: Session, ctf2_challenge) -> None:
    """attempt 外键是 SET NULL —— 快照被清掉后观测仍然算数。

    ⚠️ 与 usage 表的 CASCADE **刻意不同**：用量是那一次尝试的附属明细
    （尝试没了就没有意义），而观测说的是"这题解了几次、各花多少轮"。
    """
    from app.ctf2.models import Ctf2SolveTelemetry

    attempt = _attempt(db_session, ctf2_challenge.id)
    service.record_solve_telemetry(
        db_session,
        attempt_id=attempt.id,
        challenge=ctf2_challenge,
        outcome=_telemetry_outcome(),
    )

    db_session.delete(attempt)
    db_session.commit()

    row = db_session.execute(select(Ctf2SolveTelemetry)).scalars().one()
    assert row.attempt_id is None
    assert row.turns == 3, "观测本身不该跟着快照一起消失"


def test_telemetry_is_cascaded_with_its_challenge(db_session: Session, ctf2_challenge) -> None:
    """题被删掉 → 它的观测跟着消失（不留孤儿）。

    与上一条方向相反，这是刻意的：challenge 是观测的**归属**，
    尝试只是它的**出处**。
    """
    from app.ctf2.models import Ctf2Challenge, Ctf2SolveTelemetry

    service.record_solve_telemetry(
        db_session,
        attempt_id=None,
        challenge=ctf2_challenge,
        outcome=_telemetry_outcome(),
    )

    db_session.delete(db_session.get(Ctf2Challenge, ctf2_challenge.id))
    db_session.commit()

    assert db_session.execute(select(func.count(Ctf2SolveTelemetry.id))).scalar() == 0


def test_recall_notes_returns_both_text_and_count(
    db_session: Session, ctf2_challenge
) -> None:
    """★ `_recall_notes` 返回 `(片段, 张数)` —— 张数是统计的分母。

    只看文本空不空会把"1 张"和"3 张"抹平，而这两者对"方法卡值不值"
    的意义完全不同。

    ⚠️ 这里同时钉住另一件事：**分类为空时返回 `("", 0)`**。给全部的
    后果是把无关题的方法灌进 prompt，比不给更坏。
    """
    from app.core.vocab import Ctf2ChallengeStatus, Ctf2SandboxProfile
    from app.ctf2.models import Ctf2Challenge, Ctf2SolveNote

    # 两张卡必须挂在**别道题**上：召回规则排除当前题（同一道题再解时
    # 该读的是自己的 transcript，而不是自己那张卡）。
    for index in range(2):
        other = Ctf2Challenge(
            id=new_id("ctf2chal"),
            competition_id=ctf2_challenge.competition_id,
            remote_id=f"other-{index}",
            stage_id=None,
            title=f"同类题{index}",
            category=ctf2_challenge.category,
            description="",
            attachments=[],
            score=100,
            status=Ctf2ChallengeStatus.UNSOLVED.value,
            candidate_flag=None,
            confirmed_flag=None,
            wrong_submission_count=0,
            max_wrong_submissions=3,
            candidate_count=0,
            sandbox_profile=Ctf2SandboxProfile.BASE.value,
        )
        db_session.add(other)
        db_session.flush()
        db_session.add(
            Ctf2SolveNote(
                id=new_id("ctf2note"),
                challenge_id=other.id,
                category=other.category,
                solved=True,
                approach="先看入口参数",
                tools="curl",
                dead_ends="",
            )
        )
    db_session.commit()

    text, count = runner._recall_notes(db_session, ctf2_challenge)
    assert count == 2
    assert text, "有卡就该有片段"

    ctf2_challenge.category = ""
    db_session.commit()

    text, count = runner._recall_notes(db_session, ctf2_challenge)
    assert (text, count) == ("", 0), "分类为空必须一张都不给"
