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


async def test_a_second_final_is_accepted_so_the_turn_is_bounded(
    db_session: Session,
) -> None:
    """模型坚持写 FINAL 时必须收尾 —— 打回闸只能问一次。"""
    session = _new_session(db_session)
    gateway = ScriptedGateway(
        [
            ('ACTION: {"tool": "list_scans", "args": {}}', ""),
            ("FINAL: 现在查 libc 版本。", ""),
            ("FINAL: 现在查 libc 版本。", ""),
        ]
    )
    await engine.run_turn(
        db_session,
        session=session,
        content="解题",
        identity=ADMIN,
        gateway=gateway,
    )
    assert gateway.calls == 3, (
        "第二次 FINAL 必须被接受；否则模型一直写 FINAL 就会死循环烧钱"
    )


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

