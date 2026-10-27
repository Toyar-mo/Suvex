"""技能包 / 提示词里教的工具名，必须是内核**真的注册过**的那些。

## 这个文件在防什么

`prompts/skills/analysis/counterevidence.md` 原来写着：

    File it with `create_vulnerability_report`.

而 `create_vulnerability_report` 是 **strix** 的工具名 —— 内核
（`app/kernel/`）里根本没有它。模型照做会拿到
`未知工具 \\`create_vulnerability_report\\``（`app/kernel/runner.py`
的 `dispatch_tool` 末尾），**白烧一轮**再自己想办法。

这条错不是笔误，是**来源污染**：技能包是当年从 strix 的 skills 目录
vendor 过来的（见 `app/kernel/skills.py` 文件头），vendor 时保留了
strix 的工具名，而内核的工具集与 strix 不同。

`app/kernel/skills.py:19-26` 自己就把这条写成了硬纪律：

> 「只 vendor 与镜像真实工具对得上的 playbook……把讲这些工具的 skill
> 塞进来，等于教模型用不存在的命令」

**纪律写在注释里，没挡住。** 所以这条约束得靠测试 —— 与本项目
「注释挡不住，得靠测试」的其他先例（`test_skill_coverage.py`）同理。

## 判据：为什么是"白名单对账"而不是"黑名单抓 strix 名字"

黑名单（"不许出现 `create_vulnerability_report`"）只能挡住这一条，
下次有人从别的框架抄来 `run_exploit` 一样漏。所以这里建**白名单**：
从代码里真实取出内核注册过的工具名，再扫技能包正文里所有
`` `snake_case` `` 形式的反引号标识符，凡是"看起来像工具调用"却不在
白名单里的，一律报出来让人判断。

## ⚠️ 已知的误报方向（刻意保持宽容）

反引号标识符里绝大多数是**参数名**（`severity` / `outcome` /
`tools`），不是工具名。所以判据收窄为两条，只要有一条命中才算
"疑似工具引用"：

  1. 名字里含调用动词（`create_` / `update_` / `list_` / `get_` /
     `record_` / `write_` / `read_` / `send_` / `call_` / `add_` /
     `resolve_` / `amend_` / `view_` / `stop_` / `wait_` / `finish_` /
     `repeat_` / `describe_` / `load_` / `mark_` / `delete_`），**且**
  2. 它出现在"教你怎么做"的语境里（本文件只扫 `` `name(` `` 形式
     与"共用词表"里的确切名字）。

第 2 条把大部分参数名排除了：参数名在正文里几乎不跟左括号。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.kernel import multiagent
from app.kernel import runner as kernel_runner

_PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"

#: 调用动词前缀。只有以这些开头的反引号标识符才可能是**工具名**，
#: 参数名不会长这样。
_CALL_PREFIXES = (
    "create_",
    "update_",
    "list_",
    "get_",
    "record_",
    "write_",
    "read_",
    "send_",
    "call_",
    "add_",
    "resolve_",
    "amend_",
    "view_",
    "stop_",
    "wait_",
    "finish_",
    "repeat_",
    "describe_",
    "load_",
    "mark_",
    "delete_",
    "remove_",
    "run_",
    "apply_",
    "submit_",
)

#: 在正文里抓 `` `identifier(` `` —— 带左括号说明这是**调用**，不是名词。
_CALL_SITE = re.compile(r"`([a-z][a-z0-9_]*)\(`?")


def _kernel_tool_names() -> frozenset[str]:
    """内核**真的注册过**的全部工具名。

    三个来源，少一个都会误报：

      · `runner.TOOL_NAMES`      —— 基础工具（所有 agent 都能调）
      · `multiagent` 的编排工具   —— 只有 root / 子 agent 能调，但模型确实会调
      · `multiagent` 的记账工具   —— fact / idea / memory 那一族

    ## 为什么从模块里取而不是在测试里抄一份

    抄一份就变成了"测试与代码两份字面量"，而它们会漂移 ——
    漂移之后的测试照样绿，而它守的那件事已经不成立了。
    本项目在 `test_skill_coverage.py` 里踩过同一个坑（前端/后端两份
    `SKILLS` 字面量）。
    """
    names: set[str] = set(kernel_runner.TOOL_NAMES)

    # 编排/记账工具是模块级大写常量（`CREATE_AGENT = "create_agent"`）。
    # 取所有 **值为 snake_case 字符串** 的模块级常量 —— 这比逐个点名安全：
    # 新加一个工具忘了写进测试，这里自动覆盖。
    for attr in dir(multiagent):
        if not attr.isupper():
            continue
        value = getattr(multiagent, attr, None)
        if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]*", value):
            names.add(value)
        # `_DEBT_FREE_TOOL_NAMES` 是 frozenset，也收进来
        elif isinstance(value, frozenset):
            names.update(v for v in value if isinstance(v, str))

    return frozenset(names)


def _prompt_files() -> list[Path]:
    """全部参与渲染的提示词与技能包。"""
    return sorted(_PROMPTS_DIR.rglob("*.md"))


def test_the_kernel_tool_names_look_sane() -> None:
    """先证明白名单本身不是空的 —— 否则下面所有断言都会"通过"。

    ## 为什么必须有这一条

    如果 `_kernel_tool_names()` 因为重构返回了空集（比如常量改名成
    小写、或 `TOOL_NAMES` 被挪走），下面每一条"技能包里的工具名都
    在白名单里"都会**空转通过** —— 一个永远绿的测试比没有更糟，
    因为它给了虚假的安全感。
    """
    names = _kernel_tool_names()
    assert len(names) >= 20, f"白名单只有 {len(names)} 个工具名，取数逻辑可能已失效：{sorted(names)}"
    # 抽几个必然存在的，确认来源都接上了
    for required in ("run_shell", "http_request", "load_skill", "record_coverage", "create_agent"):
        assert required in names, f"白名单漏了 `{required}` —— `_kernel_tool_names()` 的取数逻辑要修"


def test_prompts_only_teach_tools_the_kernel_has() -> None:
    """技能包与提示词里**教模型调用**的工具，内核必须真的有。

    失败时的修法（两种都对，取决于意图）：

      · 工具其实存在、只是名字不同 → 改提示词里的名字；
      · 工具根本不存在 → 要么在内核实现它，要么把这句教学删掉/改写。

    **不要**为了让测试变绿而把工具名从提示词里删掉却留下那句指令
    （"File it with ..." 删掉工具名后就是一句没有宾语的半句话）。
    """
    allowed = _kernel_tool_names()
    offenders: list[str] = []

    for path in _prompt_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for line_no, line in enumerate(text.splitlines(), start=1):
            for match in _CALL_SITE.finditer(line):
                name = match.group(1)
                if not name.startswith(_CALL_PREFIXES):
                    continue
                if name in allowed:
                    continue
                rel = path.relative_to(_PROMPTS_DIR.parent)
                offenders.append(f"{rel}:{line_no} 教了 `{name}()`，但内核没有这个工具")

    assert not offenders, (
        "提示词里教了内核不存在的工具 —— 模型照做会拿到「未知工具」，白烧一轮：\n  "
        + "\n  ".join(offenders)
        + "\n\n内核真的有这些工具（见 `app/kernel/runner.py` 的 `TOOL_NAMES` "
        "与 `app/kernel/multiagent.py` 的模块级常量）。"
    )


def test_no_strix_report_tool_names_remain_in_prompts() -> None:
    """`create_vulnerability_report` 一族**不得**出现在提示词里。

    ## 为什么单独钉这一条（与上一条重叠是有意的）

    上一条是通用判据，它靠"反引号 + 左括号"抓调用。而这次的真事故
    恰好是**没有括号**的写法：

        File it with `create_vulnerability_report`.

    一个反引号包裹的裸名字。通用判据抓不到它，所以这里对这几个
    **已知的 strix 工具名**做无条件字符串检查。

    这条是"事故复盘"而不是"设计"：如果将来真的在内核实现了同名工具，
    把它从下面这个元组里删掉即可 —— 那时上一条测试会自动接管。
    """
    forbidden = (
        "create_vulnerability_report",
        "update_vulnerability_report",
        "create_dependency_report",
        "save_threat_model",
    )
    offenders: list[str] = []
    for path in _prompt_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for name in forbidden:
            if name in text:
                rel = path.relative_to(_PROMPTS_DIR.parent)
                offenders.append(f"{rel} 仍含 strix 工具名 `{name}`")

    assert not offenders, (
        "这些是 strix 的工具名，内核没有实现它们，教模型调用等于教它撞墙：\n  "
        + "\n  ".join(offenders)
        + "\n\n内核报告发现的机制是 `FINAL` 块里的 `findings` 数组"
        "（见 `prompts/kernel_pentest.md` §6.4）。"
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))


# ─────────────────────────────────────────────────────────────────────────────
# 2026-10-27 新增：A 档 misc/取证工具 —— 提示词教了，镜像里就必须真有
# ─────────────────────────────────────────────────────────────────────────────

#: `prompts/kernel_system.md` §6「先用现成工具」那张表里点名的命令 → 镜像里
#: 安装它所需的包名（不同名时才不同）。
#:
#: ## 这条在防什么
#:
#: 与 `test_real_skills_no_skill_for_uninstalled_tools` 是同一种病，只是
#: 方向相反：那条守「技能包别讲镜像没装的工具」，这条守「**系统提示词**
#: 里那张"镜像里都真装了"的表，不许随着 Dockerfile 漂移」。
#:
#: 表头那句原文是「下面这批工具镜像里都真装了（实测过能跑）」。这是**事实
#: 断言**，不是愿望。只要 Dockerfile 少装一个，模型照表去敲就会拿到
#: `command not found`，然后被带偏方向 —— 与 strix 工具名那次事故同一类。
_ADVERTISED_SHELL_TOOLS: dict[str, str] = {
    "zbarimg": "zbar-tools",
    "qrencode": "qrencode",
    "bkcrack": "bkcrack",
    "repak": "repak",
    "foremost": "foremost",
    "testdisk": "testdisk",
    "vol": "volatility3",
    "fenjing": "fenjing",
    "stegoveritas": "stegoveritas",
    "sstimap": "sstimap",
    "jwt_tool": "jwt_tool",
    "pcapfix": "pcapfix",
}


def test_advertised_misc_tools_are_installed_in_the_dockerfile() -> None:
    """`kernel_system.md` 承诺"镜像里都真装了"的工具，Dockerfile 必须真装。

    失败时的修法：把对应工具补进 `docker/suvex-sandbox/Dockerfile`
    （最好是**跑一遍**确认能启动，本项目最恨"能装上但一跑就崩"的假工具）；
    确实装不上（源里没有 / 包已废弃）就从表里删掉那句，并在
    `kernel_system.md` 里写明为什么不用它 —— 不要留着一句假承诺。
    """
    dockerfile = _PROMPTS_DIR.parent / "docker" / "suvex-sandbox" / "Dockerfile"
    assert dockerfile.is_file(), f"找不到沙箱镜像的 Dockerfile：{dockerfile}"
    text = dockerfile.read_text(encoding="utf-8", errors="replace")

    prompt = (_PROMPTS_DIR / "kernel_system.md").read_text(encoding="utf-8", errors="replace")

    missing_in_image: list[str] = []
    missing_in_prompt: list[str] = []
    for command, package in _ADVERTISED_SHELL_TOOLS.items():
        if command not in prompt:
            missing_in_prompt.append(command)
        if package not in text:
            missing_in_image.append(f"`{command}`（期望 Dockerfile 装 `{package}`）")

    assert not missing_in_image, (
        "kernel_system.md 的表说这些工具「镜像里都真装了」，但 Dockerfile 里"
        "找不到它们 —— 那句承诺是假的，模型照做会拿到 command not found：\n  "
        + "\n  ".join(missing_in_image)
    )
    assert not missing_in_prompt, (
        "这些工具本来在 kernel_system.md 的工具表里，现在不见了 —— 如果是有意"
        "删掉，请同步从本测试的 `_ADVERTISED_SHELL_TOOLS` 里移除，否则这条"
        "断言就在守一个已经不存在的事实：\n  " + "\n  ".join(missing_in_prompt)
    )
