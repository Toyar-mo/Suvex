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
        ("no_budget", {"tools_used": 2, "text": "先判型", "nudges_used": 2}, False),
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
    """模型一直只写叙述时，必须有限收尾，不能死循环。"""
    session = _new_session(db_session)
    gateway = ScriptedGateway([("先看看。", "")] * (engine.MAX_NARRATION_NUDGES + 5))
    await engine.run_turn(
        db_session,
        session=session,
        content="解题",
        identity=ADMIN,
        gateway=gateway,
    )
    assert gateway.calls <= 1 + engine.MAX_NARRATION_NUDGES


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
