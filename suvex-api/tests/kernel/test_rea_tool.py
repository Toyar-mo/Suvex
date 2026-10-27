"""`rea` 工具的白名单与参数拼装（v1.05）。

## 这个文件防的是什么

`rea` 的价值**全在**那条白名单上：REA 本体有 177 个子命令，而没装
Ghidra/Hopper/IDA/JADX 的机器上绝大多数会返回 `provider_unavailable`。
如果白名单被放松（或报错文案退化成"命令名拼错了"），模型会把
"环境没装"读成"这个二进制没什么可分析的"，然后放弃一条本来能走的路。

所以这里钉住四件事：

1. 不在白名单里的子命令**一定**被拦住，且报错**指名**它需要反编译引擎；
2. 自检类命令（doctor / capabilities / providers）不需要 `path`；
3. 需要 `path` 的命令缺参时要给出可照抄的调用形状；
4. `--json` 由 `_argv` 强制加上，模型自己传的会被盖掉（重复 `--json`
   会让 REA 的 argparse 报错，而模型完全看不出为什么）。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.kernel import rea
from app.kernel.runner import CommandResult


class FakeSandbox:
    """只记录被跑了什么命令，不真起容器。"""

    def __init__(self, *, ok: bool = True, stdout: str = '{"provider": "x"}') -> None:
        self.commands: list[str] = []
        self.ok = ok
        self.stdout = stdout
        self.stderr = ""

    def shell(self, command: str, *, timeout: int = 300) -> CommandResult:
        self.commands.append(command)
        return CommandResult(
            ok=self.ok, code=0 if self.ok else 1,
            stdout=self.stdout, stderr=self.stderr,
        )


def _run(args: dict[str, Any], sandbox: Any | None = None) -> str:
    return rea.run_rea_sync(args, sandbox=sandbox or FakeSandbox())


def test_no_command_returns_the_capability_list() -> None:
    """不给 `command` 时应当返回完整能力清单，而不是静默成功。"""
    out = _run({})
    assert "可用子命令" in out
    assert "capabilities" in out


def test_unknown_command_is_rejected_and_never_reaches_the_sandbox() -> None:
    box = FakeSandbox()
    out = _run({"command": "definitely-not-a-real-subcommand", "path": "/work/x"}, box)
    assert "未知子命令" in out
    # 关键：被拦住时**一个容器命令都不该跑** —— 否则就是"看起来拦住了，
    # 实际已经把未知参数发给了 REA"。
    assert box.commands == []


@pytest.mark.parametrize("cmd", ["decompile", "xrefs", "analyze", "inspect"])
def test_ghidra_family_gets_a_dedicated_explanation(cmd: str) -> None:
    """反编译族命令要专门说"缺的是引擎，不是文件有问题"。

    这条比"未知子命令"重要得多：模型看见"未知子命令"只会以为拼错了，
    然后换个拼法一直重试。
    """
    out = _run({"command": cmd, "path": "/work/x"})
    assert "需要反编译引擎" in out
    assert "provider_unavailable" in out


def test_self_check_commands_need_no_path() -> None:
    """`doctor` 不带 path 也要能跑（它查的是环境，不是某个文件）。"""
    box = FakeSandbox()
    _run({"command": "doctor"}, box)
    assert box.commands, "doctor 应该真的跑了一条命令"
    assert any("doctor" in c for c in box.commands)


def test_a_command_without_path_asks_for_it_with_an_example() -> None:
    """缺 path 时要给可照抄的 JSON 形状，而不是一句"参数错误"。"""
    box = FakeSandbox()
    out = _run({"command": "inspect-artifact"}, box)
    assert "需要 `path`" in out
    # 示例必须是可照抄的完整 JSON（键名与真实入参一致），而不是占位符
    assert '"command": "inspect-artifact"' in out
    assert '"path": "/work/app.asar"' in out
    assert box.commands == []


def test_json_flag_is_forced_even_if_the_model_passes_its_own() -> None:
    """模型重复传 `--json` 会让 REA 的 argparse 报错，必须盖掉。"""
    box = FakeSandbox()
    _run({"command": "inspect-artifact", "path": "/work/a.asar",
          "extra": {"json": True}}, box)
    actual = [c for c in box.commands if "inspect-artifact" in c]
    assert actual, "应当真的跑了一次 inspect-artifact"
    assert actual[0].count("--json") == 1


def test_inline_path_in_command_is_tolerated() -> None:
    """模型从 REA 文档学到的形状是 `rea <cmd> <path>`，会塞进 command 里。"""
    box = FakeSandbox()
    _run({"command": "inspect-artifact /work/a.asar"}, box)
    actual = [c for c in box.commands if "inspect-artifact" in c]
    assert actual, "带内联路径时应当仍然跑起来"
    assert "/work/a.asar" in actual[0]


def test_path_with_a_space_is_shell_quoted() -> None:
    """路径里的空格必须被引号包住，否则一个文件名会变成两条命令。"""
    box = FakeSandbox()
    _run({"command": "inspect-artifact", "path": "/work/has space.asar"}, box)
    actual = [c for c in box.commands if "inspect-artifact" in c]
    assert actual
    assert "'/work/has space.asar'" in actual[0]
