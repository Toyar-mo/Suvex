"""内置工具清单与开关的接线测试（v0.82）。

## 这一组在钉什么

「工具管理」这个页面能成立的前提是**四段链路都真的接通了**：

    配置列 `disabled_tools` → `KernelSettings/服务层` → `LaunchSpec`
        → `SolveSpec` → `dispatch_tool` 真的拦住

任何一段断开，症状都是**界面上看起来一切正常，但功能静默失效** ——
这正是本项目反复记录的那个形状（`mcp_servers` 在 v0.72 就是这么坏的，
v0.81 扫描路径丢 MCP 配置又是一次）。所以这里逐段钉，而不是只测纯函数。

## 分五组

1. **清单对账** —— `tools.TOOL_SPECS` 与 `runner.TOOL_NAMES` 必须同集合。
   两处各存一份是刻意的（见 `app/kernel/tools.py` 文件头：runner 的
   import 成本太高，router 层不能引它），所以必须有东西盯着它们不漂移。
2. **开关解析** —— 容错、去重、未知名字剔除；空 = 全开。
3. **引擎拦截** —— `dispatch_tool` 对被关掉的工具返回一句话且**不执行**。
4. **配置列 → spec** —— 两条路径（CTF 解题 / 渗透扫描）都要带上。
5. **HTTP 端点** —— `GET` 三态正确、`PUT` 全量替换、未知名字 400、留痕。

## 反面同样重要

大部分用例都有一个"关着/没配时行为逐字节不变"的对照分支。只测
"打开时生效"是测不出"默认值把功能悄悄关掉"的 —— 而那是对称的另一半。
"""

from __future__ import annotations

import inspect

import pytest
from sqlalchemy.orm import Session

from app.core.vocab import AuditAction
from app.kernel import tools as tools_mod

API = "/api/v1"


# ══════════════════════════════════════════════════════════
# 1. 清单对账
# ══════════════════════════════════════════════════════════


def test_catalog_matches_runner_tool_names() -> None:
    """★ 清单与引擎的 `TOOL_NAMES` 必须**同集合同长度**。

    这是本文件最重要的一条：两处各存一份是刻意的取舍（runner 太重，
    router 层不能 import 它），所以漂移必然会发生 —— 除非有东西盯着。

    漂移的两种方向后果不同，都要挡：
      · 清单里**多**一个 → 界面显示一个模型看不到的工具；
      · 清单里**少**一个 → 模型能调、界面管不到（**这一侧更糟**：
        用户以为自己关掉了它，其实没有）。
    """
    from app.kernel import runner

    tools_mod.assert_catalog_consistent(runner.TOOL_NAMES)

    catalog = tuple(spec.name for spec in tools_mod.TOOL_SPECS)
    assert len(catalog) == len(set(catalog)), "清单里有重名工具"
    assert set(catalog) == set(runner.TOOL_NAMES)


def test_catalog_covers_every_group_it_declares() -> None:
    """每个 `group` 都必须是 `TOOL_GROUPS` 里那个字符串的**逐字副本**。

    拼错一个字的症状是界面上那个工具**整栏消失**（分组不存在），
    而它不报错 —— 只是少了一行。
    """
    declared = set(tools_mod.TOOL_GROUPS)
    used = {spec.group for spec in tools_mod.TOOL_SPECS}
    assert used <= declared, f"清单里用了未声明的分组：{sorted(used - declared)}"
    assert used == declared, f"这些分组没有任何工具，界面上会是空栏：{sorted(declared - used)}"


def test_every_spec_has_a_human_label_and_summary() -> None:
    """`label` / `summary` 不能为空 —— 否则界面那一行只剩一个英文标识符。"""
    for spec in tools_mod.TOOL_SPECS:
        assert spec.label.strip(), f"{spec.name} 缺中文显示名"
        assert spec.summary.strip(), f"{spec.name} 缺说明"


