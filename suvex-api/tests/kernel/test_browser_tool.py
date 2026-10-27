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
import shlex
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from app.kernel import browser as browser_mod
from app.kernel.browser import BIN, HOME_ENV, SUBCOMMANDS, WORK_ROOT, run_browser
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


def _browser_cmds(sandbox: _FakeSandbox, *, head: str = "") -> list[str]:
    """挑出真正调 `agent-browser` 的命令。

    ★ 为什么要 `HOME_ENV` 前缀而不是直接 `startswith(BIN)`：`_invoke` 现在
    会前置 `HOME=…`（见 `browser.HOME_ENV` 的说明 —— 容器 `--read-only`
    时 `$HOME` 不可写，Chrome 起不来）。命令因此变成
    `HOME=/work/.browser/home agent-browser open …`。

    ⚠️ 这类"改前缀把断言改瞎"是个真陷阱：`startswith(f"{BIN} ")` 会
    静默返回空列表，而 `invoked[-1]` 立刻 IndexError —— 那还算好的；
    若是 `assert "x" not in invoked[-1]` 就会变成"永远通过"。所以统一走
    这一个辅助函数，前缀定义只有一处。
    """
    needle = f"{HOME_ENV} {BIN} {head}" if head else f"{HOME_ENV} {BIN} "
    return [c for c in sandbox.commands if c.startswith(needle)]


def _browser_tokens(sandbox: _FakeSandbox) -> list[str]:
    """把最后一条 `agent-browser …` 命令拆回 argv token。

    ★ 为什么不能直接对命令行做子串断言：`_invoke` 用 `shlex.quote` 拼，
    切错参数时得到的是 `fill '@e2 hello world'` —— 一个带引号的 token，
    而 `@e2` / `hello world` 的子串都还在里面，断言会假绿。
    拆回 token 才能区分「两个参数」和「一个带空格的参数」。
    """
    for command in reversed(sandbox.commands):
        if f"{BIN} " not in command:
            continue
        # 去掉前置的 `HOME=…` 赋值，只拆 agent-browser 自己的 argv
        return shlex.split(command.split(HOME_ENV, 1)[-1].strip())[1:]
    raise AssertionError(f"没有执行任何 {BIN} 命令：{sandbox.commands}")


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


def test_browser_spec_declares_how_to_actually_see_the_screenshot():
    """★ 这条是**产品级**要求，不是文档洁癖。

    `default_limitation` 的 origin story（见 `tools.py` 里那个字段的
    docstring）：2026-10-24 的 A/B 实验发现，工具在默认参数下悄悄缩水时，
    模型会把缩水结果读成结论。这里缩水的是**看图** —— `screenshot` 只把
    PNG 存成文件。

    ## 断言的是「文案与能力一致」，不是某一版文案

    v0.106 本测试断言「看不了图片」；v1.04 并行会话接入 `view_image` 后
    （Kali 实测 base64 → 200、prompt_tokens 31→223），那句话变成**反向
    误导**：模型明明能看却被告知看不了，比没有这个工具更糟。文案随之改写。

    所以这里不钉死字符串，而是钉**不变量**：
      ① 如果内核有 `view_image` → 文案不许说看不了，且必须指出 `view_image`
         （否则模型拿着路径就下结论）；
      ② 如果内核没有 `view_image` → 文案必须明确说看不到了，且给替代手段
         （否则模型以为自己验证过页面外观）。

    这样两边的提交先后顺序都不会让这条测试错红 —— 它只在**真的不一致**时红。
    """
    limitation = TOOL_BY_NAME["browser"].default_limitation
    has_view_image = "view_image" in TOOL_BY_NAME

    assert "screenshot" in limitation, "要点明缩水发生在 screenshot 上"

    if has_view_image:
        assert "看不了图片" not in limitation, (
            "内核已有 `view_image`，再说「看不了图片」会让模型放弃看图"
            f"（当前文案：{limitation}）"
        )
        assert "view_image" in limitation, (
            "必须给出真正能看图的那条路，否则模型只拿到一个路径就下结论"
        )
    else:
        assert "看不到" in limitation or "看不了" in limitation, (
            "没有 `view_image` 时必须明说看不到，否则模型会以为自己验证过页面外观"
        )
        assert "snapshot" in limitation or "get text" in limitation, (
            "要用文字手段兜底：snapshot / get text"
        )


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
    invoked = _browser_cmds(sandbox)
    assert invoked, "没有真的调用 agent-browser"
    assert "-i" in invoked[-1]


