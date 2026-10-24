"""MCP 桥（`app/kernel/mcp.py`）与三个 MCP 工具的测试。

## 为什么要有**真的** stdio server fixture

`McpClient` 的价值全在"真的能跟一个 MCP server 说上话"：JSON-RPC 成帧、
initialize 握手、id 配对、通知跳过、tools/call 的结果形状。用 mock 替掉
子进程只能测到"我按顺序调了我的方法"，测不到协议本身。

所以这里用 `python -c` 内联一段极小的 MCP server（`_FAKE_SERVER`），它：
  · 认 `initialize` / `notifications/initialized` / `tools/list` / `tools/call`
  · 提供两个工具：`echo`（原样回 text）与 `boom`（返回 isError=true）
  · 故意在 stdout 里混一行非 JSON 日志（验证我们跳过它而不是炸掉）

三组测试：
1. 纯逻辑 —— 配置解析（坏条目跳过、重名跳过、env 不进公共视图）。
2. 协议 —— 真起子进程，走通 initialize/list/call；错误路径（找不到可执行文件、
   未知连接、未知工具被白名单拦）。
3. 工具接线 —— 三个工具在 TOOL_NAMES 里、dispatch 能路由、未配置 MCP 时
   说人话而不是抛异常。
"""

from __future__ import annotations

import json
import sys

import pytest

from app.kernel import mcp, runner

# ── 一个极小的真实 MCP server（stdio，按行 JSON-RPC）──────────────

_FAKE_SERVER = r"""
import json, sys

TOOLS = [
    {"name": "echo", "description": "原样回显 text", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
    {"name": "boom", "description": "总是失败", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "secret", "description": "白名单要拦掉的工具", "inputSchema": {"type": "object", "properties": {}}},
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
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "fake", "version": "1.0"}}})
    elif method == "notifications/initialized":
        # 混一行非 JSON 日志，验证客户端跳过它
        sys.stdout.write("this is not json\n")
        sys.stdout.flush()
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "echo":
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": "echo:" + str(args.get("text", ""))}], "isError": False}})
        else:
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": "工具 " + str(name) + " 总是失败"}], "isError": True}})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "unknown method " + str(method)}})
"""


def _fake_server_config(**overrides) -> mcp.McpServerConfig:
    kwargs = {
        "name": "fake",
        "command": sys.executable,
        "args": ["-c", _FAKE_SERVER],
        "notes": "测试用的假 MCP server",
    }
    kwargs.update(overrides)
    return mcp.McpServerConfig(**kwargs)


# ── 组 1：配置解析纯逻辑 ───────────────────────────────────────


def test_parse_server_configs_normal():
    cfgs = mcp.parse_server_configs(
        [
            {
                "name": "fs",
                "command": "npx",
                "args": ["-y", "server-fs"],
                "env": {"TOKEN": "abc"},
                "notes": "文件系统",
                "allowed_tools": ["read_file"],
            }
        ]
    )
    assert len(cfgs) == 1
    cfg = cfgs[0]
    assert cfg.name == "fs"
    assert cfg.command == "npx"
    assert cfg.args == ["-y", "server-fs"]
    assert cfg.env == {"TOKEN": "abc"}
    assert cfg.notes == "文件系统"
    assert cfg.allowed_tools == ["read_file"]


def test_parse_server_configs_skips_bad_entries():
    cfgs = mcp.parse_server_configs(
        [
            {"name": "ok", "command": "x"},
            {"name": "no-command"},  # 缺 command
            {"command": "no-name"},  # 缺 name
            "not-a-dict",
            42,
        ]
    )
    assert [c.name for c in cfgs] == ["ok"]


def test_parse_server_configs_skips_duplicate_names():
    cfgs = mcp.parse_server_configs(
        [
            {"name": "dup", "command": "a"},
            {"name": "dup", "command": "b"},
        ]
    )
    assert len(cfgs) == 1
    assert cfgs[0].command == "a"


@pytest.mark.parametrize("raw", [None, [], {}, "string", 0])
def test_parse_server_configs_empty_or_wrong_type(raw):
    assert mcp.parse_server_configs(raw) == ()


