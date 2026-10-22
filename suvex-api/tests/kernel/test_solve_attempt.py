"""解题策略快照（v0.28）：受理时定下的策略必须贯穿到 worker。

## 这组用例守的是什么

`POST /ctf2/challenges/{id}/solve` 的 `mode` / `brief` / 回合预算
原本**全部没有真正生效**：

* `mode` 在 `service.start_solve` 里被 `del` 掉，worker 现读全局设置；
* `brief` 一路断在 router（schema 有、service 没参数、prompt 没位置）；
* `max_turns` 与 `mode` 共用同一个 `settings_row`，同样现读。

`mode` 那条最严重 —— 它是一条**时序漏洞**：

    用户选 safe（"我要人工确认"）→ 排队 → 期间有人把全局改成 auto
    → worker 读到 auto → **系统替用户做了一个他明确拒绝过的对外提交**

而消耗的是**不可再生**的错误提交额度。

## ★ 为什么必须正反都测（V1 与 V2）

只测"选 safe 时不会被 auto 覆盖"是不够的：一个**永远强制 safe**
的错误实现也能让那条通过。所以 V2 反向验证"选 auto 时不会被 safe
覆盖" —— 两条一起才能证明"用的是受理时那个值"，而不是"总是取更
保守的那个"。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.ids import new_id
from app.core.vocab import KernelSubmitMode
from app.kernel import runner, service
from app.kernel.models import KernelSolveAttempt


def _set_global(db: Session, *, mode: str | None = None, turns: int | None = None) -> None:
    """改全局 CTF2 设置（模拟"排队期间有人动了设置"）。

    ⚠️ 必须走 `update_ctf2_settings`，不能直接改 ORM 行：
    `effective_ctf2_settings` 返回的是 **frozen dataclass**（`KernelSettings`），
    赋值会抛 `FrozenInstanceError` —— 那是刻意的（设置快照不该被就地改）。

    ⚠️ 两个场景包开关是**必填关键字参数**（v0.44），所以这里必须
    显式透传当前值。这看起来啰嗦，但那正是那条改动的目的：漏传
    会被 Python 当场拒绝，而不是把开关静默关掉。
    """
    row = service.get_ctf2_config(db)
    service.update_ctf2_settings(
        db,
        submit_mode=mode or row.submit_mode,
        max_wrong_submissions=row.max_wrong_submissions,
        default_sandbox_profile=row.default_sandbox_profile,
        max_concurrent_solves=row.max_concurrent_solves,
        solve_poll_seconds=row.solve_poll_seconds,
        auto_sync_seconds=row.auto_sync_seconds,
        auto_priority_new=row.auto_priority_new,
        auto_start_new=row.auto_start_new,
        max_turns=turns or row.max_turns,
        enable_agentsec_scenarios=row.enable_agentsec_scenarios,
        enable_ir_scenarios=row.enable_ir_scenarios,
        enable_multi_agent=row.enable_multi_agent,
    )


def _reset_to_unsolved(db: Session, challenge) -> None:  # noqa: ANN001
    """把题目状态复位成"可以再解一次"。

    `start_solve` 的 `_assert_resolvable` 会拒绝 `solving` 状态的题
    （"这道题正在处理中"）—— 所以连续受理两次之前必须复位。
    这不是绕过校验，而是模拟真实时序：第一次跑完之后用户又点了一次。
    """
    from app.core.vocab import KernelChallengeStatus

    challenge.status = KernelChallengeStatus.UNSOLVED.value
    db.commit()


def _attempt_for(db: Session, challenge_id: str) -> KernelSolveAttempt:
    row = (
        db.execute(
            select(KernelSolveAttempt)
            .where(KernelSolveAttempt.challenge_id == challenge_id)
            .order_by(KernelSolveAttempt.created_at.desc())
        )
        .scalars()
        .first()
    )
    assert row is not None, "受理之后应当有一条策略快照"
    return row


# ══════════════════════════════════════════════════════════
# 一、快照本身：受理时落库，内容正确
# ══════════════════════════════════════════════════════════


def test_start_solve_writes_an_attempt_snapshot(
    db_session: Session, ctf2_challenge, ctf2_config, ctf2_provider
) -> None:
    """★ 受理一次解题会落一条快照，字段来自本次入参。"""
    service.start_solve(
        db_session,
        ctf2_challenge,
        mode=KernelSubmitMode.SAFE.value,
        brief="考点在反调试",
    )

    attempt = _attempt_for(db_session, ctf2_challenge.id)
    assert attempt.mode == KernelSubmitMode.SAFE.value
    assert attempt.brief == "考点在反调试"
    assert attempt.max_turns > 0
    assert attempt.sandbox_profile


def test_brief_is_trimmed_and_blank_becomes_none(
    db_session: Session, ctf2_challenge, ctf2_config, ctf2_provider
) -> None:
    """★ 空串归一成 `None` —— 否则界面会显示一个空白的"人工提示"区块。

    `""` 与"没填"在语义上是同一件事，存空串会让前端以为"用户填过"。
    """
    service.start_solve(db_session, ctf2_challenge, brief="   ")
    assert _attempt_for(db_session, ctf2_challenge.id).brief is None


def test_brief_is_trimmed(
    db_session: Session, ctf2_challenge, ctf2_config, ctf2_provider
) -> None:
    """前后空白要去掉（用户从别处粘贴时很常见）。"""
    service.start_solve(db_session, ctf2_challenge, brief="  libc 2.27  \n")
    assert _attempt_for(db_session, ctf2_challenge.id).brief == "libc 2.27"


def test_attempt_snapshots_the_provider(
    db_session: Session, ctf2_challenge, ctf2_config, ctf2_provider
) -> None:
    """★ 模型也快照：provider 配置会被改，而"这次用哪个"必须与当时一致。"""
    service.start_solve(db_session, ctf2_challenge)
    attempt = _attempt_for(db_session, ctf2_challenge.id)
    assert attempt.provider_id
    assert attempt.llm_model


def test_attempt_records_resume_flag(
    db_session: Session, ctf2_challenge, ctf2_config, ctf2_provider
) -> None:
    """续解标志落库 —— 内存里的 `_resume` 会随重启丢失，快照不会。"""
    service.start_solve(db_session, ctf2_challenge, resume=True)
    assert _attempt_for(db_session, ctf2_challenge.id).resume is True


# ══════════════════════════════════════════════════════════
# 二、★★ V1/V2：排队期间改全局设置**不影响**已受理的尝试
# ══════════════════════════════════════════════════════════


def test_v1_safe_survives_a_global_switch_to_auto(
    db_session: Session, ctf2_challenge, ctf2_config, ctf2_provider
) -> None:
    """★★ V1：选 `safe` 受理后，全局改成 `auto`，这次仍按 **safe** 跑。

    这是那条时序漏洞的直接回归闸：修复前 `build_spec` 会读到改后的
    全局值（auto），于是**替用户做了一个他明确拒绝过的对外提交**。
    """
    service.start_solve(db_session, ctf2_challenge, mode=KernelSubmitMode.SAFE.value)

    # 排队期间有人把全局改成 auto
    _set_global(db_session, mode=KernelSubmitMode.AUTO.value)

    attempt = _attempt_for(db_session, ctf2_challenge.id)
    spec = runner.build_spec(db_session, ctf2_challenge, attempt_id=attempt.id)

    assert spec.mode == KernelSubmitMode.SAFE.value, (
        "★ 受理时选的 safe 被全局设置覆盖了 —— 这正是那条时序漏洞"
    )


def test_v2_auto_survives_a_global_switch_to_safe(
    db_session: Session, ctf2_challenge, ctf2_config, ctf2_provider
) -> None:
    """★★ V2（反向）：选 `auto` 受理后全局改成 `safe`，这次仍按 **auto**。

    ## 为什么这条不能省

    只测 V1 的话，一个"永远强制 safe"的错误实现也能通过 —— 而它
    同样是错的（用户在 auto 模式下期待自动提交，却被静默降级）。
    两条一起才证明"用的是**受理时那个值**"，而不是"总取更保守的"。
    """
    service.start_solve(db_session, ctf2_challenge, mode=KernelSubmitMode.AUTO.value)

    _set_global(db_session, mode=KernelSubmitMode.SAFE.value)

    attempt = _attempt_for(db_session, ctf2_challenge.id)
    spec = runner.build_spec(db_session, ctf2_challenge, attempt_id=attempt.id)

    assert spec.mode == KernelSubmitMode.AUTO.value, (
        "★ 受理时选的 auto 被全局设置覆盖了（或实现退化成了'永远 safe'）"
    )


def test_v4_turns_survive_a_global_change(
    db_session: Session, ctf2_challenge, ctf2_config, ctf2_provider
) -> None:
    """★ V4：回合预算同样锁定 —— 排队期间改预算不影响已受理的尝试。"""
    _set_global(db_session, turns=120)
    service.start_solve(db_session, ctf2_challenge)

    _set_global(db_session, turns=7)  # 排队期间有人把它改小了

    attempt = _attempt_for(db_session, ctf2_challenge.id)
    spec = runner.build_spec(db_session, ctf2_challenge, attempt_id=attempt.id)

    assert spec.max_turns == 120, "★ 受理时的回合预算被后来的全局改动覆盖了"


# ══════════════════════════════════════════════════════════
# 三、brief 真的到了 prompt 里（V3）
# ══════════════════════════════════════════════════════════


def test_v3_brief_reaches_the_prompt(
    db_session: Session, ctf2_challenge, ctf2_config, ctf2_provider
) -> None:
    """★★ V3：`brief` 必须出现在**发给模型的第一条消息**里。

    这条守的是那个"静默丢弃"：schema 收了、router 没转交、service 没
    字段、prompt 没位置 —— 用户填了它什么都不会发生，而且没有反馈。
    """
    service.start_solve(db_session, ctf2_challenge, brief="考点在反调试，注意 ptrace")

    attempt = _attempt_for(db_session, ctf2_challenge.id)
    spec = runner.build_spec(db_session, ctf2_challenge, attempt_id=attempt.id)

    assert spec.brief == "考点在反调试，注意 ptrace"
    prompt = runner._initial_prompt(spec)
    assert "考点在反调试，注意 ptrace" in prompt, "★ brief 没有进 prompt"
    # 措辞上必须说明它是**线索**而非题面 —— 否则模型会把一条可能过时的
    # 提示当成必须成立的事实，在冲突时压过实测结果。
    assert "人工补充" in prompt
    assert "以实测为准" in prompt


def test_no_brief_means_no_prompt_section(
    db_session: Session, ctf2_challenge, ctf2_config, ctf2_provider
) -> None:
    """没填 brief 时 prompt 里**不该**出现那个区块（避免噪音）。"""
    service.start_solve(db_session, ctf2_challenge)
    attempt = _attempt_for(db_session, ctf2_challenge.id)
    spec = runner.build_spec(db_session, ctf2_challenge, attempt_id=attempt.id)

    prompt = runner._initial_prompt(spec)
    assert "人工补充的线索" not in prompt


# ══════════════════════════════════════════════════════════
# 四、兼容性：没有快照时退回旧行为（V5）
# ══════════════════════════════════════════════════════════


def test_v5_without_an_attempt_it_falls_back_to_global(
    db_session: Session, ctf2_challenge, ctf2_config, ctf2_provider
) -> None:
    """★ V5：没有快照时（测试直接调、历史任务）行为与修复前一致。

    `build_spec` 是公开函数，测试与脚本会直接调它。让它在没有快照时
    报错，会把"兼容旧调用"变成"所有旧调用都炸"。
    """
    _set_global(db_session, mode=KernelSubmitMode.AUTO.value, turns=33)

    spec = runner.build_spec(db_session, ctf2_challenge)

    assert spec.mode == KernelSubmitMode.AUTO.value
    assert spec.max_turns == 33
    assert spec.brief == ""


def test_missing_attempt_id_falls_back_but_warns(
    db_session: Session, ctf2_challenge, ctf2_config, ctf2_provider, caplog
) -> None:
    """★ 快照 id 传错/被删时**退回全局，但要出声**。

    "静默退回读全局"正是这次要消灭的那类问题 —— 如果它悄悄发生，
    我们又会回到"以为锁定了、其实没锁"的状态。
    """
    import logging

    with caplog.at_level(logging.WARNING, logger="suvex.ctf2"):
        spec = runner.build_spec(
            db_session, ctf2_challenge, attempt_id="ctf2try_NEVER_EXISTED"
        )

    assert spec.mode  # 仍然能构建出来（不炸）
    assert any("找不到解题策略快照" in r.message for r in caplog.records), (
        "★ 快照缺失时必须留一条 warning，不能静默退回"
    )


# ══════════════════════════════════════════════════════════
# 五、快照不可变：重解一次是**新增**一行，不是改旧行
# ══════════════════════════════════════════════════════════


def test_re_solving_appends_a_new_attempt(
    db_session: Session, ctf2_challenge, ctf2_config, ctf2_provider
) -> None:
    """★★ 第二次解题**新增**一行，第一次那行原样保留。

    这是"快照"与"把字段加到 challenge 上"的根本区别：后者会让
    "上一次用什么策略"被下一次覆盖 —— 而事后复盘"它当时是按什么
    策略跑的"正是最需要能回答的问题。
    """
    service.start_solve(db_session, ctf2_challenge, mode=KernelSubmitMode.SAFE.value)
    first = _attempt_for(db_session, ctf2_challenge.id)

    # 第一次跑完（或用户取消）之后，题目回到可解状态
    _reset_to_unsolved(db_session, ctf2_challenge)
    service.start_solve(db_session, ctf2_challenge, mode=KernelSubmitMode.AUTO.value)
    rows = (
        db_session.execute(
            select(KernelSolveAttempt)
            .where(KernelSolveAttempt.challenge_id == ctf2_challenge.id)
            .order_by(KernelSolveAttempt.created_at)
        )
        .scalars()
        .all()
    )

    assert len(rows) == 2, "重解应当新增一行快照"
    # 第一次那行**没有被改写**
    assert rows[0].id == first.id
    assert rows[0].mode == KernelSubmitMode.SAFE.value
    assert rows[1].mode == KernelSubmitMode.AUTO.value


# ══════════════════════════════════════════════════════════
# 六、约束：取值非法会被数据库挡下
# ══════════════════════════════════════════════════════════


def test_illegal_mode_is_rejected_by_the_database(
    db_session: Session, ctf2_challenge
) -> None:
    """★ CHECK 约束挡下拼错的 mode。

    这两列会进审计与复盘，写进一个拼错的字符串会让"这次到底按什么
    策略跑的"变成读不出来的东西。
    """
    import pytest
    from sqlalchemy.exc import IntegrityError

    db_session.add(
        KernelSolveAttempt(
            id=new_id("ctf2try"),
            challenge_id=ctf2_challenge.id,
            mode="safee",  # 拼错
            max_turns=80,
            sandbox_profile="ctf2-sandbox",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_zero_turns_is_rejected_by_the_database(
    db_session: Session, ctf2_challenge
) -> None:
    """★ 0 轮意味着"受理了但一轮都不跑" —— 那是配置错误，不是预算选择。"""
    import pytest
    from sqlalchemy.exc import IntegrityError

    db_session.add(
        KernelSolveAttempt(
            id=new_id("ctf2try"),
            challenge_id=ctf2_challenge.id,
            mode=KernelSubmitMode.SAFE.value,
            max_turns=0,
            sandbox_profile="ctf2-sandbox",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


# ══════════════════════════════════════════════════════════
# 换模 fallback 的配置校验（v0.35）
# ══════════════════════════════════════════════════════════


def _update_settings(db: Session, *, fallback_provider_id):  # noqa: ANN001
    """只改 fallback 一项，其余原样透传。"""
    row = service.get_ctf2_config(db)
    return service.update_ctf2_settings(
        db,
        submit_mode=row.submit_mode,
        max_wrong_submissions=row.max_wrong_submissions,
        default_sandbox_profile=row.default_sandbox_profile,
        max_concurrent_solves=row.max_concurrent_solves,
        solve_poll_seconds=row.solve_poll_seconds,
        auto_sync_seconds=row.auto_sync_seconds,
        auto_priority_new=row.auto_priority_new,
        auto_start_new=row.auto_start_new,
        max_turns=row.max_turns,
        fallback_provider_id=fallback_provider_id,
        # 两个场景包开关是必填关键字参数（v0.44）—— 原样透传。
        enable_agentsec_scenarios=row.enable_agentsec_scenarios,
        enable_ir_scenarios=row.enable_ir_scenarios,
        enable_multi_agent=row.enable_multi_agent,
    )


def test_fallback_provider_round_trips(db_session: Session, ctf2_provider: str) -> None:
    """配一个真实存在的 provider → 存下来、读回来一致。"""
    _update_settings(db_session, fallback_provider_id=ctf2_provider)
    assert service.effective_ctf2_settings(db_session).fallback_provider_id == ctf2_provider


def test_empty_string_clears_the_fallback(db_session: Session, ctf2_provider: str) -> None:
    """★ 空串归一成 `None`（= 不启用）—— 与"删掉这一项"是同一件事。

    前端下拉的"不启用"给的就是空串，它必须真的清掉，而不是存一个空串
    id（那会让 `if fid:` 判真，然后去查一个空 id 的 provider）。
    """
    _update_settings(db_session, fallback_provider_id=ctf2_provider)
    _update_settings(db_session, fallback_provider_id="")
    assert service.effective_ctf2_settings(db_session).fallback_provider_id is None


def test_unknown_fallback_provider_is_rejected(db_session: Session) -> None:
    """★★ 配一个**不存在**的 provider → 保存时就拒绝（不静默存下）。

    存一个不存在的 id 会让它到解题失败那一刻才暴露 —— 而那时正是
    最不该再出一个"配置指错了"的时刻。
    """
    import pytest

    from app.core.errors import ApiError

    with pytest.raises(ApiError) as exc:
        _update_settings(db_session, fallback_provider_id="prov_does_not_exist")
    assert exc.value.status_code == 400
