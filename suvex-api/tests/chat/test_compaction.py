"""上下文压缩（v2.0）：摘要落库 + 只把最近原文喂给模型。"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.chat import compaction, engine
from app.chat import service as chat_service
from app.chat.models import ChatMessage


class _Reply:
    text = "## 目标\n拿到 flag\n\n## 已确认\nFLAG_TABLE 为空"
    finish_reason = "stop"
    reasoning = None
    raw: dict[str, Any] = {}
    usage = None

    def __init__(self) -> None:
        pass


class _Gateway:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, *, system):  # noqa: ANN001, ANN201
        self.calls += 1
        assert "压缩" in system
        return _Reply()


def _seed(db: Session, count: int = 24) -> Any:
    session = chat_service.create_session(db, created_by="user_admin")
    db.commit()
    for index in range(count):
        if index % 2 == 0:
            chat_service.append_user_message(
                db, session=session, content=f"问题 {index}\n" + "x" * 2_000
            )
        else:
            chat_service.append_assistant_message(
                db, session=session, content=f"结论 {index}\n" + "y" * 2_000
            )
    db.commit()
    return session


async def test_compact_persists_boundary_and_keeps_original_messages(db_session: Session) -> None:
    session = _seed(db_session)
    before = db_session.query(ChatMessage).filter_by(session_id=session.id).count()

    row = await compaction.compact(
        db_session, session=session, gateway=_Gateway(), force=True
    )

    assert row is not None
    assert row.first_kept_seq > 1
    assert row.summary.startswith("## 目标")
    assert db_session.query(ChatMessage).filter_by(session_id=session.id).count() == before

    latest, kept = compaction.load_context(db_session, session=session)
    assert latest is not None and latest.id == row.id
    assert kept and all(m.seq >= row.first_kept_seq for m in kept)


async def test_compacted_context_starts_with_summary_then_recent(db_session: Session) -> None:
    session = _seed(db_session)
    await compaction.compact(db_session, session=session, gateway=_Gateway(), force=True)

    comp, rows = compaction.load_context(db_session, session=session)
    messages = engine._render_compacted(comp, rows)

    assert messages[0]["role"] == "user"
    assert "压缩摘要" in messages[0]["content"]
    assert "拿到 flag" in messages[0]["content"]
    # 保留的原文仍然带最新一条，没有被摘要吞掉。
    assert any("结论 23" in m["content"] for m in messages[1:])


async def test_durable_turn_auto_continues_after_compaction(
    db_session: Session, monkeypatch
) -> None:
    session = _seed(db_session, count=8)
    calls: list[dict] = []

    async def fake_run_turn(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        calls.append(kwargs)
        if len(calls) == 1:
            return engine.ChatTurnResult(needs_continue=True)
        return engine.ChatTurnResult()

    async def fake_compact(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        return object()

    monkeypatch.setattr(engine, "run_turn", fake_run_turn)
    monkeypatch.setattr(engine.chat_compaction, "compact", fake_compact)
    monkeypatch.setattr(engine, "_build_gateway", lambda *a, **k: object())

    result = await engine.run_turn_durable(
        db_session, session=session, content="继续", identity=None
    )

    assert result.fatal is False
    assert len(calls) == 2
    assert calls[1]["resume_from"] is not None


def test_context_overflow_is_detected_from_provider_text() -> None:
    assert engine._is_context_overflow(RuntimeError("context_length_exceeded"))
    assert engine._is_context_overflow(RuntimeError("This model's maximum context length is 128000"))
    assert not engine._is_context_overflow(RuntimeError("HTTP 401 invalid token"))


async def test_compaction_boundary_never_keeps_a_bare_assistant(db_session: Session) -> None:
    session = chat_service.create_session(db_session, created_by="user_admin")
    db_session.commit()
    chat_service.append_user_message(db_session, session=session, content="开始")
    for index in range(10):
        chat_service.append_assistant_message(
            db_session, session=session, content=f"过程 {index}\n" + "y" * 3000
        )
        chat_service.append_tool_message(
            db_session,
            session=session,
            tool_name="run_shell",
            tool_args={"command": f"echo {index}"},
            content=f"输出 {index}\n" + "z" * 3000,
            failed=False,
            duration_ms=1,
        )
    db_session.commit()

    row = await compaction.compact(
        db_session, session=session, gateway=_Gateway(), force=True
    )
    assert row is not None
    kept = compaction.load_context(db_session, session=session)[1]
    assert kept and kept[0].role != "assistant", "保留段不能以会被丢掉的 assistant 开头"


def test_compaction_wire_schema_uses_camel_case() -> None:
    from app.chat.schemas import ChatCompactionOut

    out = ChatCompactionOut(
        summary="s", firstKeptSeq=7, tokensBefore=10, tokensAfter=3
    )
    assert out.model_dump(by_alias=True)["firstKeptSeq"] == 7


def test_trigger_is_tighter_for_small_context_models() -> None:
    assert compaction.trigger_chars("gpt-3.5-turbo") < compaction.trigger_chars("deepseek-v4")


def test_context_usage_estimate_is_nonzero(db_session: Session) -> None:
    session = _seed(db_session, count=6)
    assert compaction.estimate_context_tokens(db_session, session=session) > 0