def test_gogo_tools_declare_their_connection() -> None:
    """gogo 那三个必须声明 `requires_connection="gogo"`。

    漏声明的后果：界面把它们显示成"就绪"，而模型调起来永远收到
    "没有名为 gogo 的连接"。这与 v0.81 那三个前置检查是同一个判据来源。
    """
    from app.kernel import runner

    for name in runner.GOGO_TOOL_NAMES:
        spec = tools_mod.TOOL_BY_NAME[name]
        assert spec.requires_connection == runner.GOGO_CONNECTION, (
            f"{name} 没声明它依赖 `{runner.GOGO_CONNECTION}` 连接"
        )


# ══════════════════════════════════════════════════════════
# 2. 开关解析
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize("raw", [None, [], ()])
def test_empty_config_means_everything_enabled(raw: object) -> None:
    """★ `None` / `[]` / 空元组都表示"一件都没关"。

    这是**默认行为**：引入这个开关之前的所有部署读到的都是 `None`，
    它们必须拿到全部 13 个工具，否则升级即静默减能力。
    """
    assert tools_mod.normalize_disabled(raw) == ()
    assert len(tools_mod.enabled_tool_names(raw)) == len(tools_mod.TOOL_SPECS)


def test_unknown_and_bad_entries_are_dropped_not_raised() -> None:
    """未知名字与坏类型**跳过**，不抛 —— 一行坏配置不该让所有任务跑不起来。

    ⚠️ 也不静默：`normalize_disabled` 会对未知名字记 warning（见它的说明）。
    这里只断言返回值，因为"记了没记"属于日志细节，钉它会让测试变脆。
    """
    raw = ["run_shell", "run_shell", "ghost_tool", 123, "", "  ", None, "http_request"]
    assert tools_mod.normalize_disabled(raw) == ("run_shell", "http_request")


def test_non_list_config_is_treated_as_nothing_disabled() -> None:
    """配置列被手工改成字符串/字典时按"全开"处理（最坏退化成旧行为）。"""
    assert tools_mod.normalize_disabled("run_shell") == ()
    assert tools_mod.normalize_disabled({"run_shell": True}) == ()


def test_disabled_order_follows_catalog_not_config() -> None:
    """解析结果按**清单顺序**而不是配置里的顺序 —— 提示词里那句
    「可用工具只有 X / Y / Z」的顺序跟着它走，顺序稳定才让日志可比。

    ⚠️ 这一条**曾经写错过期望值**：我以为 `normalize_disabled` 会按
    `TOOL_SPECS` 重排，但它只是"按输入顺序去重"。真正的保序发生在
    `enabled_tool_names`（它按清单遍历）。所以这里钉住的是**两者的
    分工**，而不是一个实现细节：

      · `normalize_disabled` —— 保持调用方给的顺序（配置里的顺序有意义，
        用户是按自己想的顺序关的，回显时应当照原样）；
      · `enabled_tool_names` —— 按清单顺序产出（提示词那行要稳定）。
    """
    # 配置里把 http_request 写在 run_shell 前面 → 原样保留
    assert tools_mod.normalize_disabled(["http_request", "run_shell"]) == (
        "http_request",
        "run_shell",
    )
    # 而"可用清单"永远按声明顺序，与配置顺序无关
    enabled = tools_mod.enabled_tool_names(["http_request", "run_shell"])
    # 声明顺序里 read_attachment 是第一个、list_requests 第五个
    assert enabled[0] == "read_attachment"
    assert enabled.index("read_attachment") < enabled.index("list_requests")
    assert "run_shell" not in enabled and "http_request" not in enabled


def test_enabling_filter_removes_exactly_the_disabled_names() -> None:
    enabled = tools_mod.enabled_tool_names(["run_shell"])
    assert "run_shell" not in enabled
    assert len(enabled) == len(tools_mod.TOOL_SPECS) - 1


# ══════════════════════════════════════════════════════════
# 3. 引擎拦截：真的不执行
# ══════════════════════════════════════════════════════════


