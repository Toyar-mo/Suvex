"""长任务持久化 + 纠偏判据（v1.19）。

用户原话：「对话又没反应的，没有长任务持久化啊」。

这个文件守两组互不重叠的东西：

1. **纠偏判据**（`app.chat.nudge`）—— 纯函数表驱动。原来靠中文意图词表，
   实测线上就漏了「先把二进制拆出来看防护和行为」这一句，于是整轮停在
   半句话上。现在主判据是结构信号（跑过工具 / 被截断 / 写了 FINAL）。
   这套判据的失败模式是**静默的**（界面卡住、没有报错），只能靠测试钉。
2. **持久化**（`app.chat.turn_runs` + `engine.run_turn_durable`）—— 一轮
   结束后库里必须有确定的终态；中断时必须留一条用户看得见的说明，并且
   状态要能被后台接手（`interrupted`，不是 `failed`）。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.chat import engine, resumer, service as chat_service, turn_runs
from app.chat import nudge as nudge_policy
from app.chat.models import ChatTurnRun
from app.core.timeutil import now_utc
from tests.test_chat import ADMIN


class ScriptedGateway:
    """按剧本回话，并且**每条**都能指定 `finish_reason`（截断要能造出来）。"""

    def __init__(self, replies: list[tuple[str, str]]) -> None:
        self.replies = list(replies)
        self.calls = 0

    async def chat(self, messages, *, system):  # noqa: ANN001, ANN201
        self.calls += 1
        text, finish = self.replies.pop(0) if self.replies else ("", "")
        return _Reply(text=text, finish_reason=finish)


class _Reply:
    def __init__(self, *, text: str, finish_reason: str = "") -> None:
        self.text = text
        self.finish_reason = finish_reason
        self.reasoning = None
        self.raw: dict[str, Any] = {}
        self.usage = None
        self.source = "content"


# ══════════════════════════════════════════════════════════
# 1. 纠偏判据（纯函数）
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("case", "kwargs", "expected"),
    [
        # 到上限 → 这句就是我们要的收尾，别推。
        ("at_limit", {"at_limit": True, "tools_used": 3, "text": "总结一下"}, False),
        # 写了 FINAL → 模型明确说完，别推。
        ("said_final", {"said_final": True, "tools_used": 3, "text": "结论"}, False),
        # 有动作 → 正常执行，别推。
        ("has_actions", {"actions": [("run_shell", {})], "tools_used": 1}, False),
        # 被切断 → 一定没说完。
        ("truncated", {"truncated": True, "text": "我把结果写在"}, True),
        # ★ 核心：这一轮真跑过工具，突然只写一句人话 = 没做完。
        ("tools_used", {"tools_used": 2, "text": "文件到了。先判型。"}, True),
        # 纯问答（零工具、无意图词）→ 不推，否则每次普通回答都多花一次调用。
        ("plain_answer", {"tools_used": 0, "text": "能，而且比你想的更可控。"}, False),
        # 零工具但写了"我先看一下" → 补充判据兜住。
        ("intent_word", {"tools_used": 0, "text": "我先看一下环境。"}, True),
        # 额度用尽 → 不推（否则死循环）。
        # ★ v1.23：纯问答（零工具）的 2 次额度用尽 → 不推（成本闸）。
        ("no_budget", {"tools_used": 0, "text": "我先看看。", "nudges_used": 2}, False),
        # ★★ v1.23 核心：**跑过工具的轮次不受那 2 次额度限制**。
        #   线上会话 chat_01M61DWC71W8989RRMW6DZS9SP 就是 nudges_used=2 /
        #   tools_used=2，旧逻辑在这里 return False，界面停在
        #   「Let me see the rest of the main function.」。
        (
            "tool_round_ignores_narration_budget",
            {"tools_used": 2, "text": "Let me see the rest.", "nudges_used": 2},
            True,
        ),
        # ★ v1.25：完整回答**仍然推一次**（安全网不松），但引擎会把被推掉的
        #   那句降级成 `process`，所以界面上不会出现两条一样的结论气泡。
        #   见 `test_a_nudged_answer_is_demoted_to_process`。
        (
            "complete_answer_after_tools_still_nudges",
            {
                "tools_used": 2,
                "text": "当前目录是 /work（沙箱的工作目录，可写；pwd 返回 /work）。"
                "之前那次 ls -la /work 你也看到了只有 attachments/ 和 persist/。",
            },
            True,
        ),
        # 但工具纠偏自身也有额度（否则确定性失败会无限续）。
        (
            "tool_round_budget_used_up",
            {"tools_used": 2, "text": "再看一下。", "tool_nudges_used": 8},
            False,
        ),
        # ★ v1.21：截断有**独立额度** —— 格式额度用完了，被切断的仍要接着写。
        #   这一条就是线上「解一半又停下」的判据：`nudges_used` 已经 2，
        #   但其中 0 次是截断，所以截断额度还有 6 次可用。
        (
            "truncated_after_format_budget",
            {"truncated": True, "text": "第二次 read 则", "nudges_used": 2},
            True,
        ),
        # 截断额度本身用尽 → 停（否则一条超长回复可以无限续下去）。
        (
            "truncation_budget_exhausted",
            {"truncated": True, "text": "半句", "nudges_used": 8, "truncations_used": 6},
            False,
        ),
        # 改了截断额度**不能**顺手把纯问答额度也放开：它仍然是 2。
        # （v1.23 起这条必须用 tools_used=0 —— 跑过工具的轮次已改为独立额度，
        #   见上面的 tool_round_ignores_narration_budget。）
        (
            "format_budget_still_two",
            {"text": "我先看看。", "tools_used": 0, "nudges_used": 2},
            False,
        ),
    ],
)
def test_should_nudge_matrix(case: str, kwargs: dict, expected: bool) -> None:
    base = {
        "at_limit": False,
        "said_final": False,
        "actions": [],
        "truncated": False,
        "tools_used": 0,
        "text": "",
        "nudges_used": 0,
        "max_nudges": 2,
        "tool_nudges_used": 0,
        "max_tool_nudges": 8,
    }
    base.update(kwargs)
    assert nudge_policy.should_nudge(**base) is expected, case


def test_long_complete_answer_is_not_treated_as_unfinished() -> None:
    """600 字的完整结论里顺带一句「我先看了一下」不算停在半路。"""
    body = "我先看了一下环境。" + "结果如下。" * 100
    assert nudge_policy.looks_unfinished(body) is False


def test_split_final_strips_both_colon_widths() -> None:
    assert nudge_policy.split_final("FINAL: 好了") == ("好了", True)
    assert nudge_policy.split_final("FINAL：好了") == ("好了", True)
    assert nudge_policy.split_final("没有终止符") == ("没有终止符", False)


def test_split_final_must_be_at_the_very_start() -> None:
    """正文中间出现 FINAL 不算终止符（否则会截断真实内容）。"""
    text = "先看这个。FINAL: 这不是终止符"
    assert nudge_policy.split_final(text) == (text, False)


# ══════════════════════════════════════════════════════════
# 2. 一轮的生命周期记账
# ══════════════════════════════════════════════════════════


def _new_session(db: Session) -> Any:
    session = chat_service.create_session(db, created_by="user_admin")
    db.commit()
    return session


def _make_stale(db: Session, run: Any) -> None:
    """把心跳拨到过期，模拟 `stale_runs()` 捞出来的那一行。"""
    run.heartbeat_at = now_utc() - timedelta(seconds=turn_runs.STALE_SECONDS + 1)
    db.commit()


def test_start_creates_running_row(db_session: Session) -> None:
    session = _new_session(db_session)
    run = turn_runs.start(db_session, session_id=session.id)
    db_session.commit()
    assert run.status == "running"
    assert run.resume_attempts == 0
    assert run.heartbeat_at is not None


def test_finish_rejects_unknown_status(db_session: Session) -> None:
    session = _new_session(db_session)
    run = turn_runs.start(db_session, session_id=session.id)
    with pytest.raises(ValueError):
        turn_runs.finish(db_session, run, status="interrupted")


def test_interrupted_run_is_resumable_only_after_heartbeat_goes_stale(
    db_session: Session,
) -> None:
    session = _new_session(db_session)
    run = turn_runs.start(db_session, session_id=session.id)
    db_session.commit()

    # 刚写过心跳：不算死。
    assert turn_runs.is_resumable(run, now=now_utc()) is False
    # 心跳超过阈值：该接手了。
    later = now_utc() + timedelta(seconds=turn_runs.STALE_SECONDS + 1)
    assert turn_runs.is_resumable(run, now=later) is True


def test_giving_up_after_max_attempts(db_session: Session) -> None:
    session = _new_session(db_session)
    run = turn_runs.start(db_session, session_id=session.id)
    run.resume_attempts = turn_runs.MAX_RESUME_ATTEMPTS
    db_session.commit()
    later = now_utc() + timedelta(seconds=turn_runs.STALE_SECONDS + 1)
    assert turn_runs.is_resumable(run, now=later) is False


def test_close_stale_on_startup_marks_running_as_interrupted(
    db_session: Session,
) -> None:
    session = _new_session(db_session)
    run = turn_runs.start(db_session, session_id=session.id)
    db_session.commit()

    changed = turn_runs.close_stale_on_startup(db_session)
    assert changed >= 1
    db_session.refresh(run)
    assert run.status == "interrupted"


def test_claim_bumps_attempts_and_keeps_it_running(db_session: Session) -> None:
    """抢到之后状态必须是 running、额度 +1（并发去重由 SQL 的 rowcount 保证）。"""
    session = _new_session(db_session)
    run = turn_runs.start(db_session, session_id=session.id)
    db_session.commit()
    now = now_utc() + timedelta(seconds=turn_runs.STALE_SECONDS + 1)

    assert turn_runs.claim(db_session, run, now=now) is True
    assert run.resume_attempts == 1
    assert run.status == "running"


def test_claim_refuses_a_fresh_heartbeat(db_session: Session) -> None:
    """心跳还新的一轮不能被抢：条件 UPDATE 必须自己再看一次心跳。"""
    session = _new_session(db_session)
    run = turn_runs.start(db_session, session_id=session.id)
    db_session.commit()

    assert turn_runs.claim(db_session, run, now=now_utc()) is False


def test_claim_refuses_a_turn_active_in_this_process(db_session: Session) -> None:
    """慢工具期间心跳会显得过期，但本进程里还在跑的一轮不能被抢。"""
    session = _new_session(db_session)
    run = turn_runs.start(db_session, session_id=session.id)
    db_session.commit()
    later = now_utc() + timedelta(seconds=turn_runs.STALE_SECONDS + 1)

    turn_runs.mark_active(run.id)
    try:
        assert turn_runs.claim(db_session, run, now=later) is False
        assert turn_runs.is_resumable(run, now=later) is False
        assert all(
            row.id != run.id
            for row in turn_runs.stale_runs(db_session, now=later)
        )
    finally:
        turn_runs.unmark_active(run.id)

    assert turn_runs.claim(db_session, run, now=later) is True


def test_hard_stale_reclaims_a_zombie_turn_active_in_this_process(
    db_session: Session,
) -> None:
    """★ v1.67：卡死僵尸即便挂着 `_ACTIVE` 租约，心跳硬过期后也必须能被回收。

    复现线上事故：一轮卡在模型流上（上游只发 keepalive），心跳冻了 49 分钟，
    协程一直占着 `_ACTIVE`，旧的 `STALE_SECONDS=90` 安全网因 `is_active()` 恒
    True 而永远跳过它 —— 自己把自己锁死。硬过期后必须放行。
    """
    session = _new_session(db_session)
    run = turn_runs.start(db_session, session_id=session.id)
    db_session.commit()
    # 心跳拨到"硬过期"之外（远大于 HARD_STALE_SECONDS）
    run.heartbeat_at = now_utc() - timedelta(
        seconds=turn_runs.HARD_STALE_SECONDS + 30
    )
    db_session.commit()
    later = now_utc()

    turn_runs.mark_active(run.id)
    try:
        # 硬过期 → 即便 active 也应被视为可接手
        assert turn_runs.is_resumable(run, now=later) is True
        assert any(
            r.id == run.id for r in turn_runs.stale_runs(db_session, now=later)
        )
        assert turn_runs.claim(db_session, run, now=later) is True
    finally:
        turn_runs.unmark_active(run.id)


def test_hard_stale_threshold_refuses_reclaim_just_below_it(
    db_session: Session,
) -> None:
    """★ v1.67：心跳只超过 STALE 但没到 HARD 的活跃轮次**仍**不能抢。

    守住"慢工具期间不误抢"这条老红线（v1.33.1）：硬过期阈值必须真比慢工具
    长。让心跳落在 `STALE_SECONDS+1 .. HARD_STALE_SECONDS` 之间，活跃时仍拒绝。
    """
    session = _new_session(db_session)
    run = turn_runs.start(db_session, session_id=session.id)
    db_session.commit()
    run.heartbeat_at = now_utc() - timedelta(seconds=turn_runs.STALE_SECONDS + 1)
    db_session.commit()
    later = now_utc()

    turn_runs.mark_active(run.id)
    try:
        assert turn_runs.is_resumable(run, now=later) is False
        assert all(
            r.id != run.id
            for r in turn_runs.stale_runs(db_session, now=later)
        )
        assert turn_runs.claim(db_session, run, now=later) is False
    finally:
        turn_runs.unmark_active(run.id)


async def test_resume_one_marks_the_run_active_while_it_runs(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """resumer 自己接手的那一轮也必须登记活跃租约（v1.33.1）。"""
    session = _new_session(db_session)
    run = turn_runs.start(db_session, session_id=session.id)
    db_session.commit()
    _make_stale(db_session, run)

    assert run.session_id == session.id
    assert session.archived is False
    # 这个用例只验证活跃租约；身份解析由线上账号表负责，测试库没有
    # `user_admin` 这一行，直接给一个管理员身份即可。
    monkeypatch.setattr(resumer, "_identity_for", lambda db, s: ADMIN)

    seen: list[bool] = []

    async def fake_run_turn(db: Session, **kwargs: Any) -> Any:
        seen.append(turn_runs.is_active(run.id))
        return engine.ChatTurnResult(messages=[])

    monkeypatch.setattr(engine, "run_turn", fake_run_turn)
    claimed: list[bool] = []
    real_claim = turn_runs.claim

    def spy_claim(*args: Any, **kwargs: Any) -> bool:
        out = real_claim(*args, **kwargs)
        claimed.append(out)
        return out

    monkeypatch.setattr(turn_runs, "claim", spy_claim)

    ran = await resumer.resume_one(db_session, run)
    assert claimed == [True]
    assert ran is True
    assert seen == [True]
    assert turn_runs.is_active(run.id) is False
    db_session.refresh(run)
    assert run.status == "done"


async def test_resume_one_keeps_the_turn_incomplete_when_the_model_stops_on_a_plan(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """后台接手再停在计划句上，也不能记成 done（v1.35）。

    resumer 走的是 `run_turn` 本身，不经过 `_run_guarded_active`；
    v1.35 新增的『停在计划句 → interrupted』如果不在这里重复一次，
    被接手的一轮会重新落回 `done`，用户看到的仍然是「没解出又结束」。
    """
    session = _new_session(db_session)
    run = turn_runs.start(db_session, session_id=session.id)
    db_session.commit()
    _make_stale(db_session, run)
    monkeypatch.setattr(resumer, "_identity_for", lambda db, s: ADMIN)

    async def still_planning(db: Session, **kwargs: Any) -> Any:
        return engine.ChatTurnResult(messages=[], needs_continue=True)

    monkeypatch.setattr(engine, "run_turn", still_planning)

    ran = await resumer.resume_one(db_session, run)
    assert ran is True
    db_session.refresh(run)
    assert run.status == "interrupted", "还没给出结论的轮次不能记成 done"
    assert run.last_error == turn_runs.NEEDS_CONTINUE_REASON


async def test_resume_one_keeps_a_user_stop_cancelled(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """后台接手期间用户停掉的一轮落 cancelled，与前台路径一致。"""
    session = _new_session(db_session)
    run = turn_runs.start(db_session, session_id=session.id)
    db_session.commit()
    _make_stale(db_session, run)
    monkeypatch.setattr(resumer, "_identity_for", lambda db, s: ADMIN)

    async def stopped(db: Session, **kwargs: Any) -> Any:
        return engine.ChatTurnResult(messages=[], stopped=True)

    monkeypatch.setattr(engine, "run_turn", stopped)

    ran = await resumer.resume_one(db_session, run)
    assert ran is True
    db_session.refresh(run)
    assert run.status == "cancelled"


async def test_resume_one_releases_the_lease_when_run_turn_raises(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """异常路径也必须释放租约，否则这一轮在本进程里永远不可接手。"""
    session = _new_session(db_session)
    run = turn_runs.start(db_session, session_id=session.id)
    db_session.commit()
    _make_stale(db_session, run)
    monkeypatch.setattr(resumer, "_identity_for", lambda db, s: ADMIN)

    async def boom(db: Session, **kwargs: Any) -> Any:
        assert turn_runs.is_active(run.id) is True
        raise RuntimeError("gateway exploded")

    monkeypatch.setattr(engine, "run_turn", boom)

    ran = await resumer.resume_one(db_session, run)
    assert ran is False
    assert turn_runs.is_active(run.id) is False
    db_session.refresh(run)
    assert run.status == "interrupted"
    assert "gateway exploded" in (run.last_error or "")


def test_collapse_demotes_answers_from_another_executor(
    db_session: Session,
) -> None:
    """后台接手另起执行体时，后完成者也要把对方那条 answer 降级。"""
    session = _new_session(db_session)
    chat_service.append_user_message(db_session, session=session, content="继续")
    first = chat_service.append_assistant_message(
        db_session, session=session, content="旧结论", surface="answer"
    )
    second = chat_service.append_assistant_message(
        db_session, session=session, content="新结论", surface="answer"
    )
    db_session.commit()

    # 模拟接手方只拿到自己这条消息的 result。
    engine._collapse_turn_answers(
        db_session, engine.ChatTurnResult(messages=[second])
    )

    db_session.refresh(first)
    db_session.refresh(second)
    assert first.surface == "process"
    assert second.surface == "answer"


def test_collapse_does_not_touch_the_previous_turn_answer(
    db_session: Session,
) -> None:
    """折叠以最后一条用户消息为界，不能把上一轮的结论也降级。"""
    session = _new_session(db_session)
    chat_service.append_user_message(db_session, session=session, content="第一问")
    previous = chat_service.append_assistant_message(
        db_session, session=session, content="第一答", surface="answer"
    )
    chat_service.append_user_message(db_session, session=session, content="第二问")
    middle = chat_service.append_assistant_message(
        db_session, session=session, content="中间结论", surface="answer"
    )
    last = chat_service.append_assistant_message(
        db_session, session=session, content="最终结论", surface="answer"
    )
    db_session.commit()

    engine._collapse_turn_answers(
        db_session, engine.ChatTurnResult(messages=[last])
    )

    for message in (previous, middle, last):
        db_session.refresh(message)
    assert previous.surface == "answer"
    assert middle.surface == "process"
    assert last.surface == "answer"


def test_finish_from_error_keeps_it_resumable_not_failed(db_session: Session) -> None:
    """中断 ≠ 判死：必须留 `interrupted`，否则"自动接着做"就永远不触发。"""
    session = _new_session(db_session)
    run = turn_runs.start(db_session, session_id=session.id)
    db_session.commit()

    turn_runs.finish_from_error(db_session, run, reason="CancelledError: 客户端断开")
    db_session.commit()
    assert run.status == "interrupted"
    assert "客户端断开" in (run.last_error or "")


# ══════════════════════════════════════════════════════════
# 3. 引擎：纠偏真的会继续跑；纯问答真的不会多花调用
# ══════════════════════════════════════════════════════════


async def test_narration_after_a_tool_call_does_not_end_the_turn(
    db_session: Session,
) -> None:
    """跑过工具后只写一句打算 → 推一次，整轮继续（用户看到的正是"没反应"那条）。"""
    session = _new_session(db_session)
    gateway = ScriptedGateway(
        [
            ('ACTION: {"tool": "list_scans", "args": {}}', ""),
            # 实测线上漏掉的那一句（中文词表抓不到）。
            ("先把二进制拆出来看防护和行为，同时把服务交互读完。", ""),
            ("FINAL: 全部做完了。", ""),
        ]
    )
    result = await engine.run_turn(
        db_session,
        session=session,
        content="解一下这道题",
        identity=ADMIN,
        gateway=gateway,
    )
    assert gateway.calls == 3, "第二轮只写叙述时必须再问一次，而不是结束"
    assert result.messages[-1].content.strip() == "全部做完了。"


async def test_tool_rounds_alone_are_enough_to_nudge(db_session: Session) -> None:
    """跑过工具之后，哪怕那句叙述里**一个"要动手"的词都没有**，也要推一次。

    ★ 这条专门隔离**结构信号**（`tools_used`）：上一版靠中文意图词表，
      而「输出都在上面了。」这种句子词表抓不到 —— 它却是真实存在的形态
      （模型把结果贴完就停了）。只有 `tools_used > 0` 能把它救回来。

    如果哪天有人把 `nudge.should_nudge` 里的 `tools_used` 判据删掉，这里
    会变成 2 次调用而红 —— 这是本文件唯一能抓住那个回退的用例。
    """
    session = _new_session(db_session)
    gateway = ScriptedGateway(
        [
            ('ACTION: {"tool": "list_scans", "args": {}}', ""),
            ("输出都在上面了。", ""),
            ("FINAL: 结论是这样。", ""),
        ]
    )
    await engine.run_turn(
        db_session,
        session=session,
        content="查一下扫描",
        identity=ADMIN,
        gateway=gateway,
    )
    assert gateway.calls == 3, "跑过工具后停住，必须推一次再收尾"


async def test_plan_like_final_with_bare_verb_is_bounced(
    db_session: Session,
) -> None:
    """线上原句的收尾形态（v1.29.1）：句首直接是动词的计划，不能当结论。

    `chat_01M63WHGQJHAJ684E5YTBQC0QX` 最后一轮的原文是：

        FINAL: stripped、动态链接、只有 read/puts/setbuf，两次输入。看反汇编确认溢出点。

    v1.29 的判据要求「先/现在/接下来…」+ 动词紧邻，这句话两个正则都不命中，
    于是 `premature_final=False` → 落成 `answer` 气泡 → 整轮 `done`。
    用户看到的就是「输出一半就停了」。

    断言三件事，缺一不可：
    * 被打回一次（`gateway.calls == 3`）—— 否则没有任何继续
    * 被打回的那条落成 `process`（折叠进过程详情，不当结论闪一下）
    * 最终结论是第三条，不是那句计划
    """
    session = _new_session(db_session)
    plan = "stripped、动态链接、只有 read/puts/setbuf，两次输入。看反汇编确认溢出点。"
    gateway = ScriptedGateway(
        [
            ('ACTION: {"tool": "list_scans", "args": {}}', ""),
            (f"FINAL: {plan}", ""),
            ("FINAL: 溢出点在 0x4006a0，偏移 0x28。", ""),
        ]
    )
    result = await engine.run_turn(
        db_session,
        session=session,
        content="解一下这道题",
        identity=ADMIN,
        gateway=gateway,
    )
    assert gateway.calls == 3, "计划式的 FINAL 必须先被打回一次，而不是直接收尾"
    bounced = [m for m in result.messages if m.content.strip() == plan]
    assert bounced, "被打回的原句仍要留在过程面里，用户能回看"
    assert bounced[0].surface == "process", "被打回的计划不是结论，不能画成答复气泡"
    assert result.messages[-1].content.strip() == "溢出点在 0x4006a0，偏移 0x28。"


async def test_plan_after_a_comma_is_bounced(
    db_session: Session,
) -> None:
    """线上原句（`chat_01M64ANA9DQVW1FGWMPEZY6NZD` 第 9 条）：

        FINAL: 主逻辑在 0x40070b，看它。

    前半句是事实，逗号后面才是动作。旧判据只认句号/换行后的裸动词，
    于是这句话被当成结论落成 `answer`，整轮 `done`。
    """
    session = _new_session(db_session)
    plan = "主逻辑在 0x40070b，看它。"
    gateway = ScriptedGateway(
        [
            ('ACTION: {"tool": "list_scans", "args": {}}', ""),
            (f"FINAL: {plan}", ""),
            ("FINAL: 溢出点在 0x4006a0，偏移 0x28。", ""),
        ]
    )
    result = await engine.run_turn(
        db_session,
        session=session,
        content="继续，解出flag再停",
        identity=ADMIN,
        gateway=gateway,
    )
    assert gateway.calls == 3, "逗号后的计划句也必须被打回一次"
    bounced = [m for m in result.messages if m.content.strip() == plan]
    assert bounced and bounced[0].surface == "process"
    assert result.messages[-1].content.strip() == "溢出点在 0x4006a0，偏移 0x28。"


async def test_repeated_plan_finals_never_land_as_an_answer(
    db_session: Session,
) -> None:
    """连着多次计划句，后面的也不能当结论（v1.30）。

    线上会话 `chat_01M640EY2P54QC1E2QS54R0R3Z`：seq 8「checksec 没输出，我用
    readelf 和 pwn 自己看防护」先被打回一次；seq 10「……典型栈溢出。看主逻辑。」
    同样是计划，但 `final_confirmations` 已经到 1，闸门直接放行 —— 落成
    `answer`、整轮 `done`。用户看到的还是「解题到一半就结束了」。

    这里让模型连续写计划句：前 `MAX_PLAN_BOUNCES` 次必须继续推，额度用尽
    之后也不能把计划画成结论 —— 要补一句"这是计划，不是结果"。
    """
    session = _new_session(db_session)
    plan = "没有 PIE、没有 canary，NX 开、Partial RELRO —— 典型栈溢出。看主逻辑。"
    replies = [('ACTION: {"tool": "list_scans", "args": {}}', "")]
    replies += [(f"FINAL: {plan}", "")] * (engine.MAX_PLAN_BOUNCES + 2)
    gateway = ScriptedGateway(replies)
    result = await engine.run_turn(
        db_session,
        session=session,
        content="解一下这道题",
        identity=ADMIN,
        gateway=gateway,
    )
    assert gateway.calls == 1 + engine.MAX_PLAN_BOUNCES + 1, (
        "额度内必须一直推（每推一次多一次调用），用完后停下来补实话"
    )
    plan_messages = [m for m in result.messages if m.content.strip() == plan]
    assert plan_messages, "被打回的计划句要留在过程面里"
    assert all(m.surface == "process" for m in plan_messages), (
        "计划句永远不是结论，哪怕打回额度已经用完"
    )
    assert "继续" in result.messages[-1].content, (
        "额度用尽时必须告诉用户下一步还没做，而不是静默收尾"
    )


async def test_plan_prose_without_final_is_bounced(
    db_session: Session,
) -> None:
    """线上复跑原句（`chat_01M652CRXMJ4H9P1XABAJ4VGSH` 第 12 条）：

        保护很弱：没有 PIE，也没有 canary。接下来查看 main、read_input 等函数。

    模型**没写** `FINAL:`，旧判据要求 `said_final` 才当计划，于是这句以
    `answer` 落库；接着两次空回复吃光纠偏额度，整轮 `done` —— 用户看到
    「输出一半就停了」。计划句的判据应该只看句子本身。
    """
    session = _new_session(db_session)
    plan = "保护很弱：没有 PIE，也没有 canary。接下来查看 main、read_input 等函数。"
    gateway = ScriptedGateway(
        [
            ('ACTION: {"tool": "list_scans", "args": {}}', ""),
            (plan, ""),
            ('ACTION: {"tool": "list_scans", "args": {}}', ""),
            ("FINAL: 溢出点在 0x4006a0，偏移 0x28。", ""),
        ]
    )
    result = await engine.run_turn(
        db_session,
        session=session,
        content="解一下这道题",
        identity=ADMIN,
        gateway=gateway,
    )
    assert gateway.calls == 4, "没写 FINAL 的计划句也必须被打回继续干"
    bounced = [m for m in result.messages if m.content.strip() == plan]
    assert bounced and bounced[0].surface == "process", "计划句不能落成结论气泡"
    assert result.messages[-1].content.strip() == "溢出点在 0x4006a0，偏移 0x28。"


async def test_plan_after_a_result_marker_is_bounced(
    db_session: Session,
) -> None:
    """v1.34：完成标记之后还挂着「接下来」时，不能当结论结束。

    reviewer-4e5f63 的引擎级复现：第二轮写

        FINAL: 反汇编如下：main 在 0x40070b。接下来写 exploit。

    旧判据只看前半句的「反汇编如下」就直接放过，surface=answer、
    gateway.calls=2，整轮 done。正确行为是打回一次，等真正的结论。
    """
    session = _new_session(db_session)
    plan = "FINAL: 反汇编如下：main 在 0x40070b。接下来写 exploit。"
    gateway = ScriptedGateway(
        [
            ('ACTION: {"tool": "list_scans", "args": {}}', ""),
            (plan, ""),
            ("FINAL: exploit 跑通，flag 是 flag{ok}。", ""),
        ]
    )
    result = await engine.run_turn(
        db_session,
        session=session,
        content="解一下这道题",
        identity=ADMIN,
        gateway=gateway,
    )
    assert gateway.calls == 3, "结尾仍是计划时必须打回一次，而不是直接收尾"
    bounced = [m for m in result.messages if m.content.strip() == plan.removeprefix("FINAL: ")]
    assert bounced and bounced[0].surface == "process", "计划句不能落成结论气泡"
    assert result.messages[-1].content.strip() == "exploit 跑通，flag 是 flag{ok}。"


async def test_empty_replies_after_a_plan_use_the_tool_budget(
    db_session: Session,
) -> None:
    """跑过工具的轮次，空回复不能吃光那 2 次叙事额度（v1.31）。

    线上复跑：计划句之后模型连回两次空，`nudges_used` 当场到 2，空回复分支
    看到「已经说过话」就直接 `return` —— 界面停在半句计划上。跑过工具的
    轮次应该跟 `should_nudge` 用同一个更大的工具额度。
    """
    session = _new_session(db_session)
    gateway = ScriptedGateway(
        [
            ('ACTION: {"tool": "list_scans", "args": {}}', ""),
            ("保护很弱：没有 PIE。接下来查看 main。", ""),
            ("", ""),
            ("", ""),
            ("FINAL: flag{live_replay}", ""),
        ]
    )
    result = await engine.run_turn(
        db_session,
        session=session,
        content="解一下这道题",
        identity=ADMIN,
        gateway=gateway,
    )
    assert gateway.calls == 5, "两次空回复之后仍然要再推一次，而不是就此收尾"
    assert result.messages[-1].content.strip() == "flag{live_replay}"
    assert not any("没有产出内容" in m.content for m in result.messages)


async def test_narration_nudges_do_not_eat_the_empty_reply_budget(
    db_session: Session,
) -> None:
    """叙述纠偏不能把空回复的额度一起吃掉（v1.32）。

    线上复跑（log `/home/kali/replay3.log`，会话 `chat_01M652CRXMJ4H9P1XABAJ4VGSH`）：
    round 0 ～ 12 里有 6 次 `resume/format` 纠偏，每一次都 `nudges_used += 1`；
    轮到 round 15 的计划句被打回、下一个回复是空的时候，
    `(nudges_used - truncations_used) < MAX_EMPTY_NUDGES` 已经是 `6 < 6`，
    当场判「额度用完」，于是又停在半句计划上（只是这次多了一句说明）。

    两类纠偏的花费该分开记账：叙述纠偏是「它写了但没动手」，
    空回复是「它一个字没写」——后者才是需要单独兜底的故障。
    """
    session = _new_session(db_session)
    narration = "这是一个 64 位 ELF，保护很弱。"
    plan = "接下来查看 main、read_input 等函数。"
    replies = [('ACTION: {"tool": "list_scans", "args": {}}', "")]
    # ★ v1.64.1：叙述轮数从 6 降到 3 —— 必须**小于** `MAX_NO_TOOL_NUDGES`(5)。
    #   新加的"连续 NO_TOOL 轮不再推"闸会在第 5 轮没动手时收尾；本测试要验的
    #   是"叙述纠偏不吃空回复额度"这件事，3 轮已足够体现分离，且不会被那道闸
    #   提前截断。（真实任务里叙述后紧跟工具调用会清零计数，不会累积到 5。）
    replies += [(narration, "")] * 3
    replies += [(plan, ""), ("", ""), ("FINAL: flag{live_replay}", "")]
    gateway = ScriptedGateway(replies)
    result = await engine.run_turn(
        db_session,
        session=session,
        content="解一下这道题",
        identity=ADMIN,
        gateway=gateway,
    )
    assert gateway.calls == 7, "前面 3 次叙述纠偏之后，空回复仍然要再推一次"
    assert result.messages[-1].content.strip() == "flag{live_replay}"


async def test_plan_gate_note_when_empty_replies_exhaust_everything(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """连空回复额度也烧完时，不能把半句计划当结论交出去（v1.31）。"""
    from app.chat.prompts import render_plan_exhausted_note

    monkeypatch.setattr(engine, "MAX_EMPTY_NUDGES", 1)
    session = _new_session(db_session)
    plan = "接下来查看 main、read_input 等函数。"
    replies = [('ACTION: {"tool": "list_scans", "args": {}}', ""), (plan, "")]
    replies += [("", "")] * 3
    gateway = ScriptedGateway(replies)
    result = await engine.run_turn(
        db_session,
        session=session,
        content="解一下这道题",
        identity=ADMIN,
        gateway=gateway,
    )
    assert result.messages[-1].content == render_plan_exhausted_note(), (
        "预算全烧完时也要告诉用户「上面那句是计划，不是结果」"
    )
    plan_messages = [m for m in result.messages if m.content.strip() == plan]
    assert plan_messages and all(m.surface == "process" for m in plan_messages)


async def test_plain_answer_ends_without_an_extra_model_call(
    db_session: Session,
) -> None:
    """整轮零工具的普通回答不写 FINAL 也不该被推（这是成本底线）。"""
    session = _new_session(db_session)
    gateway = ScriptedGateway([("能，而且比你想的更可控。", "")])
    await engine.run_turn(
        db_session,
        session=session,
        content="你能做什么",
        identity=ADMIN,
        gateway=gateway,
    )
    assert gateway.calls == 1


async def test_truncated_reply_is_nudged_even_with_no_tools(
    db_session: Session,
) -> None:
    session = _new_session(db_session)
    gateway = ScriptedGateway(
        [
            ("我把关键证据写在这里：flag{a", "length"),
            ("FINAL: flag{abc}", ""),
        ]
    )
    await engine.run_turn(
        db_session,
        session=session,
        content="解题",
        identity=ADMIN,
        gateway=gateway,
    )
    assert gateway.calls == 2


async def test_nudges_are_bounded(db_session: Session) -> None:
    """模型一直只写打算时，必须有限收尾，不能死循环。

    v1.35 起「先看看。」是计划句：零工具轮次走叙述纠偏额度
    （`MAX_NARRATION_NUDGES`），额度用完后不再把它当答复，而是补一句
    `render_plan_exhausted_note()` 并记下"还没答完"。这里钉的是
    "不超过额度 + 一次收尾"。
    """
    session = _new_session(db_session)
    gateway = ScriptedGateway(
        [("先看看。", "")] * (engine.MAX_NARRATION_NUDGES + 5)
    )
    result = await engine.run_turn(
        db_session,
        session=session,
        content="解题",
        identity=ADMIN,
        gateway=gateway,
    )
    assert gateway.calls <= 1 + engine.MAX_NARRATION_NUDGES
    assert result.needs_continue is True


# ══════════════════════════════════════════════════════════
# 3b. 计划句不能落 `done`（v1.35）
# ══════════════════════════════════════════════════════════


async def test_zero_tool_plan_does_not_finish_the_turn(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """零工具轮次的计划句也不能整轮 `done`（v1.35 的漏洞类别 1）。

    旧判据要求 `tools_used > 0` 才把 FINAL 当计划 —— 纯问答轮次里模型写
    「接下来分析 main 函数，然后构造 exploit。」会被当成 `answer`，整轮
    `done`，用户看到的就是「话停在计划上」。

    v1.35 的通道选择：零工具 = "说要去做却没写 ACTION"，走纠偏通道
    （`format`，额度 `MAX_NARRATION_NUDGES`）；额度用完后仍不落 `done`。
    """
    monkeypatch.setattr(engine, "MAX_NARRATION_NUDGES", 1)
    session = _new_session(db_session)
    plan = "接下来分析 main 函数，然后构造 exploit。"
    gateway = ScriptedGateway([(plan, ""), (plan, "")])
    result = await engine.run_turn_durable(
        db_session,
        session=session,
        content="解一下这道题",
        identity=ADMIN,
        gateway=gateway,
    )
    assert gateway.calls == 2, "零工具的计划句必须先被打回一次"
    assert result.needs_continue is True
    rows = chat_service.list_messages(db_session, session=session)
    plan_rows = [r for r in rows if r.content.strip() == plan]
    assert plan_rows and all(r.surface == "process" for r in plan_rows), (
        "计划句永远是过程面，不能画成答复气泡"
    )
    assert rows[-1].content == engine.render_plan_exhausted_note()
    run = db_session.query(ChatTurnRun).filter_by(session_id=session.id).one()
    assert run.status == "interrupted", "还没给出结论的轮次不能记成 done"


async def test_plan_after_tools_with_exhausted_bounces_is_not_done(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """跑过工具后额度用尽的计划句：最后一条可见消息是「这是计划」且不落 done。"""
    monkeypatch.setattr(engine, "MAX_PLAN_BOUNCES", 1)
    session = _new_session(db_session)
    plan = "接下来分析 main 函数，然后构造 exploit。"
    gateway = ScriptedGateway(
        [
            ('ACTION: {"tool": "list_scans", "args": {}}', ""),
            (plan, ""),
            (plan, ""),
        ]
    )
    result = await engine.run_turn_durable(
        db_session,
        session=session,
        content="解一下这道题",
        identity=ADMIN,
        gateway=gateway,
    )
    assert result.needs_continue is True
    rows = chat_service.list_messages(db_session, session=session)
    assert rows[-1].content == engine.render_plan_exhausted_note(), (
        "额度用尽时必须把「这是计划，不是结果」作为最后一条可见消息"
    )
    run = db_session.query(ChatTurnRun).filter_by(session_id=session.id).one()
    assert run.status != "done"
    assert run.status == "interrupted"


async def test_plain_qa_still_finishes_done(db_session: Session) -> None:
    """纯问答（无 ACTION、无计划词）仍然是一句 `answer` + `done`。"""
    session = _new_session(db_session)
    gateway = ScriptedGateway([("能，而且比你想的更可控。", "")])
    result = await engine.run_turn_durable(
        db_session,
        session=session,
        content="你能做什么",
        identity=ADMIN,
        gateway=gateway,
    )
    assert gateway.calls == 1, "普通回答不该被误打回（成本底线）"
    assert result.needs_continue is False
    rows = chat_service.list_messages(db_session, session=session)
    assert rows[-1].content.strip() == "能，而且比你想的更可控。"
    assert rows[-1].surface == "answer"
    run = db_session.query(ChatTurnRun).filter_by(session_id=session.id).one()
    assert run.status == "done"


# ══════════════════════════════════════════════════════════
# 4. 持久化外壳：成功落 done；中断落 interrupted + 留一句人话
# ══════════════════════════════════════════════════════════


async def test_durable_turn_finishes_done(db_session: Session) -> None:
    session = _new_session(db_session)
    gateway = ScriptedGateway([("FINAL: 好。", "")])
    await engine.run_turn_durable(
        db_session,
        session=session,
        content="你好",
        identity=ADMIN,
        gateway=gateway,
    )
    runs = db_session.query(ChatTurnRun).filter_by(session_id=session.id).all()
    assert [r.status for r in runs] == ["done"]


async def test_durable_turn_records_interruption_and_leaves_a_visible_note(
    db_session: Session,
) -> None:
    """模型调用炸掉时：状态必须可接手，用户必须在界面上看到一句话。

    改造前这里只有"半句话 + 什么都不发生" —— 用户没有任何依据判断
    是卡了、断了、还是模型不想说了。
    """

    class Boom(ScriptedGateway):
        async def chat(self, messages, *, system):  # noqa: ANN001, ANN201
            raise RuntimeError("provider 502")

    session = _new_session(db_session)
    with pytest.raises(engine.ChatEngineError):
        await engine.run_turn_durable(
            db_session,
            session=session,
            content="解题",
            identity=ADMIN,
            gateway=Boom([]),
        )

    runs = db_session.query(ChatTurnRun).filter_by(session_id=session.id).all()
    assert [r.status for r in runs] == ["interrupted"]
    assert "调用模型失败" in (runs[0].last_error or "")

    messages = chat_service.list_messages(db_session, session=session)
    assert messages, "中断后必须留下一条用户看得见的说明"
    assert "中途断了" in messages[-1].content


# ══════════════════════════════════════════════════════════
# 5. 接手：该放弃的要放弃（不能无限烧模型）
# ══════════════════════════════════════════════════════════


async def test_resumer_gives_up_when_the_session_is_archived(
    db_session: Session,
) -> None:
    session = _new_session(db_session)
    run = turn_runs.start(db_session, session_id=session.id)
    db_session.commit()
    _make_stale(db_session, run)
    chat_service.archive_session(db_session, session=session, archived=True)
    db_session.commit()

    ran = await resumer.resume_one(db_session, run)
    assert ran is False
    db_session.refresh(run)
    assert run.status == "failed"
    assert "归档" in (run.last_error or "")


async def test_resumer_gives_up_when_the_owner_account_is_gone(
    db_session: Session,
) -> None:
    """所有者账号没了就不能接手 —— 尤其不能提权成 admin 去跑命令。"""
    session = chat_service.create_session(db_session, created_by="user_ghost")
    db_session.commit()
    run = turn_runs.start(db_session, session_id=session.id)
    db_session.commit()
    _make_stale(db_session, run)

    ran = await resumer.resume_one(db_session, run)
    assert ran is False
    db_session.refresh(run)
    assert run.status == "failed"


async def test_run_forever_feeds_a_real_session_to_the_scanner() -> None:
    """★ 回归：后台循环必须把**真正的 Session** 交给 `stale_runs`。

    ## 这条守的是一个已经上线过的静默缺陷

    `app.main` 原来写的是 `create_task(run_forever(get_session_factory))` ——
    传的是**函数**。循环里 `db = db_factory()` 于是得到 `sessionmaker`，
    `db.scalars(...)` 每 10 秒抛 `AttributeError`，被 `except Exception`
    降级成一条 WARNING。日志里只有"接手扫描失败（下一轮继续）"，
    而**自动接手一次都没发生过**：用户看到的表现与完全没做这个功能一样。

    判据刻意不看日志、不看返回值，而是直接检查"被扫描函数收到了什么类型"：
    只有真的 `Session` 才能查库。若有人把 main 里的传参改回函数，或把
    `run_forever` 里的 `session_factory()` 写回 `db_factory`，这里立刻红。
    """
    from app.core import db as db_module
    from sqlalchemy.orm import Session

    seen: list[object] = []

    def spy(db: object, **_kwargs: object) -> list[object]:
        seen.append(db)
        return []

    original = turn_runs.stale_runs
    turn_runs.stale_runs = spy  # type: ignore[assignment]
    task = asyncio.create_task(
        resumer.run_forever(db_module.get_session_factory(), poll_seconds=0.01)
    )
    try:
        for _ in range(300):
            if seen:
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        turn_runs.stale_runs = original  # type: ignore[assignment]

    assert seen, "后台循环一次扫描都没跑起来"
    assert isinstance(seen[0], Session), (
        f"交给扫描的不是 Session 而是 {type(seen[0]).__name__} —— 这正是"
        "「传 get_session_factory 函数」那个缺陷的形态，自动接手会永远不生效"
    )


async def test_truncated_reply_keeps_continuing_past_the_format_budget(
    db_session: Session,
) -> None:
    """被切断的回复可以连推多次，不受 `MAX_NARRATION_NUDGES`(=2) 限制。

    ★ 这条钉的是线上那条会话（`chat_01M60YNRTZ0Z8S7BF85NNWMRNN`）的形态：
      三条 turn_run 全部 `nudges_used=2` 就收尾，而模型每次都是撞
      `max_tokens` 被切在半句话上。旧逻辑把两种纠偏共用两次额度，于是
      第三次被切断时什么都不做 —— 界面上就是「解一半又停下了」。

    回退方式：把 `MAX_TRUNCATION_CONTINUES` 改成 0，或让 `should_nudge`
    在截断时也看 `max_nudges` → 这里的 calls 会掉回 3 而红。
    """
    session = _new_session(db_session)
    gateway = ScriptedGateway(
        [
            ("我先确认工具。", "length"),
            ("主函数在 0x40070b。看它的逻辑。", "length"),
            ("第二次 read 则", "length"),
            ("FINAL: 拿到 shell 了。", ""),
        ]
    )
    await engine.run_turn(
        db_session,
        session=session,
        content="解出 flag",
        identity=ADMIN,
        gateway=gateway,
    )
    assert gateway.calls == 4, "连续三次被切断都必须接着写，而不是停在半句话上"


async def test_truncation_continues_are_bounded(db_session: Session) -> None:
    """一直撞 `max_tokens` 也必须有限收尾 —— 否则无限续跑烧钱。"""
    session = _new_session(db_session)
    gateway = ScriptedGateway(
        [("还是半句话", "length")] * (engine.MAX_TRUNCATION_CONTINUES + 5)
    )
    await engine.run_turn(
        db_session,
        session=session,
        content="解题",
        identity=ADMIN,
        gateway=gateway,
    )
    assert gateway.calls == engine.MAX_TRUNCATION_CONTINUES + 1


def test_chat_gateway_asks_for_a_larger_output_budget() -> None:
    """对话侧必须显式把 `max_tokens` 抬到 `CHAT_MAX_TOKENS`（v1.21）。

    ★ 为什么用源码契约而不是构造真 gateway：`_build_gateway` 需要一个
      数据库里的 `ProviderAccount` 行，而这里要钉的只是"那一行参数还在"。

    背景：`LlmGateway` 默认 `max_tokens=4096`。模型把推理写进正文时会反复
    撞到这个上限（线上三条 turn_run 都是 `nudges_used=2` 收尾的半句话）。
    截断续跑是兜底，抬上限是减少发生 —— 两者都要有。删掉这个参数不会让
    任何行为测试变红（默认值仍然能跑），所以只能这样钉。
    """
    source = inspect.getsource(engine._build_gateway)
    assert "max_tokens=CHAT_MAX_TOKENS" in source
    assert engine.CHAT_MAX_TOKENS > 4096


async def test_a_tool_round_still_gets_nudged_after_an_empty_reply_ate_the_budget(
    db_session: Session,
) -> None:
    """★ v1.23：空回复吃掉纠偏额度后，跑过工具的轮次**仍然**要被推。

    这是线上会话 `chat_01M61DWC71W8989RRMW6DZS9SP` 的完整复现（33 条消息）：

        seq16/17  反汇编结果 → assistant「防护：...看主逻辑。」（只叙述，被推 #1）
        seq18     assistant 内容长度 0（模型抽风写空串，被推 #2 —— 额度用尽）
        seq19     tool run_shell（反汇编输出）
        seq20     assistant「Let me see the rest of the main function.」
                  → 旧逻辑 nudges_used=2 已满 → return → 界面停在这句英文上

    修法：跑过工具的轮次有独立额度 `MAX_TOOL_NUDGES`，不受那 2 次影响。

    回退方式：把 `should_nudge` 里 `if tools_used > 0` 的分支删掉（或挪回
    `nudges_used >= max_nudges` 之后）→ 这里的 calls 会停在 5 而红。
    """
    session = _new_session(db_session)
    gateway = ScriptedGateway(
        [
            ('ACTION: {"tool": "list_scans", "args": {}}', ""),
            ("防护看完了，接着看主逻辑。", ""),
            ("", ""),
            ('ACTION: {"tool": "list_scans", "args": {}}', ""),
            ("Let me see the rest of the main function.", ""),
            ("FINAL: 拿到了。", ""),
        ]
    )
    await engine.run_turn(
        db_session,
        session=session,
        content="解题",
        identity=ADMIN,
        gateway=gateway,
    )
    assert gateway.calls == 6, (
        "跑过工具的轮次在纠偏额度用尽后仍必须被推一次，而不是停在半句英文上"
    )


async def test_a_nudged_answer_is_demoted_to_process(db_session: Session) -> None:
    """★ v1.25：被纠偏推掉的那句"提前结论"要降级成过程面。

    实测缺陷（两次）：模型先给一句完整结论，我们按"跑过工具就推"再推一次，
    它只好把同一句话重复一遍 —— 界面上就是两条几乎一样的气泡。

    修法不是在判据里猜"像不像半句"（「`pwd` 输出是 `/work`。」这种 18 字的
    完整回答任何启发式都会误判），而是**落库时就把它降级**：只有最后一句
    才是 `answer`。

    回退方式：删掉 engine 里 nudge 分支的 `assistant_message.surface = "process"`
    → 这里会看到两条 answer 而红。
    """
    from app.chat.service import list_messages

    session = _new_session(db_session)
    gateway = ScriptedGateway(
        [
            ('ACTION: {"tool": "list_scans", "args": {}}', ""),
            # 第一次给出"完整结论" → 降级为 process，再问一次
            ("`pwd` 输出是 `/work`。", ""),
            # 又是完整结论 → 已经降级过一次，**接受**它作为最终答复
            ("当前工作目录是 `/work`。", ""),
        ]
    )
    await engine.run_turn_durable(
        db_session, session=session, content="看当前目录", identity=ADMIN, gateway=gateway
    )

    rows = list_messages(db_session, session=session, limit=50)
    answers = [r for r in rows if r.role == "assistant" and (r.content or "").strip()]
    assert [r.surface for r in answers] == ["process", "answer"], (
        "只有最后一句才是对话内容；被推掉的提前结论要收进过程详情"
    )
    # 允许为"可能没说完"多问一次（安全网），但界面上只有一句结论
    assert gateway.calls >= 3


# ══════════════════════════════════════════════════════════
# 6. `FINAL:` 打回闸（v1.29）—— 写了 FINAL 但那只是计划
# ══════════════════════════════════════════════════════════


async def test_a_premature_final_is_pushed_back_once_and_the_turn_continues(
    db_session: Session,
) -> None:
    """跑过工具之后写 `FINAL: 现在查 libc 版本。` 不能就此收尾。

    ★ 线上会话 `chat_01M62X9DDC52RM95NKQW8P5DY8` 就是这个形态：模型把
      「接下来要做什么」当成结论写进 `FINAL:`，而 `FINAL:` 是终止符 ——
      用户看到的是「又是中途停下，继续都没用啊」，每点一次「继续」只推进一步。

    修法是**只打回一次**：第二次仍写 FINAL 就认（否则会死循环，见下一条用例）。
    """
    session = _new_session(db_session)
    gateway = ScriptedGateway(
        [
            ('ACTION: {"tool": "list_scans", "args": {}}', ""),
            ("FINAL: 现在查 libc 版本。", ""),
            ("FINAL: 查到了，libc 是 2.31-0ubuntu9。", ""),
        ]
    )
    result = await engine.run_turn(
        db_session,
        session=session,
        content="解出 flag",
        identity=ADMIN,
        gateway=gateway,
    )
    assert gateway.calls == 3, "计划型的 FINAL 必须被打回一次，而不是直接收尾"
    assert result.messages[-1].content.strip() == "查到了，libc 是 2.31-0ubuntu9。"


async def test_the_premature_final_is_stored_as_process_not_as_an_answer(
    db_session: Session,
) -> None:
    """被打回的那句不能以「结论气泡」的身份留在界面上。

    它还会被写进历史（模型下一轮要看到自己说过什么），但 `surface` 必须是
    `process` —— 否则用户先看到一句「现在查 libc 版本。」的结论气泡，
    再看到真正的结论，同一个问题两个答案。
    """
    session = _new_session(db_session)
    gateway = ScriptedGateway(
        [
            ('ACTION: {"tool": "list_scans", "args": {}}', ""),
            ("FINAL: 现在查 libc 版本。", ""),
            ("FINAL: 结论是 2.31。", ""),
        ]
    )
    await engine.run_turn(
        db_session,
        session=session,
        content="解题",
        identity=ADMIN,
        gateway=gateway,
    )
    rows = chat_service.list_messages(db_session, session=session)
    premature = [r for r in rows if r.content.strip() == "现在查 libc 版本。"]
    assert premature, "那句计划必须留在历史里（模型要看到自己说过）"
    assert premature[0].surface == "process", "计划型的 FINAL 不能当结论气泡"
    assert rows[-1].surface == "answer"


async def test_repeated_finals_are_bounded_and_never_end_silently(
    db_session: Session,
) -> None:
    """模型坚持写计划式 FINAL 时必须收尾，但**不能静默把计划当结论**。

    v1.30 之前这条用例断言的是「第二次 FINAL 必须被接受」—— 那正是线上
    会话 `chat_01M640EY2P54QC1E2QS54R0R3Z` 停在半路的原因：唯一的打回额度
    被前一句计划用掉，第二句同样是计划却只能当结论（`surface=answer`）。

    现在的契约是两句话：
    * 调用次数依然有界（每轮最多 `MAX_PLAN_BOUNCES` 次打回 + 1 次收尾）；
    * 额度用尽时补一句实话，不把计划伪装成结果。
    """
    session = _new_session(db_session)
    plan = "现在查 libc 版本。"
    replies = [('ACTION: {"tool": "list_scans", "args": {}}', "")]
    replies += [(f"FINAL: {plan}", "")] * (engine.MAX_PLAN_BOUNCES + 3)
    gateway = ScriptedGateway(replies)
    result = await engine.run_turn(
        db_session,
        session=session,
        content="解题",
        identity=ADMIN,
        gateway=gateway,
    )
    assert gateway.calls == 1 + engine.MAX_PLAN_BOUNCES + 1, (
        "打回额度必须有限，否则模型一直写计划句就会死循环烧钱"
    )
    plan_messages = [m for m in result.messages if m.content.strip() == plan]
    assert all(m.surface == "process" for m in plan_messages), (
        "计划句永远不是结论，哪怕打回次数已经用完"
    )
    assert "继续" in result.messages[-1].content


async def test_a_resumed_turn_keeps_its_tool_count(db_session: Session) -> None:
    """接手一轮时必须接着用**原来那轮的工具计数**（v1.29 修的真 bug）。

    ## 这条守的是什么

    `run_turn` 里原来把 `tools_used = 0` 写在 `if resume_from is not None:`
    **之后**，无条件清零 —— 于是接手的那一轮永远以为「这一轮没跑过工具」，
    而 `should_nudge` 的 tool 分支正是靠 `tools_used > 0` 判「任务型轮次」。
    后果：接手后模型只写一句叙述就静默收尾，正是用户说的「继续都没用」。

    判据不看内部变量，只看**多花了几次模型调用**：
      - 计数被保留（tools_used=2）→ 第一句叙述就该推 → calls == 2；
      - 计数被清零 → 那句「输出都在上面了。」零意图词 → 不推 → calls == 1。
    """
    session = _new_session(db_session)
    run = turn_runs.start(db_session, session_id=session.id)
    turn_runs.heartbeat(
        db_session,
        run,
        round_index=3,
        nudges_used=0,
        tool_nudges_used=0,
        tools_used=2,
    )
    db_session.commit()

    gateway = ScriptedGateway([("输出都在上面了。", ""), ("FINAL: 好了。", "")])
    await engine.run_turn(
        db_session,
        session=session,
        content="",
        identity=ADMIN,
        gateway=gateway,
        turn_run=run,
        resume_from=run,
    )
    assert gateway.calls >= 2, (
        "接手后工具计数必须被保留（bug 时清零 → 这句零意图词的叙述不推 → calls==1）"
    )


async def test_empty_reply_without_tools_uses_the_narration_budget(
    db_session: Session,
) -> None:
    """没跑过工具的轮次，空回复仍走 `MAX_NARRATION_NUDGES` 那条老额度。

    v1.31 给跑过工具的轮次单独开了空回复额度，但不能顺手把闲聊也改宽：
    「在吗」这种一问一答的轮次碰到空回复，历史行为是推一次就够。
    """
    session = _new_session(db_session)
    gateway = ScriptedGateway([("", ""), ("FINAL: 你好。", "")])
    await engine.run_turn(
        db_session,
        session=session,
        content="在吗",
        identity=ADMIN,
        gateway=gateway,
    )
    assert gateway.calls == 2


async def test_truncated_empty_reply_after_tools_uses_the_truncation_budget(
    db_session: Session,
) -> None:
    """跑过工具的轮次被截断到空串时，走的是**截断**额度而不是空回复额度。

    两条额度的语义不同：截断是「字数切在半句」，空回复是「一个字没写」。
    混用会让截断的重试次数跟着空回复的默认值飘。
    """
    session = _new_session(db_session)
    gateway = ScriptedGateway(
        [
            ('ACTION: {"tool": "list_scans", "args": {}}', ""),
            ("", "length"),
            ("FINAL: flag{ok}", ""),
        ]
    )
    await engine.run_turn(
        db_session,
        session=session,
        content="解题",
        identity=ADMIN,
        gateway=gateway,
    )
    assert gateway.calls == 3


async def test_approved_resume_bounces_its_plan_and_survives_empty_replies(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """点「确认」之后的续跑，也必须能打回计划句、扛住空回复。

    这条守的是 v1.31 删掉的那行：`resume_approved` 里原本有一段纠偏代码，
    但它前面无条件 `return result`，所以批准后的续跑**从来没有纠偏过** ——
    模型说完一句「我来跑一下」就直接收尾，与 `run_turn` 的 v1.13 缺陷同源。

    判据只看次数与落库的 `surface`：计划句必须四步走完
    （工具 → 计划句 → 空回复 → 结论），且计划句永远不是 `answer`。
    """
    session = _new_session(db_session)
    message = chat_service.append_tool_message(
        db_session,
        session=session,
        tool_name="list_scans",
        tool_args={},
        content="",
    )
    message.approval_status = "approved"
    db_session.commit()

    async def fake_execute(ctx, name, args):  # noqa: ANN001, ANN202
        return "scans: 3", False

    monkeypatch.setattr(engine, "_execute_tool", fake_execute)

    plan = "先看反汇编，确认溢出点，然后再写 exploit。"
    gateway = ScriptedGateway(
        [
            ('ACTION: {"tool": "list_scans", "args": {}}', ""),
            (plan, ""),
            ("", ""),
            ("FINAL: 好了。", ""),
        ]
    )
    result = await engine.resume_approved(
        db_session,
        session=session,
        message=message,
        identity=ADMIN,
        gateway=gateway,
    )
    assert gateway.calls == 4, (
        "批准后的续跑必须能推：工具 → 计划句打回 → 空回复推一次 → 结论"
    )
    plan_messages = [m for m in result.messages if m.content == plan]
    assert plan_messages, "那句计划句必须落库（用户要能看见它）"
    assert all(m.surface == "process" for m in plan_messages), (
        "计划句是过程面，不能画成结论气泡"
    )
    assert result.messages[-1].content == "好了。"


async def test_approved_resume_without_actions_uses_the_old_budgets(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """续跑里一个工具都没跑时，截断/空回复仍走原来的两条额度。

    批准后模型可能先被截断、再吐一次空 —— 两者都不是「跑过工具」的轮次，
    所以 `tools_used > 0` 那两条分支不能盖住它们。
    """
    session = _new_session(db_session)
    message = chat_service.append_tool_message(
        db_session,
        session=session,
        tool_name="list_scans",
        tool_args={},
        content="",
    )
    message.approval_status = "approved"
    db_session.commit()

    async def fake_execute(ctx, name, args):  # noqa: ANN001, ANN202
        return "scans: 0", False

    monkeypatch.setattr(engine, "_execute_tool", fake_execute)

    gateway = ScriptedGateway(
        [("", "length"), ("", ""), ("FINAL: 好了。", "")]
    )
    result = await engine.resume_approved(
        db_session,
        session=session,
        message=message,
        identity=ADMIN,
        gateway=gateway,
    )
    assert gateway.calls == 3
    assert result.messages[-1].content == "好了。"


async def test_approved_resume_stops_on_a_plan_when_bounces_run_out(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """续跑把计划句的打回额度用完时，也要补一句实话再收尾。

    否则用户看到的最后一句话仍然是那句计划 —— 与主循环 v1.30 修的是同一件事，
    只是换在了批准后的分支上。
    """
    session = _new_session(db_session)
    message = chat_service.append_tool_message(
        db_session,
        session=session,
        tool_name="list_scans",
        tool_args={},
        content="",
    )
    message.approval_status = "approved"
    db_session.commit()

    async def fake_execute(ctx, name, args):  # noqa: ANN001, ANN202
        return "scans: 1", False

    monkeypatch.setattr(engine, "_execute_tool", fake_execute)
    monkeypatch.setattr(engine, "MAX_PLAN_BOUNCES", 1)

    plan = "FINAL: 先看反汇编，确认溢出点。"
    gateway = ScriptedGateway(
        [
            ('ACTION: {"tool": "list_scans", "args": {}}', ""),
            (plan, ""),
            (plan, ""),
        ]
    )
    result = await engine.resume_approved(
        db_session,
        session=session,
        message=message,
        identity=ADMIN,
        gateway=gateway,
    )
    assert result.messages[-1].content == engine.render_plan_exhausted_note()
    assert "继续" in result.messages[-1].content
    assert result.needs_continue is True, "批准后续跑停在计划上，也不是答完了"



async def test_repeated_prose_plans_are_bounded(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """计划句打回额度用尽后立刻收尾，不再先走 tool nudge（v1.32）。

    reviewer-9e8e8e 的 P2：`run_turn` 的 `if plan_like_final:` 原本在
    `should_nudge(...)` **之后**，于是额度用尽的计划句还会被 tool nudge
    继续推，最多拖到 300 次工具轮数；`resume_approved` 里同一段却在之前。
    现在两边顺序一致：额度用尽 → 补一句实话 → `return`。
    """
    monkeypatch.setattr(engine, "MAX_PLAN_BOUNCES", 1)
    session = _new_session(db_session)
    plan = "接下来查看 main。"
    gateway = ScriptedGateway(
        [
            ('ACTION: {"tool": "list_scans", "args": {}}', ""),
            (plan, ""),
            (plan, ""),
        ]
    )
    result = await engine.run_turn(
        db_session,
        session=session,
        content="解一下这道题",
        identity=ADMIN,
        gateway=gateway,
    )
    assert gateway.calls == 3, "额度用尽后必须立刻收尾，不能再走 tool nudge"
    assert result.messages[-1].content == engine.render_plan_exhausted_note()