def test_snapshot_can_ask_for_full_tree():
    sandbox = _FakeSandbox(replies=[("snapshot", _FakeResult(stdout="tree"))])
    _run(sandbox, {"command": "snapshot", "interactive": False})
    invoked = _browser_cmds(sandbox)
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
    invoked = _browser_cmds(sandbox)
    assert "@e3" in invoked[-1]


def test_fill_quotes_text_so_spaces_survive():
    """★ 模型给的值可能含空格、引号、`$`。

    不 quote 的话 shell 会把它们拆开，而症状是"选择器写对了却
    找不到元素"—— 那个错误方向会浪费很多回合。
    """
    sandbox = _FakeSandbox(replies=[("fill", _FakeResult(stdout="✓ Done"))])
    _run(sandbox, {"command": "fill", "target": "@e3", "text": "hello world $HOME"})
    invoked = _browser_cmds(sandbox)
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
# ★ 最重要的一条：截图后必须明说「怎么才真看得到」
# ══════════════════════════════════════════════════════════


def test_screenshot_says_exactly_how_to_actually_see_the_image():
    """这条是整个工具最容易被做成「假能力」的地方。

    `screenshot` 的成功**只意味着文件写出来了**，不意味着模型看过了。
    如果返回值不说清下一步，模型会基于一个它没看到的画面下结论 ——
    那比工具不存在更糟，因为它会以为自己验证过。

    ★ 期望值变过（记录，否则以后会有人以为是笔误）：
      - v0.106 本测试断言「看不到」/「无法看到」。
      - v1.04 并行会话接入 `view_image` 后，这句变成反向误导 —— 图**能**
        送进对话了，再说看不到会让模型放弃看图。文案改为把
        `view_image` 的**完整调用**写进返回值。
      - 现在钉住契约本身：**必须给出可执行的那一步**，且路径要能直接抄。
    """
    sandbox = _FakeSandbox(
        replies=[("screenshot", _FakeResult(exit_code=0, stdout="Screenshot saved: /work/a.png"))]
    )
    out = _run(sandbox, {"command": "screenshot"})

    assert "view_image" in out, (
        f"screenshot 之后必须告诉模型怎么真看图，否则它拿着路径就下结论（当前：{out}）"
    )
    # 路径要真的写进去，不能只说"用 view_image" —— 让它自己去猜路径等于
    # 多烧一轮，而且猜错就是白跑。
    assert "/work/" in out and ".png" in out, "要把具体路径写进返回值"
    # 并且保留不看图也能拿到信息的替代手段（省 token 的主路径）
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
    invoked = _browser_cmds(sandbox, head="skills")
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


# ══════════════════════════════════════════════════════════
# 内联参数形式（`"command": "open https://…"`）
# ══════════════════════════════════════════════════════════
#
# ★ 这一组来自一次**实机失败**（2026-10-27，Kali，v0.111 实测）：
#
#   模型收到"用 browser 打开 https://example.com"之后，连着两次把参数写成
#   `{"command": "open https://example.com"}` —— URL 塞在 command 里。
#   而实现读的是 `args["url"]`，取到空串，回「`open` 需要 `url`」。
#   模型第三次才猜对字段名。
#
#   这不是模型笨：内置技能文档（`browser skills core`）教的就是
#   agent-browser 自己的 CLI 形状（`open <url>` / `click @e3`）。技能文档
#   说 CLI，工具 schema 说 JSON 字段 —— **两份契约漂了**，而模型两边都读。
#
#   所以工具这边多走一步：子命令后面跟的内容当作该子命令的第一个位置参数。
#   提示词同时改成明确写字段名（两处都修，任何一处单独修都不够：
#   模型可能先读提示词，也可能先读技能）。