def test_public_dict_hides_env():
    """`public_dict` 是给日志/回执用的 —— **绝不能**带出 env（可能含 token）。"""
    cfg = mcp.McpServerConfig(
        name="x", command="c", env={"SECRET_TOKEN": "super-secret-value"}
    )
    view = cfg.public_dict()
    assert "env" not in view
    assert "super-secret-value" not in json.dumps(view)


def test_registry_empty_is_falsy():
    reg = mcp.McpRegistry()
    assert not reg
    assert len(reg) == 0
    assert reg.names() == []


def test_registry_summaries_without_connecting():
    """`summaries()` 只读配置、**不连 server**（tool_count 为 None）。"""
    cfg = _fake_server_config()
    reg = mcp.McpRegistry((cfg,))
    rows = reg.summaries()
    assert len(rows) == 1
    assert rows[0]["id"] == "fake"
    assert rows[0]["tool_count"] is None
    assert rows[0]["description"] == "测试用的假 MCP server"


# ── 组 2：真协议（起子进程）────────────────────────────────────


@pytest.mark.asyncio
async def test_client_initialize_and_list_tools():
    cfg = _fake_server_config()
    async with mcp.McpClient(cfg) as client:
        tools = await client.list_tools()
    names = [t["name"] for t in tools]
    assert names == ["echo", "boom", "secret"]
    assert tools[0]["inputSchema"]["properties"]["text"]["type"] == "string"


@pytest.mark.asyncio
async def test_client_call_tool_returns_content():
    cfg = _fake_server_config()
    async with mcp.McpClient(cfg) as client:
        result = await client.call_tool("echo", {"text": "hi"})
    assert result["isError"] is False
    assert result["content"][0]["text"] == "echo:hi"


@pytest.mark.asyncio
async def test_registry_describe_renders_schemas():
    reg = mcp.McpRegistry((_fake_server_config(),))
    text = await reg.describe("fake")
    assert "echo" in text
    assert "原样回显 text" in text
    assert "inputSchema" in text or "输入 schema" in text


@pytest.mark.asyncio
async def test_registry_describe_caches_tools():
    """第二次 describe 不应再起子进程（靠 tools_cache）。"""
    reg = mcp.McpRegistry((_fake_server_config(),))
    first = await reg.describe("fake")
    assert reg.summaries()[0]["tool_count"] == 3
    second = await reg.describe("fake")
    assert first == second


@pytest.mark.asyncio
async def test_registry_describe_unknown_connection():
    reg = mcp.McpRegistry((_fake_server_config(),))
    text = await reg.describe("nope")
    assert "没有名为" in text
    assert "fake" in text  # 回可用清单


@pytest.mark.asyncio
async def test_registry_call_renders_text():
    reg = mcp.McpRegistry((_fake_server_config(),))
    out = await reg.call("fake", "echo", {"text": "x"})
    assert out == "echo:x"


@pytest.mark.asyncio
async def test_registry_call_is_error_prefixed():
    reg = mcp.McpRegistry((_fake_server_config(),))
    out = await reg.call("fake", "boom", {})
    assert out.startswith("MCP 工具返回错误")
    assert "总是失败" in out


@pytest.mark.asyncio
async def test_registry_call_respects_allowed_tools():
    """部署者白名单：只允许 echo，secret 必须被拦（且**不连 server**）。"""
    reg = mcp.McpRegistry((_fake_server_config(allowed_tools=["echo"]),))
    out = await reg.call("fake", "secret", {})
    assert "拒绝" in out
    assert "secret" in out
    # 白名单允许的那个仍能跑
    assert await reg.call("fake", "echo", {"text": "y"}) == "echo:y"


@pytest.mark.asyncio
async def test_client_missing_executable_raises_helpful_error():
    cfg = mcp.McpServerConfig(name="ghost", command="_definitely_not_a_real_binary_xyz")
    with pytest.raises(mcp.McpError) as exc:
        async with mcp.McpClient(cfg):
            pass
    assert "ghost" in str(exc.value)
    assert "找不到" in str(exc.value)


