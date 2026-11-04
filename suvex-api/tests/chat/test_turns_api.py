"""对话轮执行账目的 HTTP 面（v1.19）。

用户报的是「对话又没反应了，没有长任务持久化啊」。后端把每一轮的执行
状态落库之后，还差最后一环：**用户能看见、能动手**。这个文件守两件事：

1. `GET /chat/sessions/{id}/turns` 真的把状态说出来，且 `resumable` 判据
   与后台接手用的是同一个函数（前后端不各写一份）。
2. `POST .../turns/{run_id}/resume` 的两道闸：已结束的轮次点继续必须
   明确报错（而不是"看起来成功了但什么都没发生"）；中断的轮次点继续
   必须真的产出新消息。

⚠️ 这里**不去断言"后台循环会不会捡它"** —— 后台行为由
`tests/chat/test_turn_persistence.py` 的纯函数用例守；HTTP 层只验接线。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.chat import resumer, service as chat_service, turn_runs
from app.chat.models import ChatSession
from app.core.timeutil import now_utc


def _make_session_via_api(client: TestClient) -> str:
    response = client.post("/api/v1/chat/sessions", json={"title": "长任务"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _age_and_interrupt(db: Session, run_id: str) -> None:
    """把一轮做旧并置成 `interrupted`（模拟"进程被杀掉"）。"""
    run = turn_runs.get(db, run_id=run_id)
    assert run is not None
    run.status = "interrupted"
    run.heartbeat_at = now_utc() - timedelta(seconds=turn_runs.STALE_SECONDS + 30)
    run.updated_at = now_utc()
    db.commit()


def test_turns_endpoint_reports_an_interrupted_run_as_resumable(
    ready_client: TestClient, db_session: Session
) -> None:
    session_id = _make_session_via_api(ready_client)
    run = turn_runs.start(db_session, session_id=session_id)
    db_session.commit()
    _age_and_interrupt(db_session, run.id)

    response = ready_client.get(f"/api/v1/chat/sessions/{session_id}/turns")
    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["status"] == "interrupted"
    assert rows[0]["resumable"] is True, "中断且心跳过期的轮次必须可接手"


def test_turns_endpoint_reports_round_and_tool_progress(
    ready_client: TestClient, db_session: Session
) -> None:
    """★ v1.61：`/turns` 必须把轮次进度（第几轮 / 用过几次工具 / 心跳）吐出来。

    这是「服务端在跑、前端不知道进行到哪了」那条症状的**服务端侧接线**：
    前端在没有 SSE 时靠轮询这个端点画"仍在推进：第 N 轮 · 已用 M 次工具"。
    漏了这几项，前端拿到的就永远是 0/0，进度条会卡在"第 1 轮"看起来像没动。
    """
    session_id = _make_session_via_api(ready_client)
    run = turn_runs.start(db_session, session_id=session_id)
    db_session.commit()
    # 模拟引擎跑了几轮、执行过几次工具后的心跳。
    turn_runs.heartbeat(
        db_session,
        run,
        round_index=4,
        nudges_used=1,
        truncations_used=0,
        tool_nudges_used=0,
        tools_used=7,
    )
    db_session.commit()

    response = ready_client.get(f"/api/v1/chat/sessions/{session_id}/turns")
    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) == 1
    # 驼峰命名（CamelModel）：roundIndex / toolsUsed / heartbeatAt。
    assert rows[0]["roundIndex"] == 4
    assert rows[0]["toolsUsed"] == 7
    assert rows[0]["heartbeatAt"] is not None
    assert rows[0]["status"] == "running"


def test_turns_endpoint_hides_another_users_session(
    ready_client: TestClient, db_session: Session
) -> None:
    other = chat_service.create_session(db_session, created_by="someone-else", title="别人的")
    db_session.commit()

    response = ready_client.get(f"/api/v1/chat/sessions/{other.id}/turns")
    assert response.status_code == 404


def test_resuming_a_finished_run_is_rejected(
    ready_client: TestClient, db_session: Session
) -> None:
    """已经跑完的轮次点继续必须报错。

    ★ 这是最容易被做成"静默无操作"的一条：如果只查 run 存在就往下走，
      用户点了继续、界面什么都没变，还以为是自己网卡了。
    """
    session_id = _make_session_via_api(ready_client)
    run = turn_runs.start(db_session, session_id=session_id)
    db_session.commit()
    turn_runs.finish(db_session, run, status="done")
    db_session.commit()

    response = ready_client.post(
        f"/api/v1/chat/sessions/{session_id}/turns/{run.id}/resume"
    )
    assert response.status_code == 400, response.text
    assert "不需要继续" in response.text


def test_resuming_an_unknown_run_is_404(
    ready_client: TestClient,
) -> None:
    session_id = _make_session_via_api(ready_client)
    response = ready_client.post(
        f"/api/v1/chat/sessions/{session_id}/turns/turn_does_not_exist/resume"
    )
    assert response.status_code == 404


def test_resuming_an_interrupted_run_returns_its_new_messages(
    ready_client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """接手成功时，响应必须是**这一轮新落库的消息**。

    替身只做"接手者会做的事"（记账 + 落一条回答），不去真调模型 ——
    HTTP 层要验的是"端点把接手结果正确带回给前端"，不是模型说什么。
    """
    session_id = _make_session_via_api(ready_client)
    run = turn_runs.start(db_session, session_id=session_id)
    db_session.commit()
    _age_and_interrupt(db_session, run.id)

    async def fake_resume_one(db: Session, target: object) -> bool:
        row = turn_runs.get(db, run_id=getattr(target, "id"))
        assert row is not None
        chat_session = db.get(ChatSession, row.session_id)
        chat_service.append_assistant_message(
            db, session=chat_session, content="接着跑完了，结论在上面。"
        )
        turn_runs.finish(db, row, status="done")
        db.commit()
        return True

    monkeypatch.setattr(resumer, "resume_one", fake_resume_one)

    response = ready_client.post(
        f"/api/v1/chat/sessions/{session_id}/turns/{run.id}/resume"
    )
    assert response.status_code == 200, response.text
    messages = response.json()["messages"]
    assert [m["role"] for m in messages] == ["assistant"]
    assert "接着跑完了" in messages[0]["content"]


def test_a_failed_run_can_be_retried_by_a_human(
    ready_client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ v1.22：`failed`（自动接手已放弃）也必须允许人工重试。

    典型场景：密钥过期 → 后台判定"再跑也没用"直接 `give_up`，状态落成
    `failed`；用户去设置页改好密钥，回来点错误条上的「重试」。

    ★ 这条钉的是一个**把用户唯一能做对的事堵死**的缺陷形态：如果这里
      继续用 `is_resumable` 判（它只认 running/interrupted），用户改完
      密钥点重试会收到 400「这一轮不需要继续」—— 而界面上的错误条正要
      求他去改密钥。要求做一件事，却在做完后拒绝执行。

    回退方式：把 router 里 `if run.status == "failed": reset_for_manual_retry`
    删掉 → 这里会变 400 而红。
    """
    session_id = _make_session_via_api(ready_client)
    run = turn_runs.start(db_session, session_id=session_id)
    db_session.commit()
    turn_runs.give_up(db_session, run, reason="密钥无效")
    db_session.commit()
    assert run.status == "failed"

    async def fake_resume_one(db: Session, target: object) -> bool:
        row = turn_runs.get(db, run_id=getattr(target, "id"))
        assert row is not None
        chat_session = db.get(ChatSession, row.session_id)
        chat_service.append_assistant_message(
            db, session=chat_session, content="改完密钥之后跑通了。"
        )
        turn_runs.finish(db, row, status="done")
        db.commit()
        return True

    monkeypatch.setattr(resumer, "resume_one", fake_resume_one)

    response = ready_client.post(
        f"/api/v1/chat/sessions/{session_id}/turns/{run.id}/resume"
    )
    assert response.status_code == 200, response.text
    assert "跑通了" in response.json()["messages"][0]["content"]