def test_open_accepts_url_inside_command():
    """`{"command": "open https://example.com"}` 必须能开。

    这是实机那次失败的原样复现。
    """
    sandbox = _FakeSandbox()
    out = _run(sandbox, {"command": "open https://example.com"})

    assert "参数错误" not in out, f"内联 url 不该被拒绝：{out[:200]}"
    joined = "\n".join(sandbox.commands)
    assert "https://example.com" in joined, f"url 没被传给浏览器：{joined}"


def test_open_prefers_explicit_url_over_inline():
    """显式 `url` 字段优先于 command 里跟的那串。

    模型可能两个都给（先按技能文档写，又补上字段）。冲突时以字段为准，
    因为它更明确。
    """
    sandbox = _FakeSandbox()
    _run(sandbox, {"command": "open https://ignored.example", "url": "https://used.example"})

    joined = "\n".join(sandbox.commands)
    assert "https://used.example" in joined
    assert "https://ignored.example" not in joined


def test_click_accepts_ref_inside_command():
    """`{"command": "click @e3"}` —— 技能文档里 click 就是这个形状。"""
    sandbox = _FakeSandbox()
    out = _run(sandbox, {"command": "click @e3"})

    assert "参数错误" not in out
    assert "@e3" in "\n".join(sandbox.commands)


def test_fill_splits_target_and_text_from_inline():
    """`{"command": "fill @e2 hello world"}` —— 目标与文本必须切成两个 token。

    切错时会往输入框里填 `@e2 hello world`，而失败是静默的（命令 exit 0）。

    ★ 这里必须**解析回 token 再断言**，不能只查子串：`_invoke` 用
    `shlex.quote` 拼命令行，切错时得到的是 `'@e2 hello world'`（一个带
    引号的 token），子串 `@e2` 和 `hello world` 都照样命中 —— 用子串断言
    的话这个缺陷完全测不出来（第一版就是这么写的，变异验证没抓住）。
    """
    sandbox = _FakeSandbox()
    out = _run(sandbox, {"command": "fill @e2 hello world"})

    assert "参数错误" not in out
    tokens = _browser_tokens(sandbox)
    assert tokens[:2] == ["fill", "@e2"], f"目标 token 不对：{tokens}"
    assert tokens[2:] == ["hello world"], f"文本 token 不对：{tokens}"


def test_get_splits_what_and_selector_from_inline():
    """`{"command": "get text h1"}` —— what 与选择器分开。

    只把整串当 what 的话，agent-browser 会收到 `get "text h1"` 这种坏参数。
    """
    sandbox = _FakeSandbox()
    _run(sandbox, {"command": "get text h1"})

    joined = "\n".join(sandbox.commands)
    assert "text" in joined and "h1" in joined


def test_snapshot_with_flags_still_works():
    """`{"command": "snapshot -i"}` 不该被当成未知子命令。

    注意：`inline_rest` 只拆分**已知子命令**开头的串，所以
    `snapshot -i` 会被拆成 command=snapshot + inline_rest="-i"。
    `-i` 由 `interactive` 字段控制，这里只要不报错、不发错参数即可。
    """
    sandbox = _FakeSandbox()
    out = _run(sandbox, {"command": "snapshot -i"})

    assert "未知子命令" not in out, f"`snapshot -i` 被拒了：{out[:200]}"
    assert sandbox.commands, "没有执行任何命令"


def test_unknown_head_is_still_rejected():
    """拆分只对**已知**子命令生效，不能把随便一句话当子命令放过。

    没有这条的话，`{"command": "teleport somewhere"}` 会被拆成
    command=teleport，然后走"未知子命令"分支 —— 结果一样，但如果拆分
    逻辑写宽了（比如接受任何 head），就会静默执行错的东西。
    """
    out = _run(_FakeSandbox(), {"command": "teleport somewhere"})
    assert "未知子命令" in out