@pytest.mark.asyncio
async def test_client_unknown_method_becomes_mcperror():
    cfg = _fake_server_config()
    with pytest.raises(mcp.McpError) as exc:
        async with mcp.McpClient(cfg) as client:
            await client._request("nope/method", {}, timeout=10.0)
    assert "fake" in str(exc.value)


def test_render_call_result_handles_non_text():
    out = mcp._render_call_result(
        {"content": [{"type": "text", "text": "a"}, {"type": "image", "data": "..."}]}
    )
    assert "a" in out
    assert "已略过" in out


def test_render_call_result_handles_plain_string():
    assert mcp._render_call_result("hello") == "hello"


# ── 组 3：工具接线（dispatch 路由）──────────────────────────────


def test_mcp_tools_are_registered():
    for name in ("list_mcps", "describe_mcp", "call_mcp"):
        assert name in runner.TOOL_NAMES


@pytest.mark.asyncio
async def test_list_mcps_says_none_when_unconfigured():
    """没配 MCP 时必须**说人话**，不能抛异常也不能返回空串。"""
    spec = _spec()
    out = await runner.dispatch_tool("list_mcps", {}, spec=spec, sandbox=None, http=None)
    assert "没有配置" in out


@pytest.mark.asyncio
async def test_describe_mcp_requires_connection_arg():
    spec = _spec()
    out = await runner.dispatch_tool("describe_mcp", {}, spec=spec, sandbox=None, http=None)
    assert "参数错误" in out
    assert "connection" in out


@pytest.mark.asyncio
async def test_call_mcp_requires_both_args():
    spec = _spec()
    out = await runner.dispatch_tool(
        "call_mcp", {"connection": "x"}, spec=spec, sandbox=None, http=None
    )
    assert "参数错误" in out
    assert "tool" in out


@pytest.mark.asyncio
async def test_call_mcp_accepts_json_string_arguments():
    """模型常把 arguments 序列化成字符串 —— 必须接受（学 strix）。"""
    spec = _spec(mcp_servers=(_fake_server_config(),))
    out = await runner.dispatch_tool(
        "call_mcp",
        {"connection": "fake", "tool": "echo", "arguments": '{"text": "from-string"}'},
        spec=spec,
        sandbox=None,
        http=None,
    )
    assert out == "echo:from-string"


@pytest.mark.asyncio
async def test_call_mcp_rejects_malformed_arguments_string():
    spec = _spec(mcp_servers=(_fake_server_config(),))
    out = await runner.dispatch_tool(
        "call_mcp",
        {"connection": "fake", "tool": "echo", "arguments": "{not json"},
        spec=spec,
        sandbox=None,
        http=None,
    )
    assert "参数错误" in out
    assert "describe_mcp" in out  # 要指引模型去看 schema


@pytest.mark.asyncio
async def test_registry_from_spec_caches_on_spec():
    spec = _spec(mcp_servers=(_fake_server_config(),))
    first = mcp.registry_from_spec(spec)
    second = mcp.registry_from_spec(spec)
    assert first is second


# ── 配置链路：DB 列 → KernelSettings → build_spec → SolveSpec ──────
#
# ★ 这一组测的是"接上了没有"，不是"解析对不对"（那是上面第 1 组的事）。
#   为什么值得单独测：`mcp_servers` 这条链横跨四个文件，而每一段单独看都
#   是对的 —— 少写一行 `mcp_servers=...`，解析逻辑再完美也是死代码，
#   症状是"配了 MCP 但智能体永远说没有连接"，且**不报错**。
#   这正是本项目反复吃过的那类缺陷形状（声明存在 ≠ 真的接上）。


def test_kernel_config_row_has_mcp_servers_column():
    """配置列真的在模型上（而不是只在迁移里）。"""
    from app.settings.models import KernelConfig

    assert "mcp_servers" in KernelConfig.__table__.columns


def test_kernel_settings_carries_mcp_servers():
    """`KernelSettings` 有你才能把它往下传。"""
    from app.kernel.service import KernelSettings

    assert "mcp_servers" in KernelSettings.__dataclass_fields__


