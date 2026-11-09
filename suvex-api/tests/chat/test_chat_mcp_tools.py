"""对话页挂载 MCP server 的测试（v1.80）。

## 这个文件守的是什么

在 v1.80 之前，`ChatSession.mcp_server_names` 只是一个**被存下来的偏好**：
对话引擎不认识它，勾了也不会多出任何工具。这一组用例钉住四件事：

1. **未挂载不暴露** —— 没勾任何 server 时，工具表里一个 `mcp__…` 都没有；
2. **挂载后工具出现且可调用** —— 名字带 `mcp__<server>__<tool>` 前缀，
   模型按文本协议调它，结果真的来自 server；
3. **坏 server 不拖垮整轮** —— 一台连不上只记一条状态（进提示词与
   `/mcp-status`），其余工具照常可用，这一轮照常结束；
4. **审批闸门照旧** —— 开了 `require_approval` 时 MCP 工具也挂起，
   批准后由 `resume_approved` 真执行。

## 为什么用真的 stdio server

与 `tests/test_kernel_mcp.py` 同一条理由：这一层的价值全在"真的说上话"。
用 mock 替掉子进程只能测到"我调了我的方法"，测不到命名、发现、调用、
渲染整条链路。假 server 用 `sys.executable -c` 内联，无需额外依赖。
"""

from __future__ import annotations

import sys

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.chat import engine
from app.chat import mcp_tools
from app.chat import service as chat_service
from tests.test_chat import ADMIN, FakeGateway

API = "/api/v1"

#: 一个极小的 MCP server：两个工具（echo 成功、boom 返回 isError）。
_FAKE_SERVER = r"""
import json, sys

TOOLS = [
    {"name": "echo", "description": "原样回显 text",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
    {"name": "boom", "description": "总是失败",
     "inputSchema": {"type": "object", "properties": {}}},
]

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        continue
    method = msg.get("method")
    mid = msg.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "serverInfo": {"name": "fake", "version": "1.0"}}})
    elif method == "notifications/initialized":
        pass
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "echo":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": "echo:" + str(args.get("text", ""))}],
                "isError": False}})
        else:
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": "工具 " + str(name) + " 总是失败"}],
                "isError": True}})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "unknown"}})
"""


def _server(name: str = "fake", *, command: str | None = None) -> dict:
    return {
        "name": name,
        "command": command or sys.executable,
        "args": ["-c", _FAKE_SERVER],
        "notes": "测试用的假 MCP server",
    }


def _install_servers(db: Session, *servers: dict) -> None:
    """把 MCP 配置写进内核设置（与设置页保存走同一张表）。"""
    from app.kernel import service as kernel_service

    row = kernel_service.get_ctf2_config(db)
    row.mcp_servers = list(servers)
    db.commit()


def _session_with_mounts(db: Session, *names: str, require_approval: bool = False):
    session = chat_service.create_session(db, created_by="user_admin")
    session.mcp_server_names = list(names) or None
    session.require_approval = require_approval
    db.commit()
    return session


# ══════════════════════════════════════════════════════════
# 1. 未挂载：一个 MCP 工具都不出现
# ══════════════════════════════════════════════════════════


async def test_without_mounts_no_mcp_tools_are_exposed(db_session: Session) -> None:
    # 平台里配了 server，但**会话没挂载** —— 仍然不能暴露。
    _install_servers(db_session, _server())
    session = _session_with_mounts(db_session)

    catalog = await mcp_tools.discover_mounted(db_session, session)

    assert catalog.bindings == {}
    assert catalog.status_payload() == {
        "mountedCount": 0,
        "connectedCount": 0,
        "servers": [],
    }
    assert catalog.prompt_section() == ""

    ctx = await engine._build_tool_context(db_session, session, ADMIN)
    names = {row["function"]["name"] for row in engine._tools_schema(ctx)}
    assert not any(name.startswith("mcp__") for name in names)


# ══════════════════════════════════════════════════════════
# 2. 挂载后：工具出现、命名正确、可调用
# ══════════════════════════════════════════════════════════


async def test_mounted_server_tools_are_prefixed_and_callable(db_session: Session) -> None:
    _install_servers(db_session, _server())
    session = _session_with_mounts(db_session, "fake")

    catalog = await mcp_tools.discover_mounted(db_session, session)

    status = catalog.statuses[0]
    assert status.connected is True
    assert status.tool_count == 2
    assert set(catalog.bindings) == {"mcp__fake__echo", "mcp__fake__boom"}
    # schema 来自 server 的 `inputSchema`，不是我们瞎编的。
    assert catalog.bindings["mcp__fake__echo"].input_schema["properties"]["text"]["type"] == "string"

    ctx = await engine._build_tool_context(db_session, session, ADMIN)
    out, failed = await engine._execute_tool(ctx, "mcp__fake__echo", {"text": "hi"})
    assert (out, failed) == ("echo:hi", False)

    # isError=true 要标成失败，但**只**影响这一条工具结果。
    out, failed = await engine._execute_tool(ctx, "mcp__fake__boom", {})
    assert failed is True
    assert "总是失败" in out


