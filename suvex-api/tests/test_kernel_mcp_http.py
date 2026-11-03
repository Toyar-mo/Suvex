"""v1.62：MCP streamable-HTTP 传输的测试。

背景：v1.61 及之前整条 MCP 链路**只做 stdio**。用户贴了一条
`{"type":"http","url":"https://.../mcp"}` 的配置，模型被引去 `npx` spawn
进程、连不上远端 URL，报「stdout 已关闭（server 退出了）」—— 一个 grep 不到
的假象：问题不在 stdio，在于这条连接根本不该走 stdio。

这里用 `httpx.MockTransport` 桩一个远端 MCP server（不碰真网络），验证：
  · 配置解析认得 http 条目（name+url，无需 command）；
  · SSE / application/json 两种响应都能抽出 JSON-RPC 消息；
  · initialize 抓 `Mcp-Session-Id`、后续请求带回；
  · list_tools / call_tool 两条路径都通；
  · 通知（notifications/initialized）走 202；
  · stdio 不回归（仍按 command spawn）。
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.kernel import mcp

# ── 组 1：配置解析（http 条目）────────────────────────────────


def test_parse_http_entry_only_needs_name_and_url():
    """http 条目只要 name+url —— **不需要** command。

    这正是老版本的病根：`parse_server_configs` 只认 command，于是 http
    条目被当坏条目静默跳过（症状："我配了 http 却说我一条都没有"）。
    """
    cfgs = mcp.parse_server_configs(
        [
            {
                "name": "ctf2-suvex",
                "type": "http",
                "url": "https://ctf2.dasctf.com/api/ai/v1/mcp",
            }
        ]
    )
    assert len(cfgs) == 1
    cfg = cfgs[0]
    assert cfg.name == "ctf2-suvex"
    assert cfg.is_http() is True
    assert cfg.type == "http"
    assert cfg.url == "https://ctf2.dasctf.com/api/ai/v1/mcp"
    assert cfg.command == ""


def test_parse_http_keeps_headers_but_public_dict_hides_them():
    """headers 可能含 token —— 解析要收，公开视图**不回显**。"""
    cfgs = mcp.parse_server_configs(
        [
            {
                "name": "x",
                "type": "http",
                "url": "https://h/mcp",
                "headers": {"Authorization": "Bearer secret"},
            }
        ]
    )
    assert cfgs[0].headers == {"Authorization": "Bearer secret"}
    public = cfgs[0].public_dict()
    assert "headers" not in public  # 不回显
    assert public["url"] == "https://h/mcp"  # url 不是凭据，回显
    assert public["type"] == "http"


def test_parse_http_missing_url_is_skipped():
    """http 条目缺 url = 坏条目，跳过（不抛，rest 照用）。"""
    cfgs = mcp.parse_server_configs(
        [
            {"name": "bad", "type": "http"},  # 缺 url
            {"name": "good", "type": "http", "url": "https://h/mcp"},
        ]
    )
    assert [c.name for c in cfgs] == ["good"]


def test_parse_stdio_without_type_still_works():
    """缺 type 的条目按 stdio 处理（向后兼容：历史配置没有 type 字段）。"""
    cfgs = mcp.parse_server_configs([{"name": "s", "command": "npx", "args": ["-y", "x"]}])
    assert cfgs[0].type == "stdio"
    assert cfgs[0].is_http() is False
    assert cfgs[0].command == "npx"


# ── 组 2：SSE 解析 ───────────────────────────────────────────


def test_parse_sse_messages_extracts_data_lines():
    body = (
        ": comment line should be ignored\n"
        "event: message\n"
        'data: {"jsonrpc":"2.0","id":1,"result":{"tools":[]}}\n'
        "\n"
        'data: {"jsonrpc":"2.0","method":"notifications/progress","params":{}}\n'
        "\n"
        "data: [DONE]\n"
    )
    msgs = mcp._parse_sse_messages(body)
    assert len(msgs) == 2
    assert msgs[0]["id"] == 1
    assert msgs[1]["method"] == "notifications/progress"


def test_parse_sse_messages_handles_batch_data():
    body = 'data: [{"jsonrpc":"2.0","id":1,"result":{}},{"jsonrpc":"2.0","id":2,"result":{}}]\n'
    msgs = mcp._parse_sse_messages(body)
    assert [m["id"] for m in msgs] == [1, 2]


def test_parse_sse_skips_non_json():
    body = "data: not json\n" "data: {broken\n"
    assert mcp._parse_sse_messages(body) == []


# ── 组 3：HTTP 客户端（MockTransport 桩一个远端 server）───────


def _make_http_client(handler) -> httpx.AsyncClient:
    """把 httpx.MockTransport 包成 AsyncClient（与 client.py 的注入同一纪律）。"""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)


def _rpc_result(msg, result):
    return {"jsonrpc": "2.0", "id": msg.get("id"), "result": result}


def _http_server_handler(request: httpx.Request) -> httpx.Response:
    """一个最小的 streamable-HTTP MCP server（用 httpx.MockTransport 桩）。

    - initialize：回 application/json + `Mcp-Session-Id` 头；
    - notifications/initialized：202 无 body；
    - tools/list：回 SSE（text/event-stream），验证 SSE 路径；
    - tools/call：回 application/json，验证 JSON 路径。
    """
    payload = json.loads(request.content.decode())
    method = payload.get("method")
    # 验证会话标识被后续请求带回
    if method != "initialize":
        assert request.headers.get("Mcp-Session-Id") == "sess-123"

    if method == "initialize":
        body = {
            "jsonrpc": "2.0",
            "id": payload["id"],
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "serverInfo": {"name": "fake-http", "version": "1.0"},
            },
        }
        return httpx.Response(
            200,
            json=body,
            headers={
                "content-type": "application/json",
                "Mcp-Session-Id": "sess-123",
            },
        )
    if method == "notifications/initialized":
        return httpx.Response(202)
    if method == "tools/list":
        msg = _rpc_result(
            payload,
            {
                "tools": [
                    {"name": "echo", "description": "回显", "inputSchema": {"type": "object"}},
                    {"name": "boom", "description": "失败", "inputSchema": {"type": "object"}},
                ]
            },
        )
        sse = f'event: message\ndata: {json.dumps(msg)}\n\n'
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})
    if method == "tools/call":
        args = (payload.get("params") or {}).get("arguments") or {}
        msg = _rpc_result(
            payload,
            {
                "content": [{"type": "text", "text": "echo:" + str(args.get("text", ""))}],
                "isError": False,
            },
        )
        return httpx.Response(200, json=msg, headers={"content-type": "application/json"})
    # 未知方法
    return httpx.Response(
        200,
        json={"jsonrpc": "2.0", "id": payload.get("id"), "error": {"code": -32601, "message": "no"}},
        headers={"content-type": "application/json"},
    )


@pytest.mark.asyncio
async def test_http_client_initialize_list_and_call():
    client = _make_http_client(_http_server_handler)
    try:
        cfg = mcp.McpServerConfig(
            name="ctf2", type="http", url="https://ctf2.example/mcp"
        )
        async with mcp.McpClient(cfg, http_client=client) as c:
            tools = await c.list_tools()
            assert [t["name"] for t in tools] == ["echo", "boom"]
            result = await c.call_tool("echo", {"text": "hi"})
            assert result["content"][0]["text"] == "echo:hi"
            # initialize 抓到了 session id
            assert c._session_id == "sess-123"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_http_client_error_becomes_mcperror():
    """server 回 JSON-RPC error → 转 McpError（带中文 message）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        if payload.get("method") == "initialize":
            return httpx.Response(
                200,
                json=_rpc_result(payload, {"protocolVersion": "2024-11-05", "capabilities": {}}),
                headers={"content-type": "application/json"},
            )
        if payload.get("method") == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "error": {"code": -32000, "message": "boom happened"},
            },
            headers={"content-type": "application/json"},
        )

    client = _make_http_client(handler)
    try:
        cfg = mcp.McpServerConfig(name="x", type="http", url="https://h/mcp")
        async with mcp.McpClient(cfg, http_client=client) as c:
            with pytest.raises(mcp.McpError) as ei:
                await c.list_tools()
            assert "boom happened" in str(ei.value)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_http_client_http_error_status_becomes_mcperror():
    """server 回 HTTP 500 → 转成一句可读的 McpError（带上 body 摘要）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        if payload.get("method") == "initialize":
            return httpx.Response(
                200,
                json=_rpc_result(payload, {"protocolVersion": "2024-11-05"}),
                headers={"content-type": "application/json"},
            )
        if payload.get("method") == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(500, text="internal server error", headers={"content-type": "text/plain"})

    client = _make_http_client(handler)
    try:
        cfg = mcp.McpServerConfig(name="x", type="http", url="https://h/mcp")
        async with mcp.McpClient(cfg, http_client=client) as c:
            with pytest.raises(mcp.McpError) as ei:
                await c.list_tools()
            assert "500" in str(ei.value)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_http_registry_describe_and_call():
    """McpRegistry 走 http 连接也能 describe / call（端到端，全 mock）。"""
    client = _make_http_client(_http_server_handler)
    try:
        cfg = mcp.McpServerConfig(name="ctf2", type="http", url="https://ctf2.example/mcp")
        reg = mcp.McpRegistry((cfg,))
        # ★ 注意：McpRegistry 内部 new McpClient(cfg) 不带 http_client；
        # 这里换成直接构造 registry 但通过 patch 注入 client 的方式验证。
        # 由于 Registry 每次 new McpClient，我们把 factory 挂到 cfg 上不可行，
        # 改用 monkeypatch mcp.McpClient 的默认 http_client。
        import app.kernel.mcp as mcp_mod

        orig_init = mcp_mod.McpClient.__init__

        def patched_init(self, cfg, *, http_client=None):
            orig_init(self, cfg, http_client=client)

        mcp_mod.McpClient.__init__ = patched_init
        try:
            text = await reg.describe("ctf2")
            assert "echo" in text
            out = await reg.call("ctf2", "echo", {"text": "hi"})
            assert "echo:hi" in out
        finally:
            mcp_mod.McpClient.__init__ = orig_init
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_stdio_client_still_spawns_subprocess():
    """★ 不回归：stdio 连接仍按 command 起子进程、走管道（不碰 http 分支）。"""
    import sys

    fake = (
        "import json,sys\n"
        "for line in sys.stdin:\n"
        "    line=line.strip()\n"
        "    if not line: continue\n"
        "    m=json.loads(line); mid=m.get('id'); meth=m.get('method')\n"
        "    if meth=='initialize':\n"
        "        sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':mid,'result':{'protocolVersion':'2024-11-05','capabilities':{}}})+'\\n'); sys.stdout.flush()\n"
        "    elif meth=='notifications/initialized': pass\n"
        "    elif meth=='tools/list':\n"
        "        sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':mid,'result':{'tools':[{'name':'ping'}]}})+'\\n'); sys.stdout.flush()\n"
    )
    cfg = mcp.McpServerConfig(name="s", command=sys.executable, args=["-c", fake])
    async with mcp.McpClient(cfg) as c:
        tools = await c.list_tools()
    assert [t["name"] for t in tools] == ["ping"]


# ── 组 4：service 层（保存 / 试连）—— 走 http 类型 ─────────────


def test_save_mcp_servers_persists_http_type_url_headers(db_session):
    """★ 核心：保存 http 连接必须落 type/url/headers。

    老版本 `save_mcp_servers` 只落 name/command/args/env/notes/allowed_tools/
    call_timeout —— type/url/headers 全丢，于是 http 连接存进去就退化成 stdio
    （type 缺→默认 stdio，url 缺→连不上）。这是"我加了 http 但没生效"的直接原因。
    """
    from app.kernel import service

    saved = service.save_mcp_servers(
        db_session,
        [
            {
                "name": "ctf2-suvex",
                "type": "http",
                "url": "https://ctf2.dasctf.com/api/ai/v1/mcp",
                "headers": {"Authorization": "Bearer tok"},
                "notes": "dasctf 平台",
            }
        ],
    )
    # 公开视图：type/url 回显，headers 只回 has_headers
    assert saved[0]["type"] == "http"
    assert saved[0]["url"] == "https://ctf2.dasctf.com/api/ai/v1/mcp"
    assert saved[0]["has_headers"] is True
    assert "headers" not in saved[0]
    # 直接读库，确认 headers 真的落了（只是不回显）
    row = service.get_ctf2_config(db_session)
    assert row.mcp_servers[0]["type"] == "http"
    assert row.mcp_servers[0]["headers"] == {"Authorization": "Bearer tok"}


def test_save_http_without_url_rejected(db_session):
    """http 连接缺 url → 400（不能存一条连不上的连接）。"""
    import pytest as _pytest

    from app.core.errors import ApiError
    from app.kernel import service

    with _pytest.raises(ApiError):
        service.save_mcp_servers(db_session, [{"name": "x", "type": "http"}])


def test_save_stdio_without_command_rejected(db_session):
    """stdio 连接缺 command → 400（不回归）。"""
    import pytest as _pytest

    from app.core.errors import ApiError
    from app.kernel import service

    with _pytest.raises(ApiError):
        service.save_mcp_servers(db_session, [{"name": "x", "type": "stdio"}])


def test_save_http_preserve_headers_keeps_old(db_session):
    """preserve_headers=True 时保留旧 headers（编辑不丢凭据，与 env 对称）。"""
    from app.kernel import service

    service.save_mcp_servers(
        db_session,
        [{"name": "x", "type": "http", "url": "https://h/mcp", "headers": {"A": "1"}}],
    )
    # 再存一次：带 preserve_headers 且 headers 为空 → 旧值应保留
    service.save_mcp_servers(
        db_session,
        [
            {
                "name": "x",
                "type": "http",
                "url": "https://h/mcp",
                "headers": {},
                "preserve_headers": True,
            }
        ],
    )
    row = service.get_ctf2_config(db_session)
    assert row.mcp_servers[0]["headers"] == {"A": "1"}


async def test_test_mcp_server_http_requires_url():
    """试连 http 连接缺 url → ok:False + 明确原因（不是"没有填 command"）。"""
    from app.kernel import service

    res = await service.test_mcp_server({"name": "x", "type": "http"})
    assert res["ok"] is False
    assert "url" in res["reason"]


async def test_test_mcp_server_http_connects(monkeypatch):
    """试连 http 连接真走 http 客户端握手成功（mock httpx）。"""
    from app.kernel import mcp as mcp_mod
    from app.kernel import service

    client = _make_http_client(_http_server_handler)
    orig_init = mcp_mod.McpClient.__init__

    def patched_init(self, cfg, *, http_client=None):
        orig_init(self, cfg, http_client=client)

    monkeypatch.setattr(mcp_mod.McpClient, "__init__", patched_init)
    try:
        res = await service.test_mcp_server(
            {
                "name": "ctf2",
                "type": "http",
                "url": "https://ctf2.example/mcp",
            }
        )
    finally:
        await client.aclose()
    assert res["ok"] is True, res
    assert "echo" in res["tools"]


# ── 组 5：engine 工具层 `_tool_add_mcp_server` 的 http 分支 ──────


class _AdminIdentity:
    scopes = frozenset({"read", "scan", "admin"})

    def can(self, scope: str) -> bool:
        return scope in self.scopes


class _Ctx:
    """`_ToolContext` 的最小替身（加 mcp 工具只用到 db + identity）。"""

    def __init__(self, db):
        self.db = db
        self.session = None
        self.identity = _AdminIdentity()


async def test_add_mcp_server_http_saves_connection(db_session, monkeypatch):
    """★ 用户截图那条路径：type=http + url 就该能加进来（不再被 npx 引偏）。"""
    from app.chat import engine
    from app.kernel import mcp as mcp_mod
    from app.kernel import service

    client = _make_http_client(_http_server_handler)
    orig_init = mcp_mod.McpClient.__init__

    def patched_init(self, cfg, *, http_client=None):
        orig_init(self, cfg, http_client=client)

    monkeypatch.setattr(mcp_mod.McpClient, "__init__", patched_init)
    try:
        out = await engine._tool_add_mcp_server(
            _Ctx(db_session),
            {
                "name": "ctf2-suvex",
                "type": "http",
                "url": "https://ctf2.dasctf.com/api/ai/v1/mcp",
            },
        )
    finally:
        await client.aclose()
    # 成功回执（不是"失败："）
    assert not out.startswith("失败"), out
    assert "已添加" in out
    # 真的落了库，且 type=http
    rows = service.get_mcp_servers(db_session)
    match = [r for r in rows if r.get("name") == "ctf2-suvex"]
    assert match and match[0]["type"] == "http"
    assert match[0]["url"] == "https://ctf2.dasctf.com/api/ai/v1/mcp"


async def test_add_mcp_server_http_missing_url_errors(db_session):
    """http 但没 url → 明确报错，不落库。"""
    from app.chat import engine

    out = await engine._tool_add_mcp_server(
        _Ctx(db_session), {"name": "x", "type": "http"}
    )
    assert out.startswith("失败")
    assert "url" in out