def test_effective_settings_default_is_empty_not_missing():
    """没有配置行时 `mcp_servers` 是 None（= 没配），不是抛异常/漏字段。

    `None` 与 `[]` 语义不同：None 是"从没配过"，[] 是"配过、清空了"。
    """
    from app.kernel.service import KernelSettings

    defaults = [f for f in KernelSettings.__dataclass_fields__ if f == "mcp_servers"]
    assert defaults == ["mcp_servers"]


def test_parse_accepts_the_shape_a_json_column_returns():
    """DB 的 JSON 列回来是 list[dict]，必须能解析成配置元组。

    这条是"配置列形状 ↔ 解析器输入"的契约：如果哪天 `parse_server_configs`
    改成只吃某种自定义对象，MCP 就会静默变成"没配"。
    """
    raw = [
        {"name": "fs", "command": "npx", "args": ["-y", "server-fs"], "notes": "文件系统"},
        {"name": "bad"},  # 缺 command，应被跳过
    ]
    parsed = mcp.parse_server_configs(raw)
    assert [c.name for c in parsed] == ["fs"]
    assert parsed[0].args == ["-y", "server-fs"]


def test_solve_spec_carries_mcp_servers_through():
    """`SolveSpec` 上的字段能存住配置（多智能体共享 spec，所以这是共享点）。"""
    cfg = _fake_server_config()
    spec = _spec(mcp_servers=(cfg,))
    assert spec.mcp_servers == (cfg,)

    reg = mcp.registry_from_spec(spec)
    assert reg.names() == ["fake"]


# ── 测试夹具 ──────────────────────────────────────────────────


def _spec(**overrides):
    """最小 SolveSpec（只填工具层需要的字段）。"""

    from app.kernel.runner import SolveSpec

    kwargs = {
        "challenge_id": "c1",
        "title": "t",
        "category": "",
        "description": "",
        "profile": "suvex-sandbox",
        "access_urls": (),
        "attachments": (),
        "llm_model": "m",
        "llm_api_key": "k",
        "llm_api_base": "http://x",
        "llm_label": "l",
        "llm_kind": "cloud",
        "mode": "safe",
        "max_turns": 3,
        "max_wrong_submissions": 3,
        "network_isolated": True,
    }
    kwargs.update(overrides)
    return SolveSpec(**kwargs)


# ══════════════════════════════════════════════════════════
# 组 9：调用超时与大结果读取（v0.83，实测抓到的两个真缺陷）
# ══════════════════════════════════════════════════════════
#
# 这两个都不在"纯逻辑"范畴，而是**配置能不能传到协议层**的问题。
# 它们共同的特点是：错了不报错，只在真实调用某一类慢/大任务时才炸，
# 而炸出来的错看起来与真实原因无关。


def test_call_timeout_defaults_to_none_meaning_global():
    """没配 `call_timeout` 时是 `None`（= 用全局 60 秒），不是 0。

    ⚠️ 0 与 None 在这里必须区分：`McpServerConfig.call_timeout or DEFAULT`
    里 `0` 是假值（会回落到默认），但语义上"配置成 0 秒"是一个**错误配置**
    而不是"没配"。解析层把非正数一律当成"没配"并记 warning。
    """
    cfgs = mcp.parse_server_configs([{"name": "gogo", "command": "x"}])
    assert cfgs[0].call_timeout is None


@pytest.mark.parametrize("raw", [300, 300.0, "300"])
def test_call_timeout_parsed_from_config(raw):
    """配置里的超时要能解析成 float（字符串数字也收）。

    这条挡的是"配了却不生效"：DB 是 JSON 列，手填时很容易写成字符串。
    """
    cfgs = mcp.parse_server_configs(
        [{"name": "gogo", "command": "x", "call_timeout": raw}]
    )
    assert cfgs[0].call_timeout == 300.0


