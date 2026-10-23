"""gogo 工具层（`runner.py` 的三个一等工具 + `gogo_mcp.py` 的纯逻辑）测试。

## 为什么这组测试的重点不是「gogo 能不能扫」

gogo 能不能扫是**那 25MB 二进制**的事，用 pytest 测不了（也测不出什么）。
这里测的是**接上了没有、接错了会不会静默**：

1. **三个前置检查**（`_gogo_via_mcp`）—— 没配 MCP / 配了但名字不对 /
   白名单拦住。这三条对应本项目反复吃过的缺陷形状「声明存在 ≠ 真的接上」：
   漏一条的症状都是「工具看起来在图里、调了却永远失败，且不报错」。
2. **模板规整的正确性** —— 这是本次最硬的一处：不规整的话 13,619 个
   nuclei 模板只有 4,234 个能载入，且失败是**整批的**（`yaml.Unmarshal`
   原子，一个形状不对全不进）。规整函数必须把那一类形状都吃掉。
3. **「0 命中」必须被说成结论** —— 工具静默返空会让模型以为工具坏了，
   于是退化成 `http_request` 一个个猜 URL（实测 208 次调用里 100% 是
   `run_shell`、一个 POC 都没跑过，就是这个退化路径的终点）。

真二进制的端到端验证在 `_tmp/ref/accept_gogo.py`（在 Kali 的沙箱容器里跑，
10/10 通过），不在 pytest 里 —— 那些依赖 25MB 的 gogo 二进制和可达目标。
"""

from __future__ import annotations

import json
import sys

import pytest

from app.kernel import gogo_mcp, mcp, runner


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


def _fake_gogo_config(*, allowed_tools=None, name="gogo") -> mcp.McpServerConfig:
    """指向 `gogo_mcp.py` 自己的 MCP 连接（用测试里那个假 server 更合适）。"""
    return mcp.McpServerConfig(
        name=name,
        command=sys.executable,
        args=["-c", _STUB_SERVER],
        notes="测试用假 gogo MCP server",
        allowed_tools=allowed_tools,
    )


# ── 一个极小的 stdio MCP server，冒充 gogo ──────────────────────
#
# 它不真的扫任何东西，只为验证 runner 侧的接线：能不能把参数送到 server、
# 能不能把 server 的 JSON 原样带回给模型。

_STUB_SERVER = r"""
import json, sys

TOOLS = [
    {"name": "recon_scan", "description": "假扫端口", "inputSchema": {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]}},
    {"name": "run_poc", "description": "假跑 POC", "inputSchema": {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]}},
    {"name": "extract_secrets", "description": "假抽密钥", "inputSchema": {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]}},
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
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "gogo-stub", "version": "0"}}})
    elif method == "notifications/initialized":
        pass
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        # 把收到的参数原样回显，测试据此断言「参数真的送到了 server」
        payload = {"ok": True, "tool": name, "got_args": args, "alive_count": 0}
        send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}], "isError": False}})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "unknown"}})
"""


# ── 组 1：三个工具在 TOOL_NAMES 里 ───────────────────────────────


def test_gogo_tools_are_registered():
    for name in ("recon_scan", "run_poc", "extract_secrets"):
        assert name in runner.TOOL_NAMES, f"{name} 没进 TOOL_NAMES —— 模型看不到它"


def test_gogo_tool_names_constant_matches():
    assert runner.GOGO_TOOL_NAMES == ("recon_scan", "run_poc", "extract_secrets")
    for name in runner.GOGO_TOOL_NAMES:
        assert name in runner.TOOL_NAMES


# ── 组 2：三个前置检查（缺一条就是「配了却静默失效」）───────────


@pytest.mark.asyncio
async def test_gogo_tool_without_any_mcp_config_says_so():
    """没配 MCP：必须说「没配」，并**指出 shell 里的替代路线**。

    不这么做的症状：模型调一次、失败一次、再调一次，最后放弃 —— 而它本来
    用 nmap 也能干活。
    """
    spec = _spec()
    out = await runner.dispatch_tool(
        "recon_scan", {"target": "1.1.1.1"}, spec=spec, sandbox=None, http=None
    )
    assert "没有配置任何 MCP 连接" in out
    assert "run_shell" in out, "必须给出替代路线，否则模型会反复重试"


@pytest.mark.asyncio
async def test_gogo_tool_with_wrong_connection_name_lists_actual():
    """配了别的 server，不是 gogo —— 要说清「你配的是 X」，而不是笼统失败。"""
    other = _fake_gogo_config(name="filesystem")
    spec = _spec(mcp_servers=(other,))
    out = await runner.dispatch_tool(
        "recon_scan", {"target": "1.1.1.1"}, spec=spec, sandbox=None, http=None
    )
    assert "名为 `gogo` 的 MCP 连接" in out
    assert "filesystem" in out, "要报出实际配了什么"