def _spec_with(disabled: tuple[str, ...]):
    """造一个最小 `SolveSpec`，只为跑 `dispatch_tool`。"""
    from app.kernel.runner import SolveSpec

    return SolveSpec(
        challenge_id="ch_x",
        title="t",
        category="misc",
        description="d",
        profile="base",
        access_urls=(),
        attachments=(),
        llm_model="m",
        llm_api_key="k",
        llm_api_base="http://gw/v1",
        llm_kind="cloud",
        mode="safe",
        max_turns=10,
        disabled_tools=disabled,
    )


async def test_dispatch_refuses_a_disabled_tool() -> None:
    """★ 被关掉的工具**必须真的不执行**，且回一句能继续走的话。

    ## 为什么要断言"没执行"而不只是"返回了禁用提示"

    只断言返回文本的话，一个"先执行、再把结果换成提示"的实现照样通过
    —— 而那种实现的后果正是我们要防的：用户以为关掉了它。

    这里用 `run_shell` + 一个必然失败的沙箱句柄：如果它真被执行了，
    会走到 sandbox 调用并抛异常/返回另一种错误，文本不会是禁用那句。
    """
    from app.kernel import runner

    spec = _spec_with(("run_shell",))
    result = await runner.dispatch_tool(
        "run_shell", {"command": "id"}, spec=spec, sandbox=None, http=None
    )
    assert "关闭" in result
    assert "run_shell" in result
    # 必须告诉它下一步（"别再试它" + 当前可用清单），否则模型会重试。
    assert "别再试" in result
    assert "http_request" in result


async def test_dispatch_still_runs_when_nothing_disabled() -> None:
    """空 `disabled_tools` 时行为与引入开关之前一致（走正常分发）。

    这条是上一条的**对照组**：它证明"拦住"是开关造成的，
    而不是 `dispatch_tool` 对 `run_shell` 本身做了什么。
    """
    from app.kernel import runner

    spec = _spec_with(())
    # 不传 sandbox → 正常路径会在执行时出错并返回一句"执行失败"，
    # 但**绝不能**是那句"已被关闭"。这就是我们要的区分。
    result = await runner.dispatch_tool(
        "run_shell", {"command": "id"}, spec=spec, sandbox=None, http=None
    )
    assert "关闭" not in result


async def test_dispatch_refuses_every_gogo_tool_too() -> None:
    """gogo 三件套同样可被关闭 —— 它们是 `TOOL_NAMES` 成员。

    这条顺带钉住"关闭"与"没配 MCP"是**两条不同的提示**：
    两者都会让工具不可用，但用户该做的事不同（去开关里打开 vs 去配连接）。
    """
    from app.kernel import runner

    for name in runner.GOGO_TOOL_NAMES:
        spec = _spec_with((name,))
        result = await runner.dispatch_tool(
            name, {"target": "http://x"}, spec=spec, sandbox=None, http=None
        )
        assert "关闭" in result, f"{name} 没被拦住：{result[:80]}"
        # 不应误报成"没配 MCP"
        assert "没有配置任何 MCP 连接" not in result


# ══════════════════════════════════════════════════════════
# 4. 配置列 → spec（两条路径）
# ══════════════════════════════════════════════════════════


def test_solve_spec_has_disabled_tools_field() -> None:
    from app.kernel import runner

    assert "disabled_tools" in runner.SolveSpec.__dataclass_fields__
    assert runner.SolveSpec.__dataclass_fields__["disabled_tools"].default == ()


def test_launch_spec_has_both_fields() -> None:
    """★ `LaunchSpec` 必须有 `mcp_servers` 与 `disabled_tools` 两个字段。

    这是 v0.81 那个缺口的**根**：`mcp_servers` 在 `LaunchSpec` 上根本
    不存在，所以 `build_solve_spec_from_scan` 想传也传不了。
    缺一个字段的症状不是报错，是扫描路径静默丢掉那份配置。
    """
    from app.scans.runner import LaunchSpec

    fields = LaunchSpec.__dataclass_fields__
    assert "mcp_servers" in fields, "LaunchSpec 缺 mcp_servers —— 扫描路径会丢 MCP 配置"
    assert "disabled_tools" in fields
    assert fields["mcp_servers"].default == ()
    assert fields["disabled_tools"].default == ()


