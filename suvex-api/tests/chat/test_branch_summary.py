from sqlalchemy.orm import Session

from app.chat import service


def test_fork_injects_branch_summary_as_process_message(db_session: Session) -> None:
    session = service.create_session(db_session, created_by="admin", title="分支测试")
    service.append_user_message(db_session, session=session, content="先看入口")
    service.append_assistant_message(db_session, session=session, content="看完了")
    db_session.commit()

    child = service.fork_session(
        db_session,
        source=session,
        upto_seq=1,
        branch_summary="<summary>另一个分支试过 X，失败原因 Y。</summary>",
    )
    db_session.commit()

    assert child.branch_summary and "另一个分支" in child.branch_summary
    rows = service.list_messages(db_session, session=child, limit=20)
    summary_rows = [r for r in rows if r.surface == "process"]
    assert len(summary_rows) == 1
    assert "另一个分支" in summary_rows[0].content