@pytest.mark.asyncio
async def test_gogo_tool_respects_allowed_tools_whitelist():
    """部署白名单拦住的工具，必须**把允许清单回给模型**。"""
    cfg = _fake_gogo_config(allowed_tools=["recon_scan"])
    spec = _spec(mcp_servers=(cfg,))
    out = await runner.dispatch_tool(
        "run_poc", {"target": "1.1.1.1"}, spec=spec, sandbox=None, http=None
    )
    assert "白名单不允许" in out
    assert "recon_scan" in out


# ── 组 3：参数真的送到 server、结果真的带回来 ────────────────────


@pytest.mark.asyncio
async def test_recon_scan_passes_args_through_to_server():
    spec = _spec(mcp_servers=(_fake_gogo_config(),))
    out = await runner.dispatch_tool(
        "recon_scan",
        {"target": "10.0.0.0/24", "ports": "top2", "smart": "s"},
        spec=spec,
        sandbox=None,
        http=None,
    )
    payload = json.loads(out)
    assert payload["tool"] == "recon_scan"
    assert payload["got_args"] == {"target": "10.0.0.0/24", "ports": "top2", "smart": "s"}


@pytest.mark.asyncio
async def test_run_poc_and_extract_secrets_route_to_their_own_tool():
    spec = _spec(mcp_servers=(_fake_gogo_config(),))
    for tool in ("run_poc", "extract_secrets"):
        out = await runner.dispatch_tool(
            tool, {"target": "10.0.0.1:8080"}, spec=spec, sandbox=None, http=None
        )
        assert json.loads(out)["tool"] == tool


# ── 组 4：nuclei → neutron 模板规整（本次最硬的一处）────────────


def test_normalise_converts_reference_string_to_list():
    """nuclei 允许 `reference:` 写裸字符串，neutron 要 []string。

    实测这是 13,619 个模板里**最主要**的一类载入失败
    （`cannot unmarshal !!str into []string`）。
    """
    doc = gogo_mcp.normalise_template({"id": "x", "info": {"reference": "https://a/b\nhttps://c/d"}})
    assert doc["info"]["reference"] == ["https://a/b", "https://c/d"]


def test_normalise_joins_sequence_fields_to_string():
    """`description`/`cwe-id` 等写成 list 时要拼成字符串（neutron 要 scalar）。"""
    doc = gogo_mcp.normalise_template(
        {
            "id": "x",
            "info": {
                "description": ["第一行", "第二行"],
                "classification": {"cwe-id": ["CWE-798", "CWE-799"], "cvss-score": 9.8},
            },
        }
    )
    assert doc["info"]["description"] == "第一行,第二行"
    assert doc["info"]["classification"]["cwe-id"] == "CWE-798,CWE-799"
    # 非 list 的字段（cvss-score 是 float）不能被碰
    assert doc["info"]["classification"]["cvss-score"] == 9.8


def test_normalise_drops_flow_and_renames_requests():
    """`flow` 在 neutron 里没有等价物（留着整批载入失败）；`requests` = `http`。"""
    doc = gogo_mcp.normalise_template(
        {"id": "x", "info": {}, "flow": "http(1) && http(2)", "requests": [{"method": "GET"}]}
    )
    assert "flow" not in doc, "flow 必须删掉，否则整批 unmarshal 失败"
    assert "http" in doc and "requests" not in doc
    assert doc["http"] == [{"method": "GET"}]


def test_normalise_adds_http_tag_so_poc_is_selected():
    """没有 `http` tag 的模板，gogo 的 POC 选取逻辑**不会挑它**。

    这一条是实测踩过的：自己写的 POC 漏了 `tags: http`，`-e` 跑起来
    静默跳过、0 命中，看起来像「模板写错了」。
    """
    doc = gogo_mcp.normalise_template({"id": "x", "info": {"tags": "cve,rce"}})
    assert "http" in doc["info"]["tags"]
    # 已有的 tag 不能丢
    assert "cve" in doc["info"]["tags"] and "rce" in doc["info"]["tags"]


def test_normalise_empty_info_is_created_not_crashed():
    doc = gogo_mcp.normalise_template({"id": "x"})
    assert isinstance(doc["info"], dict)
    assert "http" in doc["info"]["tags"]


def test_convert_templates_rejects_missing_dir_before_yaml_import():
    """目录不存在 → 先报「不是目录」，优先于「缺 PyYAML」。

    顺序反了的话模型看到的是部署问题，于是它不会去改自己写错的路径。
    （这条顺序是实测踩过才调过来的。）
    """
    with pytest.raises(gogo_mcp.ToolArgError) as exc:
        gogo_mcp.convert_templates(["/nope/definitely/not/a/dir"])
    assert "不是目录" in str(exc.value)