def _launch_spec(**overrides):  # noqa: ANN001 - 测试夹具
    from app.scans.runner import LaunchSpec

    base = dict(
        scan_id="scan_x",
        target="http://demo.test:8080",
        target_kind="url",
        skills=("web",),
        brief="",
        goal="findVulns",
        scan_mode="safe",
        scan_max_turns=20,
        run_dir=None,
        log_path=None,
        llm_model="openai/deepseek-v4.1-flash",
        llm_api_key="k",
        llm_api_base="http://gw/v1",
    )
    base.update(overrides)
    return LaunchSpec(**base)


def test_scan_path_carries_disabled_tools() -> None:
    from app.scans.agent_runner import build_solve_spec_from_scan

    spec = build_solve_spec_from_scan(_launch_spec(disabled_tools=("run_shell",)))
    assert tuple(getattr(spec, "disabled_tools", ())) == ("run_shell",)

    spec_off = build_solve_spec_from_scan(_launch_spec())
    assert tuple(getattr(spec_off, "disabled_tools", ())) == ()


def test_scan_path_carries_mcp_servers() -> None:
    """★ 扫描路径必须把 `LaunchSpec.mcp_servers` 透传进 `SolveSpec`。

    这一条盯的是 v0.81 的真实缺口：`kernel_pentest.md` 整段教模型用
    `recon_scan` / `run_poc`，而渗透扫描是主线 —— 配置却从未送到引擎。
    断言用一条假的 `McpServerConfig`，只验**透传**，不验连接。
    """
    from app.kernel import mcp
    from app.scans.agent_runner import build_solve_spec_from_scan

    cfg = mcp.McpServerConfig(name="gogo", command="/usr/local/bin/gogo_mcp.py")
    spec = build_solve_spec_from_scan(_launch_spec(mcp_servers=(cfg,)))
    assert tuple(getattr(spec, "mcp_servers", ())) == (cfg,)

    spec_off = build_solve_spec_from_scan(_launch_spec())
    assert tuple(getattr(spec_off, "mcp_servers", ())) == ()


def test_scan_path_translation_source_mentions_both_fields() -> None:
    """源码级断言：翻译函数里真的写了这两个关键字。

    上一条测的是行为，这一条测的是**没有绕过翻译层**（比如有人改成
    在别处 setattr）。两条都留着的理由：行为断言在默认值恰好相同时
    抓不到"忘了传"，而源码断言抓不到"传错了值"。
    """
    from app.scans import agent_runner

    src = inspect.getsource(agent_runner.build_solve_spec_from_scan)
    assert "mcp_servers=" in src
    assert "disabled_tools=" in src


# ══════════════════════════════════════════════════════════
# 5. HTTP 端点
# ══════════════════════════════════════════════════════════


def test_get_tools_lists_every_tool(ready_client) -> None:  # noqa: ANN001
    """`GET /kernel/tools` 回全部工具，默认全开、无 MCP 时 gogo 未就绪。"""
    resp = ready_client.get(f"{API}/kernel/tools")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    names = {item["name"] for item in body["tools"]}
    assert names == set(tools_mod.TOOL_BY_NAME)
    assert body["groups"] == list(tools_mod.TOOL_GROUPS)
    # 默认：一件都没关
    assert body["disabled"] == []
    assert all(item["enabled"] for item in body["tools"])
    # 没有配 MCP → gogo 三件套未就绪，且**给得出原因**
    assert body["gogoReady"] is False
    assert body["gogoReason"]
    for item in body["tools"]:
        if item["requiresConnection"] == "gogo":
            assert item["ready"] is False
            assert item["reason"]


