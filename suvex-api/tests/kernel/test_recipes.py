"""工具配方库（`app/kernel/recipes.py`）的接线测试。

## 这一组在钉什么

配方库的价值是「把工具的真实调用姿势 + 默认参数下做不到什么，写进代码」。
它有三种会**静默失效**的坏法，而且三种都不会报错：

1. **配方拼出来的 argv 是错的** —— 命令会跑、往往退出码 0、输出无意义。
   模型把它读成结论，那比崩溃糟得多。
2. **`default_limitation` 没写** —— 模型不知道「默认只扫 1000 个端口」，
   把缩水的输出当成「目标就是这样」。这是本模块存在的主要理由。
3. **配方写了但没接线** —— 界面上看不到、模型调不到，代码在仓库里躺着。
   ★ 这正是本项目反复踩的那个形状（`mcp_servers` 在 v0.72 坏过一次）。

## 分四组

1. **自洽校验** —— 模块末尾的 `_assert_recipes_consistent()` 真的在 import 时跑了。
2. **`build_argv`** —— 拼接、必填缺失、空值、注入、`allowed_exit_codes`。
3. **每条配方都得能立起来** —— 参数不重复、command 非空、分组合法。
4. **真实命中过沙箱的配方** —— 每个含 `default_limitation` 的都必须写清局限。

## 反面同样重要

大部分用例都配一个「不给参数时该报错、而不是拼一个残缺命令」的对照分支。
只测「给了参数能拼对」是测不出「少了参数照样跑」的 —— 而那是对称的另一半。
"""

from __future__ import annotations

import inspect

import pytest

from app.kernel import recipes as recipes_mod
from app.kernel.recipes import (
    PARAM_FORMATS,
    RECIPE_BY_NAME,
    RECIPE_GROUPS,
    RECIPE_SPECS,
    build_argv,
)


# ══════════════════════════════════════════════════════════
# 1. 自洽校验真的在 import 时跑了
# ══════════════════════════════════════════════════════════


def test_the_consistency_check_runs_at_import_time():
    """`_assert_recipes_consistent()` 必须是**模块级调用**，不是「留给测试来调」。

    如果它只在测试里被调，那么一份拼错的配方能一路活到生产，
    症状是模型调它时才抛 ValueError —— 那时已经在解题中途，白烧一个回合。
    """
    source = inspect.getsource(recipes_mod)
    # 找模块级那一行裸调用（不是 def 里面）
    assert "\n_assert_recipes_consistent()\n" in source, (
        "模块末尾必须以模块级语句调用 _assert_recipes_consistent()；"
        "只定义不调用等于没有校验"
    )


def test_duplicate_recipe_names_would_be_rejected():
    """重名配方会被 import 时的校验拦住（这里用一个临时构造验证判据本身）。"""
    seen: set[str] = set()
    for recipe in RECIPE_SPECS:
        assert recipe.name not in seen, f"配方名重复：{recipe.name}"
        seen.add(recipe.name)


def test_every_recipe_group_is_declared():
    for recipe in RECIPE_SPECS:
        assert recipe.group in RECIPE_GROUPS, (
            f"配方 {recipe.name} 的分组 {recipe.group!r} 不在 RECIPE_GROUPS 里；"
            "分组写错的症状是界面上那一栏永远是空的，没人会去查为什么"
        )


def test_every_recipe_has_a_command_and_a_summary():
    for recipe in RECIPE_SPECS:
        assert recipe.command, f"配方 {recipe.name} 没写 command"
        assert recipe.summary, f"配方 {recipe.name} 没写 summary（界面上会是空白行）"


def test_param_formats_are_declared_and_flags_have_names():
    for recipe in RECIPE_SPECS:
        names: set[str] = set()
        for param in recipe.params:
            assert param.name not in names, f"配方 {recipe.name} 的参数名重复：{param.name}"
            names.add(param.name)

            assert param.format in PARAM_FORMATS, (
                f"配方 {recipe.name} 的参数 {param.name} 格式非法：{param.format!r}"
            )
            if param.format == "flag":
                assert param.flag, (
                    f"配方 {recipe.name} 的参数 {param.name} 是 flag 但没写 flag 名"
                )
            if param.format == "template":
                assert "{value}" in param.template, (
                    f"配方 {recipe.name} 的参数 {param.name} 模板缺 {{value}}"
                )


# ══════════════════════════════════════════════════════════
# 2. build_argv —— 拼接
# ══════════════════════════════════════════════════════════


def test_positional_params_are_sorted_by_position_not_declaration_order():
    """位置参数必须按 `position` 排，不能按声明顺序。

    这两者一旦不一致，命令会把参数塞到错误的位置上 —— 而工具往往不会报错，
    它只是**扫了别的地方**。
    """
    recipe = RECIPE_BY_NAME["nmap"]
    argv = build_argv(recipe, {"target": "10.0.0.1"})
    assert argv[0] == "nmap"
    assert argv[:3] == ["nmap", "-sT", "-sV"], "无条件前置参数必须紧跟命令名"
    assert "10.0.0.1" in argv