# ── 组 5：参数校验（坏输入不能变成一次真的扫描）─────────────────


def test_clean_target_rejects_shell_metacharacters():
    with pytest.raises(gogo_mcp.ToolArgError) as exc:
        gogo_mcp._clean_target("1.2.3.4; rm -rf /")
    assert "不允许的字符" in str(exc.value)


def test_clean_target_accepts_cidr_and_hostport():
    assert gogo_mcp._clean_target("10.0.0.0/24") == "10.0.0.0/24"
    assert gogo_mcp._clean_target("a.example.com:8443") == "a.example.com:8443"


def test_clean_ports_rejects_injection_and_keeps_presets():
    with pytest.raises(gogo_mcp.ToolArgError):
        gogo_mcp._clean_ports("80; id")
    assert gogo_mcp._clean_ports("top2") == "top2"
    assert gogo_mcp._clean_ports("") == "top1"
    assert gogo_mcp._clean_ports("80,443,8000-8100") == "80,443,8000-8100"


def test_split_target_keeps_cidr_intact():
    """CIDR 里的冒号（IPv6）和斜线不能被 rpartition 拆坏。"""
    assert gogo_mcp._split_target("10.0.0.0/24") == ("10.0.0.0/24", "80")
    assert gogo_mcp._split_target("10.0.0.1:8080") == ("10.0.0.1", "8080")
    assert gogo_mcp._split_target("10.0.0.1") == ("10.0.0.1", "80")


# ── 组 6：「0 命中」必须被说成结论 ───────────────────────────────


def test_recon_scan_note_is_present_when_nothing_found(monkeypatch):
    """扫到 0 个端口时，返回里必须有 `note` 说清「这是结论不是故障」。"""
    monkeypatch.setattr(
        gogo_mcp, "_run_gogo", lambda argv, timeout: _cp("", "", 0)
    )
    payload = json.loads(gogo_mcp.tool_recon_scan({"target": "10.0.0.1"}))
    assert payload["alive_count"] == 0
    assert "note" in payload
    assert "结论" in payload["note"]
    # 关键词：不能让模型以为工具坏了
    assert "故障" in payload["note"]


def test_run_poc_says_no_hit_does_not_mean_safe(monkeypatch):
    monkeypatch.setattr(gogo_mcp, "_run_gogo", lambda argv, timeout: _cp("", "", 0))
    payload = json.loads(gogo_mcp.tool_run_poc({"target": "10.0.0.1"}))
    assert payload["hit_count"] == 0
    assert "不等于目标没有漏洞" in payload["note"]


def test_run_poc_carries_the_l1_judgement_hint(monkeypatch):
    """POC 命中的判级提示必须由工具自己带出来，不靠提示词记。"""
    monkeypatch.setattr(gogo_mcp, "_run_gogo", lambda argv, timeout: _cp("", "", 0))
    payload = json.loads(gogo_mcp.tool_run_poc({"target": "10.0.0.1"}))
    assert "L1" in payload["judgement_hint"]
    assert "confirmed" in payload["judgement_hint"]


def test_extract_secrets_merges_and_dedupes_across_records(monkeypatch):
    stdout = (
        '{"extracted": {"ip": ["10.1.1.1", "10.1.1.1"], "jwt": ["eyJ..."]}}\n'
        '{"extracted": {"ip": ["10.1.1.2"]}}\n'
    )
    monkeypatch.setattr(gogo_mcp, "_run_gogo", lambda argv, timeout: _cp(stdout, "", 0))
    payload = json.loads(gogo_mcp.tool_extract_secrets({"target": "10.0.0.1"}))
    assert payload["by_category"]["ip"] == ["10.1.1.1", "10.1.1.2"], "跨记录要合并并去重"
    assert payload["total"] == 3


# ── 组 7：opsec 是默认档（安全档位在代码里，不在提示词里）──────


def test_run_gogo_always_appends_opsec(monkeypatch):
    """`--opsec` 必须**总是**带上 —— 它限速并把 high/medium POC 挡在引擎层。

    这条是「提示词是建议，代码才是约束」的具体体现：不这么做，模型就
    能在参数里把激进模板打开。
    """
    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return _cp("", "", 0)

    monkeypatch.setattr(gogo_mcp.subprocess, "run", fake_run)
    monkeypatch.setattr(gogo_mcp, "resolve_gogo_bin", lambda: "/fake/gogo")
    gogo_mcp._run_gogo(["-i", "10.0.0.1"], timeout=30)
    assert "--opsec" in seen["argv"]


