"""`browser` 工具的守卫测试。

## 这个文件守的是什么

这份测试的**每一条都对应一个已实测确认的失败模式**，不是"覆盖率"。
理由：`browser` 是一个**宿主侧翻译层** —— 它把 agent-browser 的文本
输出翻译成模型能行动的指令。翻译层最容易出的错不是崩，而是
**说错话**（比如截图后不告诉模型它看不到图）。所以这里大量断言
落在**返回的字符串里有没有那句话**上。

三个来源：
1. 本机实测（Windows + Chrome，2026-10-26）——
   退出码、stderr 位置、snapshot 输出格式；
2. `app/kernel/browser.py` 模块 docstring 里的三条约束；
3. `docker/suvex-sandbox/Dockerfile` 里 agent-browser 那一段的 ENV。

## 为什么用假 sandbox 而不是真 docker

宿主没有 docker（本项目一直如此），而 `SandboxContainer` 的契约很小：
只需要 `shell()` 和 `copy_in()`。用假对象能断言"发了哪条命令"，
这比"跑通了"更有价值 —— 我们要验的是**命令对不对**，
不是 agent-browser 本身（那是它自己的测试）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from app.kernel import browser as browser_mod
from app.kernel.browser import BIN, SUBCOMMANDS, WORK_ROOT, run_browser
from app.kernel.runner import TOOL_NAMES
from app.kernel.tools import TOOL_BY_NAME, TOOL_SPECS


# ══════════════════════════════════════════════════════════
# 假沙箱：只实现 browser.py 用到的那两个方法
# ══════════════════════════════════════════════════════════


@dataclass
class _FakeResult:
    """对应 `runner.CommandResult` 的最小面。"""

    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def combined(self, limit: int = 8000) -> str:
        text = self.stdout
        if self.stderr.strip():
            text = f"{text}\n[stderr]\n{self.stderr}" if text else self.stderr
        return text[:limit]


@dataclass
class _FakeSandbox:
    """记录所有被执行的 shell 命令，按预设返回结果。"""

    #: 预设：命令里含某个子串 → 返回什么。按插入顺序匹配。
    replies: list[tuple[str, _FakeResult]] = field(default_factory=list)
    #: 所有收到的命令（断言用）
    commands: list[str] = field(default_factory=list)
    #: 没命中 preset 时的默认返回
    default: _FakeResult = field(default_factory=lambda: _FakeResult())

    def shell(self, command: str, *, timeout: float = 300) -> _FakeResult:
        self.commands.append(command)
        for needle, reply in self.replies:
            if needle in command:
                return reply
        return self.default

    def copy_in(self, host_path, container_path) -> _FakeResult:
        self.commands.append(f"copy_in {host_path} -> {container_path}")
        return _FakeResult()


def _run(sandbox: _FakeSandbox, args: dict) -> str:
    return asyncio.run(run_browser(args, sandbox=sandbox))


# ══════════════════════════════════════════════════════════
# 注册：工具必须同时出现在 TOOL_NAMES 与 TOOL_SPECS
# ══════════════════════════════════════════════════════════


def test_browser_is_registered_in_runner_tool_names():
    """少了这条，模型调 `browser` 会得到「未知工具」。

    ⚠️ 这条测试存在的原因是一个**真实发生过的漂移**：`load_recipe`
    曾经只在 `TOOL_NAMES` 里、不在 `TOOL_SPECS` 里，导致设置页管不到它
    （`disabled_tools` 只认清单里的名字），而那个对账测试从 v0.99 起
    一直是红的。所以两条都要断言。
    """
    assert "browser" in TOOL_NAMES


def test_browser_has_a_tool_spec():
    assert "browser" in TOOL_BY_NAME
    spec = TOOL_BY_NAME["browser"]
    assert spec.group == "沙箱执行"
    assert spec.label  # 中文显示名
    assert spec.summary


def test_browser_spec_declares_it_cannot_see_images():
    """★ 这条是**产品级**要求，不是文档洁癖。

    `default_limitation` 的 origin story（见 `tools.py` 里那个字段的
    docstring）：2026-10-24 的 A/B 实验发现，工具在默认参数下悄悄缩水时，
    模型会把缩水结果读成结论。这里缩水的是**看图** ——
    `screenshot` 存了文件但模型看不到内容。

    如果这条说明缺失，界面与提示词都不会告诉模型它看不了图，
    而模型会以为自己验证过页面外观。所以断言**关键词**。
    """
    limitation = TOOL_BY_NAME["browser"].default_limitation
    assert "图片" in limitation
    assert "screenshot" in limitation


def test_subcommands_match_module_constant():
    """`SUBCOMMANDS` 是给模型的清单，`run_browser` 用它做白名单。

    两边不一致会出现"清单里有但调不动"或反过来的假能力。
    """
    # 这不是自我引用：断言的是常量**被真的用上了**（见下面的白名单测试）。
    assert len(SUBCOMMANDS) == len(set(SUBCOMMANDS)), "子命令有重复"
    for name in SUBCOMMANDS:
        assert name and name.strip() == name


# ══════════════════════════════════════════════════════════
# 参数校验：错用法要给出「下一步」，不能只报错
# ══════════════════════════════════════════════════════════


def test_missing_command_lists_all_subcommands():
    out = _run(_FakeSandbox(), {})
    assert "参数错误" in out
    for name in SUBCOMMANDS:
        assert name in out, f"清单里漏了 {name}"


def test_unknown_command_suggests_the_common_flow():
    out = _run(_FakeSandbox(), {"command": "teleport"})
    assert "未知子命令" in out
    # 模型需要知道"换哪条路"，所以要点名最常见的入口
    assert "open" in out and "snapshot" in out


def test_open_requires_url():
    out = _run(_FakeSandbox(), {"command": "open"})
    assert "参数错误" in out and "url" in out


@pytest.mark.parametrize("bad", ["ftp://x", "file:///etc/passwd", "javascript:alert(1)", "example.com"])
def test_open_rejects_non_http_schemes(bad):
    """★ `file://` 与 `javascript:` 必须被挡。

    前者会让模型直接读容器里的文件（绕过 `read_attachment` 的白名单），
    后者能在页面上下文里执行任意代码。两者都不是"浏览器打开网页"。
    """
    out = _run(_FakeSandbox(), {"command": "open", "url": bad})
    assert "http" in out
    # 且**不该**发出任何命令
    sandbox = _FakeSandbox()
    _run(sandbox, {"command": "open", "url": bad})
    assert not any(BIN in c for c in sandbox.commands), "非法 url 不该被真的发出去"


def test_click_requires_target():
    out = _run(_FakeSandbox(), {"command": "click"})
    assert "参数错误" in out and "target" in out
    assert "snapshot" in out, "要告诉模型先去 snapshots 拿 ref"


def test_fill_requires_target():
    out = _run(_FakeSandbox(), {"command": "fill", "text": "x"})
    assert "参数错误" in out and "target" in out


def test_press_requires_key():
    out = _run(_FakeSandbox(), {"command": "press"})
    assert "参数错误" in out and "key" in out


def test_get_requires_what():
    out = _run(_FakeSandbox(), {"command": "get"})
    assert "参数错误" in out and "what" in out


def test_get_attr_requires_attr_name():
    out = _run(_FakeSandbox(), {"command": "get", "what": "attr", "target": "a"})
    assert "参数错误" in out and "attr" in out


def test_wait_requires_target_or_ms():
    out = _run(_FakeSandbox(), {"command": "wait"})
    assert "参数错误" in out


def test_eval_requires_expression():
    out = _run(_FakeSandbox(), {"command": "eval"})
    assert "参数错误" in out and "expression" in out


# ══════════════════════════════════════════════════════════
# 可写目录：只读根 + tmpfs 的硬约束
# ══════════════════════════════════════════════════════════


def test_ensures_writable_dirs_before_any_browser_command():
    """★ 容器根是 `--read-only`，`/work` 是 tmpfs。

    镜像里 `mkdir` 出来的目录会在 tmpfs 挂上来的那一刻被盖掉，
    所以每次调用都得重建 —— 少了这一步的症状是
    "Failed to create a ProcessSingleton"，看起来像权限问题。
    """
    sandbox = _FakeSandbox()
    _run(sandbox, {"command": "snapshot"})
    mkdir_cmds = [c for c in sandbox.commands if "mkdir -p" in c]
    assert mkdir_cmds, "没有先建可写目录"
    joined = " ".join(mkdir_cmds)
    for sub in ("profile", "sock", "shots"):
        assert f"{WORK_ROOT}/{sub}" in joined


def test_close_does_not_need_chrome_but_cleans_up():
    """`close` 必须能在一个已经坏掉的 daemon 上跑（兜底杀进程）。"""
    sandbox = _FakeSandbox()
    out = _run(sandbox, {"command": "close"})
    assert "已关闭" in out
    joined = " ".join(sandbox.commands)
    assert "pkill" in joined, "没有兜底杀进程：daemon 坏了就关不掉"
    assert "rm -rf" in joined, "没有清 profile/socket，下次 open 会撞到旧状态"


# ══════════════════════════════════════════════════════════
# 成功路径
# ══════════════════════════════════════════════════════════


def test_snapshot_forwards_interactive_flag_by_default():
    """`-i` 是省 token 的关键：真实站点整棵树轻松上千行。"""
    sandbox = _FakeSandbox(
        replies=[("snapshot", _FakeResult(stdout='- button "Sign in" [ref=e2]'))]
    )
    out = _run(sandbox, {"command": "snapshot"})
    assert "ref=e2" in out
    invoked = [c for c in sandbox.commands if c.startswith(f"{BIN} ")]
    assert invoked, "没有真的调用 agent-browser"
    assert "-i" in invoked[-1]


def test_snapshot_can_ask_for_full_tree():
    sandbox = _FakeSandbox(replies=[("snapshot", _FakeResult(stdout="tree"))])
    _run(sandbox, {"command": "snapshot", "interactive": False})
    invoked = [c for c in sandbox.commands if c.startswith(f"{BIN} ")]
    assert "-i" not in invoked[-1]


def test_open_appends_next_step_hint():
    """`open` 成功后必须指出下一步。

    实测发现模型 open 完常常直接 `get text`（拿不到结构），
    而不是 `snapshot -i`。一句提示能省一个回合。
    """
    sandbox = _FakeSandbox(
        replies=[("open", _FakeResult(stdout="✓ Example Domain\n  http://example.com"))]
    )
    out = _run(sandbox, {"command": "open", "url": "https://example.com"})
    assert "snapshot" in out, "open 之后没有提示下一步该 snapshot"


def test_click_forwards_ref_verbatim():
    """ref 必须原样传，不能被 shell 改写。"""
    sandbox = _FakeSandbox(replies=[("click", _FakeResult(stdout="✓ Done"))])
    _run(sandbox, {"command": "click", "target": "@e3"})
    invoked = [c for c in sandbox.commands if c.startswith(f"{BIN} ")]
    assert "@e3" in invoked[-1]


def test_fill_quotes_text_so_spaces_survive():
    """★ 模型给的值可能含空格、引号、`$`。

    不 quote 的话 shell 会把它们拆开，而症状是"选择器写对了却
    找不到元素"—— 那个错误方向会浪费很多回合。
    """
    sandbox = _FakeSandbox(replies=[("fill", _FakeResult(stdout="✓ Done"))])
    _run(sandbox, {"command": "fill", "target": "@e3", "text": "hello world $HOME"})
    invoked = [c for c in sandbox.commands if c.startswith(f"{BIN} ")]
    cmd = invoked[-1]
    # 带空格的整个值必须被引号包住
    assert "'hello world $HOME'" in cmd or '"hello world $HOME"' in cmd


# ══════════════════════════════════════════════════════════
# 失败路径：错误消息必须带「下一步」
# ══════════════════════════════════════════════════════════


def test_unknown_ref_tells_model_to_resnapshot():
    """★ 这是 ref 机制最容易踩的坑（页面一变 ref 就重新分配）。

    实测：`click @e99` → `✗ Unknown ref: e99`，**退出码 1**。
    """
    sandbox = _FakeSandbox(
        replies=[("click", _FakeResult(exit_code=1, stderr="✗ Unknown ref: e99"))]
    )
    out = _run(sandbox, {"command": "click", "target": "@e99"})
    assert "失败" in out
    assert "snapshot" in out, "没告诉模型重新快照 —— 它会重试同一条命令"


def test_element_not_found_suggests_snapshot_or_wait():
    sandbox = _FakeSandbox(
        replies=[
            (
                "click",
                _FakeResult(exit_code=1, stderr="✗ Element not found. Verify the selector"),
            )
        ]
    )
    out = _run(sandbox, {"command": "click", "target": "#nope"})
    assert "snapshot" in out or "wait" in out


def test_connection_refused_suggests_reopen():
    """容器重启后 daemon 就没了。这条提示能省掉一整轮瞎猜。"""
    sandbox = _FakeSandbox(
        replies=[("snapshot", _FakeResult(exit_code=1, stderr="connection refused"))]
    )
    out = _run(sandbox, {"command": "snapshot"})
    assert "open" in out


def test_error_message_uses_stderr_not_stdout():
    """★ 实测：agent-browser 的错误走 **stderr**，`2>/dev/null` 之后
    stdout 是空的。所以必须用 `combined()` 取消息，否则错误全丢。
    """
    sandbox = _FakeSandbox(
        replies=[
            # stdout 空、错误只在 stderr
            ("click", _FakeResult(exit_code=1, stdout="", stderr="✗ Unknown ref: e5"))
        ]
    )
    out = _run(sandbox, {"command": "click", "target": "@e5"})
    assert "Unknown ref" in out, "错误消息丢了 —— 用了 stdout 而不是 combined"


def test_success_is_detected_via_exit_code_not_the_checkmark():
    """★ 实测依据：成功 → 0，失败 → 1。

    刻意**不**解析 `✓`/`✗` 文本：那两种符号只是给人看的装饰，
    而退出码是 agent-browser 自己的契约。若哪天它改了符号，
    解析文本的写法会静默把失败当成功。
    """
    # 退出码 0 但输出里一个 ✓ 都没有 —— 仍应判成功
    sandbox = _FakeSandbox(replies=[("get", _FakeResult(exit_code=0, stdout="Login Demo"))])
    out = _run(sandbox, {"command": "get", "what": "title"})
    assert "失败" not in out
    assert "Login Demo" in out

    # 退出码 1 但输出里带 ✓ —— 仍应判失败
    sandbox2 = _FakeSandbox(
        replies=[("get", _FakeResult(exit_code=1, stdout="✓ looks fine", stderr="boom"))]
    )
    out2 = _run(sandbox2, {"command": "get", "what": "title"})
    assert "失败" in out2


# ══════════════════════════════════════════════════════════
# ★ 最重要的一条：截图后必须明说「你看不到」
# ══════════════════════════════════════════════════════════


def test_screenshot_explicitly_says_model_cannot_see_the_image():
    """这条是整个工具最容易被做成「假能力」的地方。

    `chat/attachments.py::read_text` 把二进制挡在模型外面，所以
    `screenshot` 的成功**只意味着文件写出来了**，不意味着模型看过了。
    如果返回值里不说这句，模型会基于一个它没看到的画面下结论 ——
    那比工具不存在更糟，因为它会以为自己验证过。
    """
    sandbox = _FakeSandbox(
        replies=[("screenshot", _FakeResult(exit_code=0, stdout="Screenshot saved: /work/a.png"))]
    )
    out = _run(sandbox, {"command": "screenshot"})
    assert "看不到" in out or "无法看到" in out, "没说清楚模型看不了图"
    # 并且要给出替代手段
    assert "snapshot" in out or "get text" in out


# ══════════════════════════════════════════════════════════
# skills：纯读文档，不该依赖浏览器能起来
# ══════════════════════════════════════════════════════════


def test_skills_does_not_require_browser_to_start():
    """排查浏览器起不来的时候，恰好最需要读手册。

    所以 `skills` 必须在最前面处理、**跳过**建目录那一步。
    """
    sandbox = _FakeSandbox(replies=[("skills get", _FakeResult(stdout="# Core usage"))])
    out = _run(sandbox, {"command": "skills"})
    assert "Core usage" in out
    assert not any("mkdir -p" in c for c in sandbox.commands), (
        "skills 是纯文本查询，不该先建浏览器目录"
    )


def test_skills_defaults_to_core():
    sandbox = _FakeSandbox(replies=[("skills get", _FakeResult(stdout="doc"))])
    _run(sandbox, {"command": "skills"})
    invoked = [c for c in sandbox.commands if c.startswith(f"{BIN} skills")]
    assert invoked and "core" in invoked[-1]


def test_skills_failure_lists_available_ones():
    """名字拼错时回清单，而不是空串 —— 空串会让模型反复猜。"""
    sandbox = _FakeSandbox(
        replies=[
            ("skills get", _FakeResult(exit_code=1, stderr="unknown skill")),
            ("skills list", _FakeResult(stdout="  core\n  electron")),
        ]
    )
    out = _run(sandbox, {"command": "skills", "name": "nope"})
    assert "core" in out and "electron" in out


# ══════════════════════════════════════════════════════════
# 超时上限：模型不能一次占住沙箱十几分钟
# ══════════════════════════════════════════════════════════


def test_timeout_is_clamped():
    """模型可能传 `timeout_seconds: 99999`（它见过 `run_shell` 的参数）。"""
    sandbox = _FakeSandbox()
    calls: list[float] = []
    original = sandbox.shell

    def spy(command: str, *, timeout: float = 300):
        calls.append(timeout)
        return original(command, timeout=timeout)

    sandbox.shell = spy  # type: ignore[method-assign]
    _run(sandbox, {"command": "snapshot", "timeout_seconds": 99999})
    assert calls, "没有执行任何命令"
    assert max(calls) <= 180, f"超时没被夹住：{max(calls)}"


def test_open_gets_a_longer_timeout_than_other_commands():
    """首次 open 要拉起 Chrome（建 profile、初始化渲染进程）。

    与其它命令给同一个短超时的话，冷启动会随机失败 ——
    而症状是"有时候连得上有时候连不上"。
    """
    assert browser_mod.OPEN_TIMEOUT > browser_mod.DEFAULT_TIMEOUT