def test_interrupt_without_a_running_turn_is_rejected(
    ready_client: TestClient,
) -> None:
    """★ v1.25：没有在跑的轮次时，中断要明确拒绝（409），而不是假装排上了。"""
    session_id = _make_session_via_api(ready_client)
    response = ready_client.post(
        f"/api/v1/chat/sessions/{session_id}/interrupt",
        json={"mode": "stop", "note": ""},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "NO_RUNNING_TURN"


def test_interrupt_with_a_running_turn_is_scheduled(
    ready_client: TestClient, db_session: Session
) -> None:
    """有 open 的轮次时，中断请求被接受并落下信号。"""
    from app.chat import interrupts

    interrupts.reset_all()
    session_id = _make_session_via_api(ready_client)
    turn_runs.start(db_session, session_id=session_id)
    db_session.commit()

    response = ready_client.post(
        f"/api/v1/chat/sessions/{session_id}/interrupt",
        json={"mode": "continue", "note": "换个入口"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "continue"
    assert body["hasNote"] is True
    # 信号真的落到了注册表（引擎下一个检查点会看到）
    assert interrupts.take_note(session_id) == "换个入口"
    interrupts.reset_all()


def test_sending_a_message_while_a_turn_is_running_is_rejected(
    ready_client: TestClient, db_session: Session
) -> None:
    """★ v1.49：会话已经有一轮在跑时，`POST .../messages/stream` 必须 409。

    改造前这里什么都不查，`run_turn_durable` → `_run_guarded` →
    `turn_run_store.start()` 是**无条件新建一条 turn**。线上事故
    （chat_01M6BWQ69MZJNBAB7C7QMVXPV2）：一条长任务 turn 从 14:06 一直
    running（浏览器 SSE 断了、服务端还在跑），用户以为停了就发「继续」，
    结果同一会话上起了两条并发 turn —— 老的那条继续干，新起的那条
    `round_index=0 / tools_used=0` 空转 20 多秒后 `done`。用户看到的就是
    「问一句停一次」。

    这个用例只验入口闸：有 open 轮次 → 409 `TURN_ALREADY_RUNNING`，
    且**不会**因此落下第二条 turn。
    """
    session_id = _make_session_via_api(ready_client)
    run = turn_runs.start(db_session, session_id=session_id)
    db_session.commit()
    assert run.status in turn_runs.OPEN_STATUSES

    response = ready_client.post(
        f"/api/v1/chat/sessions/{session_id}/messages/stream",
        json={"content": "继续"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "TURN_ALREADY_RUNNING"

    # 关键：没有偷偷多出第二条 turn。
    rows = turn_runs.list_runs(db_session, session_id=session_id, limit=10)
    assert len(rows) == 1, f"不该新建 turn，实际 {len(rows)} 条"


def test_sending_a_message_is_allowed_after_the_running_turn_finishes(
    ready_client: TestClient, db_session: Session
) -> None:
    """闸只拦「有 open 轮次」；那一轮结束（done）之后必须放行。

    否则修完就又造出「会话再也发不出消息」的新坑。用一个会先撞上闸、
    再被后面的空内容校验挡下的请求来区分：拿到 400（空内容）就说明
    已经越过了 `TURN_ALREADY_RUNNING` 那道闸，也说明没有落在 409。
    """
    session_id = _make_session_via_api(ready_client)
    run = turn_runs.start(db_session, session_id=session_id)
    turn_runs.finish(db_session, run, status="done")
    db_session.commit()

    response = ready_client.post(
        f"/api/v1/chat/sessions/{session_id}/messages/stream",
        json={"content": "   "},
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] != "TURN_ALREADY_RUNNING"