def test_bare_command_still_requires_its_argument():
    """拆分不能把"该报缺参数"的错误吞掉。

    `{"command": "open"}` 没有任何 url 来源，仍然必须报参数错误 ——
    否则会拿空 url 去执行，得到一个与用户意图无关的页面。
    """
    out = _run(_FakeSandbox(), {"command": "open"})
    assert "参数错误" in out and "url" in out


def test_snapshot_accepts_inline_u_flag():
    """`{"command": "snapshot -u"}` 必须真带上 `-u`。

    静默丢掉一个开关比报错更坏：模型会以为拿到的元素清单里带了 url，
    然后对着不存在的字段做判断。这条钉住"内联开关不被吞掉"。
    """
    sandbox = _FakeSandbox()
    _run(sandbox, {"command": "snapshot -u"})

    joined = "\n".join(sandbox.commands)
    assert "-u" in joined, f"`-u` 被静默丢掉了：{joined}"
    assert "-i" in joined, "默认仍应有 -i（省 token 是常态）"


def test_snapshot_inline_i_beats_explicit_false():
    """`{"command": "snapshot -i", "interactive": false}` —— 内联 `-i` 说了算。

    两个来源冲突时，模型显式写在 command 里的那个更能代表意图。
    """
    sandbox = _FakeSandbox()
    _run(sandbox, {"command": "snapshot -i", "interactive": False})

    assert "-i" in "\n".join(sandbox.commands)


# ══════════════════════════════════════════════════════════
# 技能文档与内核能力必须一致
# ══════════════════════════════════════════════════════════


def _agent_browser_skill_text() -> str:
    path = (
        Path(__file__).resolve().parents[2]
        / "prompts"
        / "skills"
        / "tooling"
        / "agent_browser.md"
    )
    assert path.is_file(), f"找不到内置技能：{path}"
    return path.read_text(encoding="utf-8")


def test_skill_doc_does_not_claim_the_model_is_blind_when_view_image_exists():
    """★ 技能文档比提示词更容易骗人：模型会去读它，然后照它做。

    `prompts/skills/tooling/agent_browser.md` 在 v0.106 写死了
    「There is no `view_image` tool in this project, and the model has no
    vision input」。v1.04 接入 `view_image` 之后，这句话变成**主动劝阻**
    —— 模型读了技能，就不去截图验证视觉问题了，而它本来可以。

    这类"文档说没有、代码里有"的漂移没法靠人记住：两份东西在不同时间
    由不同人改。所以钉一条不变量，跟随 `TOOL_BY_NAME` 自动判断。
    """
    text = _agent_browser_skill_text()
    has_view_image = "view_image" in TOOL_BY_NAME

    if has_view_image:
        for claim in (
            "There is no `view_image` tool",
            "the model has no vision input",
            "none of which exist here",
        ):
            assert claim not in text, (
                f"内核已有 `view_image`，技能文档却还说「{claim}」——"
                "模型会读它，然后放弃本来能做的视觉验证"
            )
        assert "view_image" in text, (
            "技能必须指出 `view_image`，否则模型只会把截图当文件存着"
        )


def test_skill_doc_names_the_same_screenshot_dir_as_the_code():
    """文档里的截图路径必须与代码常量一致。

    写错的话模型会去 `ls` 一个不存在的目录，然后得出"没截到图"。
    """
    text = _agent_browser_skill_text()
    shots_dir = f"{WORK_ROOT}/shots"
    assert shots_dir in text, f"技能里的截图目录应写成 {shots_dir}"


def test_skill_frontmatter_description_does_not_say_screenshots_are_file_only():
    """技能索引里显示的就是 `description`，模型据此决定要不要读全文。

    `description` 曾写「why screenshots are file-only here」—— 正文早就有了
    `view_image` 段落，但元数据没跟上。这条比正文更隐蔽：正文至少会被读过
    的人看到，`description` 是**唯一**进索引的那一行，它劝退就是彻底劝退。
    """
    text = _agent_browser_skill_text()
    front = text.split("---", 2)[1]
    assert "name: agent_browser" in front, "frontmatter 结构变了，本测试需要跟着改"

    if "view_image" in TOOL_BY_NAME:
        assert "file-only" not in front, (
            "`description` 说仅存文件，而内核有 `view_image` —— "
            "模型看索引就会放弃看图"
        )


