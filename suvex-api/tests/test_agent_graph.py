"""Agent 执行图 P1：状态机与一键任务的 API。"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.agents import graph_service as service
from app.chat.prompts import CHAT_TOOL_NAMES


def test_graph_state_machine(db_session: Session) -> None:
    task = service.create_task(db_session, created_by="u1", title="测试任务", objective="打穿")
    goal = service.add_goal(db_session, task=task, text="拿到 shell")
    intent = service.add_intent(db_session, task=task, summary="测上传", goal_id=goal.id)

    claimed = service.claim_intent(db_session, intent_id=intent.id, worker_session_id="chat_1")
    assert claimed.status == "running"

    # 只有领取者能写回。
    try:
        service.finish_intent(
            db_session, intent_id=intent.id, status="done", result="ok", worker_session_id="chat_2"
        )
    except Exception as exc:  # noqa: BLE001
        assert "领取它的 worker" in str(exc)
    else:
        raise AssertionError("别的 worker 不能写回")

    service.finish_intent(
        db_session, intent_id=intent.id, status="done", result="upload → webshell", worker_session_id="chat_1"
    )
    service.prove_goal(db_session, goal_id=goal.id, evidence="shell 已拿到")
    db_session.refresh(task)
    assert task.status == "completed"
    assert task.completed_at is not None


def test_graph_tools_are_registered() -> None:
    assert {
        "create_agent_task",
        "add_agent_goal",
        "add_agent_intent",
        "claim_agent_intent",
        "finish_agent_intent",
        "prove_agent_goal",
    } <= CHAT_TOOL_NAMES


def test_over_http_create_and_read_graph(ready_client: TestClient) -> None:
    created = ready_client.post(
        "/api/v1/agent-tasks",
        json={"title": "HTTP 任务", "objective": "验证", "scope": "只测 example.com"},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    task_id = body["task"]["id"]

    goal = ready_client.post(
        f"/api/v1/agent-tasks/{task_id}/goals", json={"text": "确认漏洞"}
    )
    assert goal.status_code == 201, goal.text
    intent = ready_client.post(
        f"/api/v1/agent-tasks/{task_id}/intents",
        json={"summary": "复现", "goalId": goal.json()["id"]},
    )
    assert intent.status_code == 201, intent.text

    graph = ready_client.get(f"/api/v1/agent-tasks/{task_id}")
    assert graph.status_code == 200
    assert graph.json()["task"]["status"] == "running"
    assert len(graph.json()["intents"]) == 1
