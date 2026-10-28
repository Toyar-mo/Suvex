"""运行中打断（v1.25）：中断并继续 / 彻底停止。

用户原话：「打断当前会话也没做，看人家 CyberStrikeAI 的设计」。

这个文件守两件事：
1. 注册表本身（跨线程的小字典）的语义 —— 取走即消费、停止优先、清干净。
2. 引擎真的会在检查点停下 / 注入说明后继续 —— 这是"按停"与"插话"的区别。
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.chat import engine, interrupts
from app.chat.models import ChatMessage, ChatTurnRun
from tests.test_chat import ADMIN


@pytest.fixture(autouse=True)
def _clean_bus():
    interrupts.reset_all()
    yield
    interrupts.reset_all()


# ══════════════════════════════════════════════════════════
# 1. 注册表语义
# ══════════════════════════════════════════════════════════


def test_note_is_consumed_once() -> None:
    """说明只能被取走一次 —— 否则同一句补充会被注入到之后每一轮。"""
    interrupts.request_continue("s1", "换个入口")
    assert interrupts.take_note("s1") == "换个入口"
    assert interrupts.take_note("s1") is None


def test_stop_wins_over_note() -> None:
    """先点了「中断并继续」又点了「彻底停止」→ 停止生效，说明丢掉。"""
    interrupts.request_continue("s1", "补充")
    interrupts.request_stop("s1")
    assert interrupts.stop_requested("s1") is True
    assert interrupts.take_note("s1") is None


def test_clear_removes_everything() -> None:
    """不清干净的话，残留信号会把**下一轮**也打断。"""
    interrupts.request_stop("s1")
    interrupts.clear("s1")
    assert interrupts.stop_requested("s1") is False
    assert interrupts.has_pending("s1") is False


def test_should_break_sees_both_signals() -> None:
    assert interrupts.should_break("s1") is False
    interrupts.request_continue("s1", "")
    assert interrupts.should_break("s1") is True
    interrupts.reset_all()
    interrupts.request_stop("s1")
    assert interrupts.should_break("s1") is True


# ══════════════════════════════════════════════════════════
# 2. 引擎行为
# ══════════════════════════════════════════════════════════


class _Gateway:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls = 0

    async def chat(self, messages, *, system):  # noqa: ANN001, ANN201
        self.calls += 1
        text = self.replies.pop(0) if self.replies else ""
        return _Reply(text)


class _Reply:
    def __init__(self, text: str) -> None:
        self.text = text
        self.reasoning = None
        self.raw: dict = {}
        self.finish_reason = ""
        self.usage = None
        self.source = "content"


def _new_session(db: Session):
    from app.chat import service as chat_service

    session = chat_service.create_session(db, created_by="u1", title="打断")
    db.commit()
    return session


async def test_stop_ends_the_turn_and_marks_it_cancelled(db_session: Session) -> None:
    """「彻底停止」：这一轮直接结束，执行记录落 `cancelled`（不是 interrupted）。"""
    session = _new_session(db_session)
    interrupts.request_stop(session.id)
    gateway = _Gateway(["我先看看目录。", "这句不该被调用"])

    result = await engine.run_turn_durable(
        db_session, session=session, content="解一下", identity=ADMIN, gateway=gateway
    )

    assert result.stopped is True
    assert gateway.calls == 1, "停止之后不该再调模型"

    run = (
        db_session.query(ChatTurnRun)
        .filter(ChatTurnRun.session_id == session.id)
        .order_by(ChatTurnRun.updated_at.desc())
        .first()
    )
    assert run is not None and run.status == "cancelled", (
        "落成 interrupted 会被后台自动接手，与'用户要停'相反"
    )


async def test_continue_injects_the_note_and_keeps_running(db_session: Session) -> None:
    """「中断并继续」：停下 → 把说明作为**过程面** user 消息注入 → 接着跑。"""
    session = _new_session(db_session)
    interrupts.request_continue(session.id, "别扫端口了，先看登录")
    gateway = _Gateway(["我先扫一下端口。", "FINAL: 好，改看登录。"])

    result = await engine.run_turn_durable(
        db_session, session=session, content="解一下", identity=ADMIN, gateway=gateway
    )

    assert gateway.calls == 2, "中断并继续之后必须**接着跑**，而不是结束"

    rows = (
        db_session.query(ChatMessage)
        .filter(ChatMessage.session_id == session.id)
        .order_by(ChatMessage.seq)
        .all()
    )
    injected = [r for r in rows if "用户补充：别扫端口了，先看登录" in (r.content or "")]
    assert len(injected) == 1, "补充说明必须被注入"
    assert injected[0].surface == "process", (
        "它是我们替用户写的注入模板，不能画成用户自己的气泡"
    )


async def test_narration_before_a_tool_call_is_process_not_answer(
    db_session: Session,
) -> None:
    """★ v1.25 核心：带 ACTION 的旁白属于**过程面**，不画气泡。

    用户看到的应该只有：自己的话 →（过程详情）→ 结论。
    """
    session = _new_session(db_session)
    gateway = _Gateway(
        [
            '我先看看目录。\nACTION: {"tool": "list_scans", "args": {}}',
            "FINAL: 看完了。",
        ]
    )

    await engine.run_turn_durable(
        db_session, session=session, content="解一下", identity=ADMIN, gateway=gateway
    )

    rows = (
        db_session.query(ChatMessage)
        .filter(ChatMessage.session_id == session.id)
        .order_by(ChatMessage.seq)
        .all()
    )
    narrations = [r for r in rows if r.role == "assistant" and "我先看看目录" in (r.content or "")]
    assert len(narrations) == 1
    assert narrations[0].surface == "process"

    final = [r for r in rows if r.role == "assistant" and "看完了" in (r.content or "")]
    assert len(final) == 1
    assert final[0].surface == "answer", "最终答复才是对话内容"