def test_put_tools_round_trips(ready_client) -> None:  # noqa: ANN001
    """关掉 → 读回关着；清空 → 读回全开。**两向都测**。

    只测"能关"的话，"关不掉"（字段必填但后端忽略空列表）发现不了。
    """
    saved = ready_client.put(f"{API}/kernel/tools", json={"disabled": ["run_shell"]})
    assert saved.status_code == 200, saved.text
    assert saved.json()["disabled"] == ["run_shell"]

    reread = ready_client.get(f"{API}/kernel/tools").json()
    states = {item["name"]: item["enabled"] for item in reread["tools"]}
    assert states["run_shell"] is False
    assert states["http_request"] is True

    cleared = ready_client.put(f"{API}/kernel/tools", json={"disabled": []})
    assert cleared.json()["disabled"] == []
    assert all(item["enabled"] for item in cleared.json()["tools"])


def test_put_tools_rejects_unknown_names(ready_client) -> None:  # noqa: ANN001
    """★ 未知工具名必须 400，不能静默接受。

    接受它的后果最隐蔽：用户以为关掉了某样东西、实际没有，而界面上
    那一栏显示"已关闭"（因为回执原样回显了他提交的名字）。
    """
    resp = ready_client.put(f"{API}/kernel/tools", json={"disabled": ["run_shelll"]})
    assert resp.status_code in (400, 422), resp.text
    # 错误里要给出可用名字，否则用户只能来问我们
    assert "run_shell" in resp.text


def test_put_tools_is_full_replacement_not_merge(ready_client) -> None:  # noqa: ANN001
    """全量替换：提交只有 B 的清单，先前关掉的 A 必须**恢复可用**。

    这条挡的是"实现成增量合并"—— 那会让"打开一个被关掉的工具"变成
    无法通过这个接口完成的动作（只能再提交一次全量，而用户不知道要带上谁）。
    """
    ready_client.put(f"{API}/kernel/tools", json={"disabled": ["run_shell"]})
    resp = ready_client.put(f"{API}/kernel/tools", json={"disabled": ["http_request"]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["disabled"] == ["http_request"]

    states = {i["name"]: i["enabled"] for i in resp.json()["tools"]}
    assert states["run_shell"] is True
    assert states["http_request"] is False


def test_put_tools_is_audited(ready_client, db_session: Session) -> None:  # noqa: ANN001
    """★ 改开关必须留痕。

    它的后果是"能力边界变了"，而失败现场（模型突然不会用某工具）
    看起来像模型变笨，不像配置被改过 —— 所以必须能回答"谁改的"。
    """
    from app.audit.models import AuditLog

    ready_client.put(f"{API}/kernel/tools", json={"disabled": ["run_shell"]})
    rows = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == AuditAction.KERNEL_TOOLS_UPDATE.value)
        .all()
    )
    assert rows, "更新工具开关没有写审计"
    assert rows[-1].detail


def test_tools_get_does_not_leak_secrets(ready_client) -> None:  # noqa: ANN001
    """清单里不能出现 MCP 的 `env`，也不能有凭据字段。

    这个端点是 read 级（比 MCP 清单低一级），所以更不能带凭据 ——
    一个只读令牌能看到别人的 token，那就不是 read 级了。

    ⚠️ 断言的是 **JSON 键名**，不是整段文本：`extract_secrets` 这个
    工具名本身就含 "secret"，拿整段文本去搜会得到一个假阳性
    （第一版就是这么红的）。**检查要匹配结构，不要匹配内容。**
    """
    import json

    body = json.loads(ready_client.get(f"{API}/kernel/tools").text)

    # 递归收集所有字典键名
    keys: set[str] = set()

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                keys.add(str(key).lower())
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(body)
    for needle in ("env", "token", "secret", "password", "apikey"):
        assert not any(needle in key for key in keys), (
            f"工具清单里出现了可疑**字段**：{needle}（实际键名：{sorted(keys)}）"
        )