async def test_run_turn_exposes_and_executes_mounted_mcp_tool(db_session: Session) -> None:
    """端到端：模型在文本协议里调用 `mcp__fake__echo`，结果落成工具消息。"""
    _install_servers(db_session, _server())
    session = _session_with_mounts(db_session, "fake")

    gateway = FakeGateway(
        [
            '我来调用。\nACTION: {"tool": "mcp__fake__echo", "args": {"text": "hello"}}',
            "调用完成，结果是 echo:hello。",
        ]
    )
    result = await engine.run_turn(
        db_session,
        session=session,
        content="用 MCP 回显一下 hello",
        identity=ADMIN,
        gateway=gateway,
    )

    tool_messages = [m for m in result.messages if m.role == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0].tool_name == "mcp__fake__echo"
    assert tool_messages[0].content == "echo:hello"
    assert tool_messages[0].tool_failed is False
    # 系统提示词里必须点名这个工具与它的 server —— 文本协议网关不认
    # `tools=`，模型只能靠这一段知道工具存在。
    assert "mcp__fake__echo" in gateway.systems[0]
    assert "「fake」" in gateway.systems[0]


# ══════════════════════════════════════════════════════════
# 3. 坏 server：只影响它自己，本轮照常结束
# ══════════════════════════════════════════════════════════


async def test_broken_server_is_reported_but_does_not_break_the_turn(
    db_session: Session,
) -> None:
    _install_servers(
        db_session,
        _server(),
        _server("broken", command="suvex-no-such-mcp-binary-xyz"),
    )
    session = _session_with_mounts(db_session, "fake", "broken")

    catalog = await mcp_tools.discover_mounted(db_session, session)

    by_name = {status.name: status for status in catalog.statuses}
    assert by_name["fake"].connected is True
    assert by_name["broken"].connected is False
    assert by_name["broken"].reason
    # 好 server 的工具照常可用；坏的一件都没暴露。
    assert "mcp__fake__echo" in catalog.bindings
    assert not any(name.startswith("mcp__broken__") for name in catalog.bindings)

    gateway = FakeGateway(["你好。"])
    result = await engine.run_turn(
        db_session,
        session=session,
        content="你好",
        identity=ADMIN,
        gateway=gateway,
    )

    # 这一轮没有被坏 server 打断，正常给出了回答。
    assert result.messages[-1].role == "assistant"
    # 提示词明确告诉模型：这台连不上，别调它的工具。
    assert "连不上" in gateway.systems[0]
    assert "broken" in gateway.systems[0]


# ══════════════════════════════════════════════════════════
# 4. 审批：MCP 工具也挂起；批准后真执行
# ══════════════════════════════════════════════════════════


async def test_approval_gates_mcp_tools_and_resume_executes_them(
    db_session: Session,
) -> None:
    _install_servers(db_session, _server())
    session = _session_with_mounts(db_session, "fake", require_approval=True)

    gateway = FakeGateway(
        ['ACTION: {"tool": "mcp__fake__echo", "args": {"text": "gated"}}']
    )
    result = await engine.run_turn(
        db_session,
        session=session,
        content="用 MCP 回显",
        identity=ADMIN,
        gateway=gateway,
    )

    assert result.awaiting_approval is not None
    pending = [m for m in result.messages if m.role == "tool"][-1]
    assert pending.tool_name == "mcp__fake__echo"
    assert pending.approval_status == "pending"
    # 挂起期间**没有**执行（内容仍是"等待确认"，不是 server 的返回）。
    assert "等待确认" in pending.content

    chat_service.decide_approval(
        db_session, session=session, message=pending, approve=True, decided_by="user_admin"
    )
    db_session.commit()

    resumed = await engine.resume_approved(
        db_session,
        session=session,
        message=pending,
        identity=ADMIN,
        gateway=FakeGateway(["好了，已经调用完。"]),
    )

    assert pending.content == "echo:gated"
    assert pending.tool_failed is False
    assert any(m.tool_name == "mcp__fake__echo" for m in resumed.messages)


# ══════════════════════════════════════════════════════════
# 5. 界面用的真实状态端点
# ══════════════════════════════════════════════════════════


def test_mcp_status_endpoint_reports_real_connection(
    ready_client: TestClient, db_session: Session
) -> None:
    """`/mcp-status` 必须**真连一次**，而不是回显会话里的名字。"""
    created = ready_client.post(f"{API}/chat/sessions", json={})
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]

    _install_servers(
        db_session,
        _server(),
        _server("broken", command="suvex-no-such-mcp-binary-xyz"),
    )
    saved = ready_client.patch(
        f"{API}/chat/sessions/{session_id}/settings",
        json={"mcpServerNames": ["fake", "broken"]},
    )
    assert saved.status_code == 200, saved.text

    response = ready_client.get(f"{API}/chat/sessions/{session_id}/mcp-status")
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["mountedCount"] == 2
    assert payload["connectedCount"] == 1
    by_name = {row["name"]: row for row in payload["servers"]}
    assert by_name["fake"]["connected"] is True
    assert by_name["fake"]["toolCount"] == 2
    assert by_name["broken"]["connected"] is False
    assert by_name["broken"]["reason"]