def test_run_gogo_does_not_duplicate_opsec(monkeypatch):
    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return _cp("", "", 0)

    monkeypatch.setattr(gogo_mcp.subprocess, "run", fake_run)
    monkeypatch.setattr(gogo_mcp, "resolve_gogo_bin", lambda: "/fake/gogo")
    gogo_mcp._run_gogo(["-i", "10.0.0.1", "--opsec"], timeout=30)
    assert seen["argv"].count("--opsec") == 1


# ── 组 8：可执行文件解析（只读卷 0644 那个坑）────────────────────


def test_resolve_gogo_bin_runs_a_probe_not_os_access(monkeypatch):
    """必须真的 exec 一次来判定，不能用 `os.access(X_OK)`。

    实测：只读卷挂进来的文件保留宿主 mode（0644），`os.access` 在有读权限
    时返回 True，于是**探测通过、执行才炸**。这里断言探测走的是真 exec。
    """
    calls: list[str] = []

    def fake_probe(path):
        calls.append(path)
        return True

    monkeypatch.setattr(gogo_mcp, "_probe", fake_probe)
    monkeypatch.setattr(gogo_mcp, "_RESOLVED_BIN", None)
    # 带分隔符的绝对路径才会跳过 `shutil.which` 直接进探测分支
    monkeypatch.setattr(gogo_mcp, "GOGO_BIN", "/usr/local/bin/gogo")
    assert gogo_mcp.resolve_gogo_bin() == "/usr/local/bin/gogo"
    assert calls == ["/usr/local/bin/gogo"], "必须真探测一次"


def test_resolve_gogo_bin_handles_which_returning_basename(monkeypatch):
    """`shutil.which` 在 Windows 返回 basename（`gogo.EXE`），仍不含分隔符。

    不记住「这是从 PATH 命中的」就会二次判定成「不在 PATH 上」，
    于是明明装好了却报「找不到 gogo 可执行文件」——典型的静默接错。
    """
    monkeypatch.setattr(gogo_mcp, "_RESOLVED_BIN", None)
    monkeypatch.setattr(gogo_mcp, "GOGO_BIN", "gogo")
    monkeypatch.setattr(gogo_mcp.shutil, "which", lambda name: "gogo.EXE")
    monkeypatch.setattr(gogo_mcp, "_probe", lambda path: True)
    assert gogo_mcp.resolve_gogo_bin() == "gogo.EXE"


def test_resolve_gogo_bin_reports_missing_with_actionable_text(monkeypatch):
    monkeypatch.setattr(gogo_mcp, "_RESOLVED_BIN", None)
    monkeypatch.setattr(gogo_mcp, "GOGO_BIN", "definitely-not-installed-xyz")
    monkeypatch.setattr(gogo_mcp.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError) as exc:
        gogo_mcp.resolve_gogo_bin()
    assert "GOGO_BIN" in str(exc.value)
    assert "PATH" in str(exc.value)


# ── 组 9：MCP 协议层（tools/list 形状）─────────────────────────


def test_tools_list_has_three_tools_with_required_schema():
    names = [t["name"] for t in gogo_mcp.TOOLS]
    assert names == ["recon_scan", "run_poc", "extract_secrets"]
    for tool in gogo_mcp.TOOLS:
        schema = tool["inputSchema"]
        assert schema.get("required") == ["target"], f"{tool['name']} 的 target 必须是必填"
        assert "description" in tool and tool["description"]


def test_handle_call_tool_wraps_argerror_as_text_result():
    """参数错误要变成 isError=true 的文本，**不能抛到协议层**。"""
    reply = gogo_mcp.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "recon_scan", "arguments": {"target": "1.1.1.1; ls"}},
        }
    )
    assert reply["result"]["isError"] is True
    assert "不允许的字符" in reply["result"]["content"][0]["text"]


def test_handle_unknown_tool_lists_available():
    reply = gogo_mcp.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "nope"}}
    )
    assert reply["result"]["isError"] is True
    assert "recon_scan" in reply["result"]["content"][0]["text"]


def test_handle_missing_yaml_names_the_dependency(monkeypatch):
    """缺 PyYAML 要报「缺 pyyaml」而不是「工具执行失败」。"""

    def boom(args):
        raise ModuleNotFoundError("No module named 'yaml'", name="yaml")

    monkeypatch.setitem(gogo_mcp.HANDLERS, "run_poc", boom)
    reply = gogo_mcp.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "run_poc", "arguments": {"target": "1.1.1.1"}},
        }
    )
    text = reply["result"]["content"][0]["text"]
    assert "pyyaml" in text.lower() or "PyYAML" in text


# ── 夹具 ──────────────────────────────────────────────────────


def _cp(stdout: str, stderr: str, code: int):
    import subprocess

    return subprocess.CompletedProcess(args=["gogo"], returncode=code, stdout=stdout, stderr=stderr)
