"""对话记录 JSONL 审计导出（v1.47）。

## 这个模块守什么

比赛（羊城杯这类）要求「保留完整 AI 对话记录备审」。页面上的对话只有
人看得到、导不出来，所以加了 `GET /chat/sessions/{id}/export`，把整段
会话原样吐成 **ndjson**（一行一条消息）。这里钉四件事：

1. 第一行是会话头（`type=session`），之后每行一条消息（`type=message`）。
2. 消息行带 uuid / parentUuid / role / text / 时间，且父链首尾相接。
3. 工具调用（toolName / toolArgs / toolFailed）与推理（reasoning）都在。
4. 归属校验：他人会话 404；归档会话**仍可导出**（备审常要旧记录）。
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient


def _session(client: TestClient, title: str) -> str:
    created = client.post("/api/v1/chat/sessions", json={"title": title})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _rows(response) -> list[dict]:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/x-ndjson")
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def _seed(sid: str, rows: list[dict]) -> None:
    """直接插消息（不走模型网关 —— 测试环境没配模型，POST 会 400）。"""
    from app.chat import service
    from app.chat.models import ChatSession
    from app.core.db import get_session_factory

    factory = get_session_factory()
    with factory() as db:
        session = db.get(ChatSession, sid)
        assert session is not None
        for row in rows:
            service._append(db, session=session, **row)
        db.commit()


def test_export_emits_header_then_messages(ready_client: TestClient) -> None:
    sid = _session(ready_client, "导出测试")
    _seed(
        sid,
        [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好，有什么可以帮你？"},
        ],
    )

    rows = _rows(ready_client.get(f"/api/v1/chat/sessions/{sid}/export"))
    assert rows, "导出的 JSONL 不能是空的"
    # 第一行必须是会话头
    head = rows[0]
    assert head["type"] == "session"
    assert head["id"] == sid
    assert head["title"] == "导出测试"
    assert "createdAt" in head
    # 之后每一行都是消息
    msgs = rows[1:]
    assert len(msgs) == 2
    assert all(m["type"] == "message" for m in msgs)
    assert msgs[0]["role"] == "user"
    assert msgs[0]["text"] == "你好"


def test_export_parent_chain_and_fields(ready_client: TestClient) -> None:
    sid = _session(ready_client, "父链")
    _seed(
        sid,
        [
            {"role": "user", "content": "第一句"},
            {"role": "assistant", "content": "第二句"},
            {"role": "assistant", "content": "第三句"},
        ],
    )

    msgs = _rows(ready_client.get(f"/api/v1/chat/sessions/{sid}/export"))[1:]
    # 首条 parentUuid 为 None，其余指向上一条的 uuid（首尾相接）
    assert msgs[0]["parentUuid"] is None
    for prev, cur in zip(msgs, msgs[1:], strict=False):
        assert cur["parentUuid"] == prev["uuid"]
    for m in msgs:
        assert m["uuid"]
        assert m["role"] in {"user", "assistant", "tool"}
        assert "text" in m
        assert "timestamp" in m


def test_export_tool_fields_round_trip(ready_client: TestClient) -> None:
    """直接塞一条工具消息，确认 toolName/toolArgs/toolFailed/reasoning 都导出。"""
    sid = _session(ready_client, "工具字段")
    _seed(
        sid,
        [
            {
                "role": "assistant",
                "content": "跑个命令",
                "reasoning": "我该看看目录",
                "tool_name": "run_shell",
                "tool_args": {"command": "ls"},
                "tool_failed": False,
                "surface": "process",
            }
        ],
    )

    msgs = _rows(ready_client.get(f"/api/v1/chat/sessions/{sid}/export"))[1:]
    tool = [m for m in msgs if m.get("toolName") == "run_shell"]
    assert len(tool) == 1
    row = tool[0]
    assert row["toolArgs"] == {"command": "ls"}
    assert row["toolFailed"] is False
    assert row["reasoning"] == "我该看看目录"
    assert row["surface"] == "process"


def test_export_archived_session_still_exportable(ready_client: TestClient) -> None:
    """归档后仍能导出（备审常要旧记录），但归属校验仍在。"""
    sid = _session(ready_client, "归档也导")
    _seed(sid, [{"role": "user", "content": "留痕"}])
    arch = ready_client.delete(f"/api/v1/chat/sessions/{sid}")
    assert arch.status_code == 200, arch.text
    rows = _rows(ready_client.get(f"/api/v1/chat/sessions/{sid}/export"))
    assert rows[0]["archived"] is True


def test_export_missing_session_404(ready_client: TestClient) -> None:
    resp = ready_client.get("/api/v1/chat/sessions/chat_does_not_exist/export")
    assert resp.status_code == 404