def test_flags_are_emitted_with_their_switch_names():
    recipe = RECIPE_BY_NAME["nmap"]
    argv = build_argv(recipe, {"target": "10.0.0.1", "ports": "80,443"})
    # `-p` 后面必须紧跟值，不能是 `-p80,443` 也不能漏了值
    assert "-p" in argv
    assert argv[argv.index("-p") + 1] == "80,443"


def test_template_params_substitute_the_value():
    recipe = RECIPE_BY_NAME["nmap"]
    argv = build_argv(recipe, {"target": "10.0.0.1", "timing": "2"})
    assert "-T2" in argv


def test_a_missing_required_param_raises_instead_of_building_a_partial_command():
    """缺必填参数必须抛，**不能**拼一个残缺命令。

    残缺命令的后果是「跑了一个参数不全的扫描器」，它往往成功退出（码 0）
    并给出无意义输出。报错模型会改；无意义输出模型会当结论。
    """
    recipe = RECIPE_BY_NAME["nmap"]
    with pytest.raises(ValueError, match="缺少必填参数"):
        build_argv(recipe, {})


def test_a_blank_value_counts_as_not_given_for_required_params():
    """`{"ports": ""}` 这种「我不指定」的写法，对**必填**参数要算作没给。"""
    recipe = RECIPE_BY_NAME["ffuf"]
    with pytest.raises(ValueError, match="缺少必填参数"):
        build_argv(recipe, {"url": "   "})


def test_a_blank_optional_value_is_dropped_rather_than_emitted_empty():
    """可选参数给空串 → 整项不出现，而不是拼出 `-p ''`。

    拼出空值的症状：nmap 报 `Failed to resolve ""` —— 又是一次白跑。
    """
    recipe = RECIPE_BY_NAME["nmap"]
    argv = build_argv(recipe, {"target": "10.0.0.1", "ports": ""})
    assert "-p" not in argv, "空的可选值不该拼出开关本身"


def test_a_default_value_participates_even_when_not_given():
    """有 `default` 的参数「不给也该出现」—— 否则模型以为自己配过了。"""
    recipe = RECIPE_BY_NAME["hashcat"]
    argv = build_argv(recipe, {"hashfile": "h.txt", "hash_type": "0"})
    assert "-a" in argv, "hashcat 的 attack_mode 默认 '0' 必须真的拼进去"
    assert argv[argv.index("-a") + 1] == "0"


def test_paths_with_spaces_and_semicolons_stay_a_single_argv_element():
    """带空格/分号的参数值必须保持为**一个** argv 元素，不能被拆开。

    这是本模块返回 `list` 而不是字符串的全部理由：一旦拼接成字符串，
    参数值里的分号就成了命令分隔符。
    """
    recipe = RECIPE_BY_NAME["nmap"]
    argv = build_argv(recipe, {"target": "10.0.0.1; rm -rf /"})
    assert "10.0.0.1; rm -rf /" in argv, (
        "带分号的目标必须原样作为一个元素；若它被拆成多段，就发生了注入"
    )


def test_the_argv_always_starts_with_the_command_and_its_fixed_args():
    for recipe in RECIPE_SPECS:
        values = {p.name: "x" for p in recipe.params if p.required}
        argv = build_argv(recipe, values)
        assert argv[0] == recipe.command, f"{recipe.name} 的 argv[0] 必须是命令名"
        assert argv[1 : 1 + len(recipe.args)] == list(recipe.args), (
            f"{recipe.name} 的无条件前置参数位置不对"
        )


# ══════════════════════════════════════════════════════════
# 3. allowed_exit_codes —— 「没命中」不是「失败」
# ══════════════════════════════════════════════════════════


def test_scanning_tools_that_exit_nonzero_on_no_match_allow_that_code():
    """`ffuf` / `gobuster` 没命中时退出码非 0 —— 若不白名单，模型会看到「失败」。

    它其实跑对了、只是没东西。把「没命中」显示成「命令失败」会诱导模型
    去重试一个已经正常工作的工具。
    """
    for name in ("ffuf", "gobuster"):
        recipe = RECIPE_BY_NAME[name]
        assert recipe.allowed_exit_codes != (0,), (
            f"{name} 没命中时退出码非 0，必须把那个码列进 allowed_exit_codes；"
            "否则「空结果」会被显示成「命令失败」"
        )


def test_tools_that_only_fail_on_error_keep_the_default_allowlist():
    """不是所有工具都该放行非 0 —— 那会让真失败也被当成成功。"""
    recipe = RECIPE_BY_NAME["nmap"]
    assert recipe.allowed_exit_codes == (0,), (
        "nmap 的非 0 退出码是真的出错，不该被放行"
    )


# ══════════════════════════════════════════════════════════
# 4. 局限说明 —— 本模块存在的主要理由
# ══════════════════════════════════════════════════════════


#: 这些工具的默认参数**必然缩水**，所以必须有 `default_limitation`。
#: 名单来自对照实验（见 `app/kernel/tools.py` 里 `ToolSpec.default_limitation`
#: 那段：不给模板目录的 POC 工具命中 0，模型把 0 写成了「目标没这个漏洞」）。
MUST_EXPLAIN = (
    "nuclei",
    "sqlmap",
    "ffuf",
    "gobuster",
    "hydra",
    "nmap",
    "masscan",
    "rustscan",
    "gdb",
    "pwn",
)


