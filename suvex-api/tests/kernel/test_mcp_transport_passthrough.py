"""MCP 的 http 传输必须真的能保存/试连（2026-11-09）。

## 事故

`McpServerIn` 支持 `type/url/headers/preserveHeaders`，`save_mcp_servers`
也全都认 —— 唯独 router 里组装 dict 时把这几个字段**丢了**。后果：

1. 保存一条 http 连接 → 服务端看到的是 `type=stdio` + 空 command
   → 400「stdio 连接必须填 command」，而界面上明明填的是 URL；
2. 编辑一条已有连接只改备注 → 请求里没有 `preserve_headers`
   → 已存的请求头被当成 `{}` 写回，**凭据静默清空**。

两条都是"用户看不见失败原因"的那类。
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.kernel import service as kernel_service

API = "/api/v1"


def _put(client: TestClient, servers: list[dict]):
    return client.put(f"{API}/kernel/mcp-servers", json={"servers": servers})


def test_http_transport_is_saved(ready_client: TestClient, db_session: Session) -> None:
    resp = _put(
        ready_client,
        [
            {
                "name": "remote",
                "type": "http",
                "url": "https://mcp.example.test/mcp",
                "headers": {"Authorization": "Bearer SECRET"},
            }
        ],
    )
    assert resp.status_code == 200, resp.text

    rows = kernel_service.get_mcp_servers(db_session)
    saved = next(item for item in rows if item.get("name") == "remote")
    assert saved["type"] == "http"
    assert saved["url"] == "https://mcp.example.test/mcp"
    assert saved["headers"] == {"Authorization": "Bearer SECRET"}


def test_preserve_headers_keeps_stored_credentials(
    ready_client: TestClient, db_session: Session
) -> None:
    """★ 只改备注时，已存请求头不能被清空。"""
    _put(
        ready_client,
        [
            {
                "name": "remote",
                "type": "http",
                "url": "https://mcp.example.test/mcp",
                "headers": {"Authorization": "Bearer SECRET"},
            }
        ],
    )

    resp = _put(
        ready_client,
        [
            {
                "name": "remote",
                "type": "http",
                "url": "https://mcp.example.test/mcp",
                "headers": {},
                "preserveHeaders": True,
                "notes": "只改了备注",
            }
        ],
    )
    assert resp.status_code == 200, resp.text

    saved = next(
        item for item in kernel_service.get_mcp_servers(db_session) if item.get("name") == "remote"
    )
    assert saved["headers"] == {"Authorization": "Bearer SECRET"}
    assert saved["notes"] == "只改了备注"


def test_http_test_connection_does_not_demand_command(ready_client: TestClient) -> None:
    """试连一条 http 连接时，报错不能说"没有填 command"。"""
    resp = ready_client.post(
        f"{API}/kernel/mcp-servers/test",
        json={
            "server": {"name": "remote", "type": "http", "url": "http://127.0.0.1:1/mcp"},
        },
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    reason = f"{payload.get('reason') or ''} {payload.get('message') or ''}"
    assert "command" not in reason.lower(), reason


async def test_mcp_stdio_does_not_inherit_host_secrets(monkeypatch) -> None:
    """MCP 子进程只能拿白名单环境，不能把 SUPEX_SECRET_KEY 交出去。"""
    import asyncio

    import pytest

    from app.kernel import mcp as mcp_mod

    captured: dict[str, str] = {}

    async def fake_exec(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        captured.update(kwargs.get("env") or {})
        raise RuntimeError("stop before protocol handshake")

    monkeypatch.setenv("SUPEX_SECRET_KEY", "must-not-leak")
    monkeypatch.setenv("SUPEX_DATABASE_URL", "postgresql://secret")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    client = mcp_mod.McpClient(
        mcp_mod.McpServerConfig(
            name="probe",
            command="python3",
            args=["-c", "pass"],
            env={"MCP_TOKEN": "allowed"},
        )
    )
    with pytest.raises(RuntimeError):
        await client._start()

    assert "SUPEX_SECRET_KEY" not in captured
    assert "SUPEX_DATABASE_URL" not in captured
    assert captured.get("MCP_TOKEN") == "allowed"
    assert "PATH" in captured
