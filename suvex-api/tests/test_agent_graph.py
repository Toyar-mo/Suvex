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


def test_runner_executes_open_intents(db_session: Session) -> None:
    """一键执行：领取 → worker 轮次 → 写回；假 runner 不调模型。"""
    import asyncio

    from app.agents import runner_service
    from app.core.db import get_session_factory

    task = service.create_task(db_session, created_by="u1", title="执行器", objective="验证")
    goal = service.add_goal(db_session, task=task, text="完成")
    intent = service.add_intent(db_session, task=task, summary="跑一条命令")

    async def fake_runner(worker, prompt):  # noqa: ANN001
        if worker.persona == "planner":
            return  # 没有新方向，队列结束
        with get_session_factory()() as sub:
            service.finish_intent(
                sub,
                intent_id=intent.id,
                status="done",
                result="fake done",
                worker_session_id=worker.id,
            )

    started = asyncio.run(
        runner_service.run_task(task_id=task.id, identity=None, turn_runner=fake_runner)
    )
    assert started == 1
    db_session.expire_all()
    assert db_session.get(service.AgentIntent, intent.id).status == "done"
    assert db_session.get(service.AgentGoal, goal.id).status == "open"


def test_planner_auto_adds_intent_when_queue_empty(db_session: Session) -> None:
    """队列空、目标未完成时自动跑 planner，并把新意图交给 worker。"""
    import asyncio

    from app.agents import runner_service
    from app.core.db import get_session_factory

    task = service.create_task(db_session, created_by="u1", title="自动规划", objective="验证")
    goal = service.add_goal(db_session, task=task, text="完成")
    added: list[str] = []

    async def fake_runner(session, prompt):  # noqa: ANN001
        if session.persona == "planner":
            with get_session_factory()() as sub:
                if added:
                    service.prove_goal(sub, goal_id=goal.id, evidence="fake done")
                else:
                    row = service.add_intent(
                        sub, task=sub.get(service.AgentTask, task.id), summary="planner 补的"
                    )
                    added.append(row.id)
            return
        with get_session_factory()() as sub:
            for iid in added:
                row = sub.get(service.AgentIntent, iid)
                if row is not None and row.status == "running":
                    service.finish_intent(
                        sub, intent_id=iid, status="done", result="ok", worker_session_id=session.id
                    )

    started = asyncio.run(
        runner_service.run_task(task_id=task.id, identity=None, turn_runner=fake_runner)
    )
    assert started == 1
    db_session.expire_all()
    assert added


def test_finding_tools_are_registered_and_validate_verdict() -> None:
    from types import SimpleNamespace

    from app.agents import finding_tools

    assert {"get_finding_context", "record_finding_result"} <= CHAT_TOOL_NAMES
    import asyncio

    out = asyncio.run(
        finding_tools.record_finding_result(
            SimpleNamespace(db=None, identity=None),
            {"finding_id": "f1", "verdict": "maybe", "evidence": "x"},
        )
    )
    assert out.startswith("失败：verdict")


def test_task_scope_narrows_allowlist(db_session: Session) -> None:
    """worker 会话的 scope 守卫只认任务 scope，不再继承全部扫描授权。"""
    from types import SimpleNamespace

    from app.chat import scope
    from app.chat import service as chat_service

    task = service.create_task(
        db_session, created_by="u1", title="范围", objective="x", scope="只测 example.com"
    )
    session = chat_service.create_session(
        db_session, created_by="u1", title="worker", agent_task_id=task.id
    )
    db_session.commit()
    ctx = SimpleNamespace(db=db_session, session=session)
    hosts, cidrs = scope.allowlist(ctx)
    assert "example.com" in hosts
    assert "127.0.0.0/8" in cidrs


def test_over_http_start_agent_run_from_finding(
    ready_client: TestClient, db_session: Session, monkeypatch
) -> None:  # noqa: ANN001
    """从漏洞详情发起复测：建任务/目标/意图，并禁止真的调模型。"""
    from app.agents import runner_service
    from app.core.timeutil import now_utc
    from app.findings.models import Finding
    from tests.factories import make_scan

    async def noop(**kwargs):  # noqa: ANN003
        return 0

    monkeypatch.setattr(runner_service, "run_task", noop)
    scan = make_scan(db_session, status="completed", target="api.example.com")
    finding = Finding(
        id="f_agent_run",
        scan_id=scan.id,
        source_ref="f_agent_run",
        severity="high",
        category="misconfiguration",
        title="测试漏洞",
        target="api.example.com",
        verification="verified",
        status="confirmed",
        discovered_at=now_utc(),
    )
    db_session.add(finding)
    db_session.commit()

    resp = ready_client.post("/api/v1/findings/f_agent_run/agent-run?mode=retest")
    assert resp.status_code == 200, resp.text
    task_id = resp.json()["taskId"]
    graph = service.graph(db_session, task=db_session.get(service.AgentTask, task_id))
    assert graph["task"].scope == "api.example.com"
    assert len(graph["goals"]) == 1
    assert len(graph["intents"]) == 1