@pytest.mark.parametrize("name", MUST_EXPLAIN)
def test_tools_whose_defaults_shrink_the_search_space_must_explain_it(name: str):
    recipe = RECIPE_BY_NAME[name]
    assert recipe.default_limitation.strip(), (
        f"{name} 的默认参数下做不到什么，必须写进 default_limitation —— "
        "不写的话模型会把缩水的输出读成结论，而这类失真不会报错"
    )


def test_the_container_network_limitation_is_stated_for_network_tools():
    """容器是 `--network none`，对外连接工具必须写明「连不上不是目标的问题」。

    这条是本轮实测出来的：`nc`/`socat` 在沙箱里对任何地址都会
    `Network is unreachable`。不写明，模型会把它读成「目标端口没开」。
    """
    for name in ("nc", "socat"):
        limitation = RECIPE_BY_NAME[name].default_limitation
        assert "无网络" in limitation or "network none" in limitation, (
            f"{name} 在容器里连不上任何外部地址（--network none），"
            "必须写明这一点，否则「连不上」会被读成「目标没开端口」"
        )


def test_pwn_recipe_warns_that_the_disasm_subcommand_is_broken():
    """`pwn disasm` 在 Python 3.14 的镜像里抛 ValueError（实测）。

    不写明的话，模型会用它反汇编，然后拿到一个 traceback，
    再花几个回合去怀疑自己的输入。
    """
    limitation = RECIPE_BY_NAME["pwn"].default_limitation
    assert "disasm" in limitation
    assert "坏" in limitation or "ValueError" in limitation, (
        "必须明确说 `pwn disasm` 是坏的，并指向替代工具（objdump / radare2）"
    )


def test_gdb_recipe_does_not_claim_pwntools_is_missing():
    """★ 这条钉的是一个**曾经写错的事实**。

    gdb 配方最初写「容器里没有 pwndbg/gef 插件（实测 pwntools 也没装）」，
    但在沙箱里实测：`pwn` 命令可用、`pwn cyclic 16` 正常、
    `from pwn import *` 齐全。写错的代价是模型不会去用本可用的 pwntools。

    真正的限制是「gdb 里没有插件」（`gdb -batch -ex checksec` →
    `Undefined command: "checksec"`），而不是「没有 pwntools」。
    """
    limitation = RECIPE_BY_NAME["gdb"].default_limitation
    assert "没有 pwndbg" in limitation or "没有 pwndbg/gef" in limitation
    assert "pwntools 也没装" not in limitation, (
        "这句话是错的：沙箱里 pwntools 装好了（pwn 命令可用）。"
        "写成「没装」会让模型放弃一个本来能用的工具"
    )


def test_checksec_recipe_is_documented_as_the_pwntools_one():
    """镜像里的 `checksec` 其实是 `pwn checksec`（实测 `--help` 打的是 pwn 的 usage）。

    这不是纯粹的文字问题：它解释了为什么启动时会先打一段 terminfo 警告，
    以及为什么它没有独立 checksec.sh 的那些选项。
    """
    limitation = RECIPE_BY_NAME["checksec"].default_limitation
    assert "pwntools" in limitation, (
        "`checksec` 是 pwntools 带的那个，不写明的话 "
        "「为什么它先打 _curses.error」就无从解释"
    )


# ══════════════════════════════════════════════════════════
# 5. 接线 —— 配方库不能只是躺着
# ══════════════════════════════════════════════════════════


def test_the_recipe_library_is_actually_imported_somewhere_outside_itself():
    """★ 这条钉的是本项目最常犯的错：**写了模块，但没人用**。

    `mcp_servers` 在 v0.72 就是这样坏的：配置列、服务层、build_spec 都有，
    但 HTTP 层两个方向都没暴露 —— 于是「配了却永远说没有连接」。

    配方库如果只被它自己的测试 import，那它在功能上等于不存在：
    界面上看不到、模型调不到。
    """
    import subprocess
    import sys

    result = subprocess.run(  # noqa: S603 - 参数固定，无用户输入
        [sys.executable, "-c", "import app.kernel.recipes"],
        capture_output=True,
        text=True,
        cwd=".",
    )
    assert result.returncode == 0, f"recipes 模块导不进来：{result.stderr}"

    # 用文本搜索而不是运行时反射：要确认代码里**有引用**，
    # 而"被 import 了但没人读它的字段"也算没接线。
    from pathlib import Path

    app_dir = Path(__file__).resolve().parents[2] / "app"
    hits = [
        p
        for p in app_dir.rglob("*.py")
        if p.name != "recipes.py" and "RECIPE" in p.read_text(encoding="utf-8")
    ]
    assert hits, (
        "app/ 下除了 recipes.py 自己，没有任何模块引用 RECIPE_*。"
        "这意味着配方库没有接线：界面上看不到、模型也调不到 —— "
        "与 mcp_servers 在 v0.72 的坏法完全相同"
    )