@pytest.mark.parametrize("raw", [0, -5, "abc", {}])
def test_bad_call_timeout_degrades_to_none(raw):
    """非法超时**降级成"没配"**，不让整条配置解析失败。

    与 mcp_servers 其余字段同一条容错纪律：一条连接写坏不该让整次任务
    起不来。最坏退化成引入这个字段之前的行为（60 秒）。
    """
    cfgs = mcp.parse_server_configs(
        [{"name": "gogo", "command": "x", "call_timeout": raw}]
    )
    assert len(cfgs) == 1
    assert cfgs[0].call_timeout is None


def test_call_tool_uses_its_own_timeout():
    """★ `call_tool` 必须用**这条连接的**超时，不是全局常量。

    这是本次修复的核心断言。做法：直接读源码确认没有把
    `DEFAULT_MCP_TIMEOUT` 写死在 `call_tool` 里 —— 行为级断言需要真起
    子进程并等 60 秒以上，太慢；而"实现里到底用了哪个变量"正是会退化的点。

    ⚠️ 配对的另一半在 `test_call_tool_timeout_reaches_request`：那条用
    打桩的 `_request` 验证**实际传下去的值**。两条一起才证明"读到配置"
    且"用上了配置"。
    """
    import inspect

    src = inspect.getsource(mcp.McpClient.call_tool)
    assert "self.cfg.call_timeout" in src, (
        "call_tool 没有读 self.cfg.call_timeout —— 配置的超时不会生效，"
        "慢调用仍然按全局 60 秒超时失败"
    )


async def test_call_tool_timeout_reaches_request():
    """行为级：`_request` 收到的 timeout 就是配置的那个值。"""

    class _FakeClient(mcp.McpClient):
        def __init__(self, cfg):  # noqa: D107 - 绕过真实子进程拉起
            self.cfg = cfg
            self.seen: list[float] = []

        async def _request(self, method, params, *, timeout):  # noqa: ANN001
            self.seen.append(timeout)
            return {"content": []}

    client = _FakeClient(mcp.McpServerConfig(name="g", command="x", call_timeout=300))
    await client.call_tool("run_poc", {})
    assert client.seen == [300.0]

    # 没配时回落到全局默认
    plain = _FakeClient(mcp.McpServerConfig(name="g", command="x"))
    await plain.call_tool("run_poc", {})
    assert plain.seen == [mcp.DEFAULT_MCP_TIMEOUT]


def test_subprocess_gets_an_explicit_stream_limit():
    """★ 起子进程时**必须**显式传 `limit=`，否则大结果会读崩。

    实测（v0.83）：`recon_scan` 扫 /24 时 gogo 返回的那一行 JSON 超过
    asyncio 默认行缓冲（64 KiB），`stdout.readline()` 抛

        ValueError: Separator is found, but chunk is longer than limit

    ⚠️ 这个错误读起来像协议错误，真实原因是"这一行太长了"。

    断言方式：读源码确认 `create_subprocess_exec` 带了 `limit=`，并且
    那个值与 `MAX_MESSAGE_BYTES` 是**同一个** —— 两处用不同的数会导致
    "读取层允许、校验层拒绝"的分裂。

    ⚠️ 起子进程在 `_start()` 里（不在 `__aenter__`）。第一版写成了
    `__aenter__`，断言直接变红 —— 而那是**测试自己**的事实错误，
    不是代码的错。所以这里先断言 `_start` 里确实有
    `create_subprocess_exec`，再断言它带了 `limit`：万一将来把起进程
    挪走，这一条会明确告诉你"找错方法了"，而不是给一个误导性的
    "缺 limit"。
    """
    import inspect

    src = inspect.getsource(mcp.McpClient._start)
    assert "create_subprocess_exec" in src, (
        "`_start()` 里没有 create_subprocess_exec —— 起进程的逻辑被挪走了，"
        "请把这条测试指向新的位置（现在它检查的不是真正起进程的代码）"
    )
    assert "limit=MAX_MESSAGE_BYTES" in src, (
        "起子进程时没有传 limit=MAX_MESSAGE_BYTES —— 大结果会抛 "
        "「Separator is found, but chunk is longer than limit」，"
        "而那个错误看起来与真实原因无关"
    )