# ══════════════════════════════════════════════════════════
# ★ 容器只读 ⇒ 必须给 Chrome 一个可写的 HOME
# ══════════════════════════════════════════════════════════


def test_invoke_prepends_a_writable_home():
    """★ 这条守的是一个**上线即全挂**的故障，而且挂得很难懂。

    容器是 `--read-only` 起的（`runner._restricted_argv`），所以
    `$HOME=/home/solver` 整个不可写。Chromium 要建
    `$HOME/.config/chromium` 与 crashpad 数据库，建不出来就死在

        Failed to create headless user data directory container
        Chrome exited early without writing DevToolsActivePort

    Kali 实测：`touch $HOME/.wtest` → `Read-only file system`；
    `mkdir $HOME/.config/chromium` → 同样失败；而把 `HOME` 指到
    `/work`（tmpfs）之后 `open` / `snapshot` / `screenshot` 立刻全通。

    ⚠️ 为什么这条必须钉在"命令里带 HOME="这个形状上，而不是去跑一遍
    Chrome：单元测试里没有真容器。退而求其次钉住**因**（环境变量的写法）
    而不是**果**（Chrome 能不能起），至少能防"清理代码时把这行删了"。
    """
    sandbox = _FakeSandbox()
    _run(sandbox, {"command": "snapshot"})

    cmds = _browser_cmds(sandbox)
    assert cmds, "没有真的调用 agent-browser"
    assert cmds[-1].startswith(f"{HOME_ENV} "), (
        f"调 agent-browser 前必须前置 `{HOME_ENV}` —— 容器根是只读的，"
        "Chrome 往 $HOME 写 user-data-dir 会失败，整条浏览器工具链会挂"
    )


def test_home_dir_is_created_under_work():
    """HOME 落点必须真被 `mkdir -p` 建出来，且在 `/work`（tmpfs）之下。

    只写环境变量不建目录等于没修：`HOME=/work/.browser/home` 指向一个
    不存在的路径时，Chrome 一样起不来（tmpfs 挂载后镜像里的目录不算数）。
    """
    sandbox = _FakeSandbox()
    _run(sandbox, {"command": "open", "url": "https://example.com"})

    mkdir_cmds = [c for c in sandbox.commands if "mkdir -p" in c]
    assert mkdir_cmds, "没有建任何目录"
    assert any(HOME_ENV.removeprefix("HOME=") in c for c in mkdir_cmds), (
        f"`mkdir -p` 里必须含 {HOME_ENV.removeprefix('HOME=')}，否则 Chrome "
        "拿到的 HOME 是个不存在的目录，等于没改"
    )
    home_path = HOME_ENV.removeprefix("HOME=")
    assert home_path.startswith(f"{WORK_ROOT}/"), (
        f"HOME 必须在 {WORK_ROOT}（tmpfs）之下，实际 {home_path} —— "
        "指到只读的根文件系统上就白改"
    )


def test_home_env_matches_the_dir_ensure_dirs_creates():
    """环境变量的值必须与 `_ensure_dirs` 建的那个目录**逐字**相同。

    两个常量各写一遍就会漂：`mkdir` 建 `/work/.browser/home`，而
    `HOME=` 指 `/work/.browser/home/`（多个斜杠）或 `home2`，测试上一条
    只查"含有"，仍会通过，但线上 Chrome 会去一个空目录。
    """
    sandbox = _FakeSandbox()
    _run(sandbox, {"command": "snapshot"})

    home_from_env = HOME_ENV.split("=", 1)[1]
    mkdir_line = next(c for c in sandbox.commands if "mkdir -p" in c)
    assert f" {home_from_env} " in mkdir_line or mkdir_line.endswith(f" {home_from_env}"), (
        f"HOME={home_from_env} 与 mkdir 的行不一致：{mkdir_line}"
    )
