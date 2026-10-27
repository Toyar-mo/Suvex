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


def test_network_limitation_distinguishes_the_two_sandbox_modes():
    """`nc`/`socat` 的局限说明必须分清**两种沙箱**，不能一句「无网络」了事。

    ## 为什么这条断言在本轮被重写

    原断言是 `"无网络" in limitation`。它在 v0.98 是对的：那时只有一个
    解题档（`--network none`）。v0.107 加了对话档（`suvex-agent` 网络，
    能真出网），同一份配方库被两档共用，于是那句话就变成了一半真一半假：

      - 在解题沙箱里跑 `nc`，确实 `Network is unreachable` —— 旧话正确。
      - 在对话沙箱里跑同一条 `nc`，它能真连出去 —— 旧话在这里是**错的**。

    而错的方向很坏：模型读到「一定失败」，就会在**能连的时候**也放弃
    尝试，或者把连上了的输出当作不可能、进而怀疑自己的命令写错了。

    所以断言从「提到无网络」升级为「必须提到两档并给出判别办法」——
    单纯的措辞扩充不算通过，得有可执行的 `ip -o addr` 判别。
    """
    for name in ("nc", "socat"):
        limitation = RECIPE_BY_NAME[name].default_limitation
        assert "network none" in limitation, (
            f"{name}：解题沙箱是 --network none，连不上必须写明"
        )
        assert "对话沙箱" in limitation, (
            f"{name}：对话沙箱（v0.107 起）挂 suvex-agent 网络、能真连出去。"
            "只写「无网络」会让模型在能连的时候也放弃尝试 —— 这比不写更坏。"
        )
        assert "ip -o addr" in limitation, (
            f"{name}：光说「两档不同」不够，模型无法从这句话知道**自己当前"
            "在哪一档**。必须给出可执行的判别办法（`ip -o addr` 看有没有"
            "非 loopback 地址）。"
        )


def test_exec_recipe_does_not_flatten_the_two_modes():
    """`exec` 的局限说明同样不能把对话档说成只读、无网。

    这是用户抱怨「在这个对话里我没有执行环境」的同一类失真：模型相信
    自己做不到，就不会去试。对话档根可写、能出网、能装包，这些都该说。
    """
    limitation = RECIPE_BY_NAME["exec"].default_limitation
    assert "对话沙箱" in limitation, (
        "exec 的局限说明必须提到对话沙箱档，否则模型会以为对话里也只能"
        "在只读无网环境里跑命令"
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
#
# ★★ v0.99：这一节原来只有一条"app/ 下有没有字符串 RECIPE"的文本搜索。
# 它**在 v0.98 状态就是绿的**，而当时模型根本看不到配方 —— API 列表那一处
# 引用就满足了它。也就是说：那条测试测的是"有人提过这个名字"，
# 不是"知识到了模型手上"。
#
# 这是本项目最贵的一类假绿灯：测试是绿的，功能是死的。
# 下面改成三条，各钉一个**具体失效位**：
#   ① 清单真的进了系统提示词（不是只进了 API）；
#   ② `load_recipe` 在工具表里，且 dispatch 真的有这个分支；
#   ③ 模型能从清单里读到"默认会漏"这句 —— 那是本模块存在的理由。


def test_recipe_index_reaches_the_system_prompt():
    """★ 配方清单必须出现在**模型真正收到的那份提示词**里。

    失效形态（v0.98 的真实状态）：`recipes.py` 写好了、`GET /kernel/recipes`
    通了、工具页上能看到 34 条 —— 而系统提示词里一个字都没有。
    模型照着默认参数跑 nmap，把"只扫了 1000 个端口"读成"目标只开了这些"。

    这条断言直接读 `load_system_prompt()` 的返回值，而不是读源码文本：
    文本里出现 `RECIPE_INDEX` 只证明写了占位符，不证明它被替换了。
    """
    from app.kernel.runner import load_system_prompt

    prompt = load_system_prompt()
    assert "{{RECIPE_INDEX}}" not in prompt, (
        "系统提示词里还留着未替换的 {{RECIPE_INDEX}} 占位符 —— "
        "模型会原样看到这串东西"
    )
    assert "load_recipe" in prompt, (
        "系统提示词里没有 load_recipe —— 模型不知道这个工具存在，"
        "配方库退化成只有人能看到的东西"
    )
    # ★★ 这里**不能**只断言 `` `nmap` `` 在提示词里 —— 变异验证抓过这个洞。
    #
    # `nmap` 这个词在提示词里还有三处别的来源：
    #   · 技能清单里那篇 `tooling/nmap.md`（"nmap：Canonical Nmap CLI syntax…"）
    #   · 手法块里"`nmap` 只用来确认端口"
    #   · 别处的 `command -v gobuster ffuf nmap`
    # 所以 `` `nmap` in prompt `` 在配方清单**整个没渲染**时照样为真。
    #
    # 要区分"配方清单到了模型手上"，只能断言**配方清单独有的形态**：
    # 每一条都以 `- \`名字\`` 起头并带「默认会漏」。
    recipe_lines = [
        line
        for line in prompt.splitlines()
        if line.startswith("- `") and "默认会漏" in line
    ]
    assert len(recipe_lines) >= 30, (
        f"提示词里只有 {len(recipe_lines)} 行带「默认会漏」的配方条目，"
        "而配方库有 34 条 —— 配方索引没真的渲染进去"
        "（占位符被替换成一句废话也算这种情况）"
    )
    joined = "\n".join(recipe_lines)
    for name in ("nmap", "sqlmap", "ffuf"):
        assert f"`{name}`" in joined, f"配方清单里没有 `{name}`"


def test_recipe_index_carries_the_default_limitation_inline():
    """★ 清单里必须带一句「默认会漏」，而且这句要**真的到模型手上**。

    ## 为什么这条是独立的一条用例

    因为它是**这一节唯一不能省的东西**。选择发生在调用之前：模型决定
    "用 nmap 扫一下"的那一刻，如果清单只说「nmap：端口扫描」，
    它就会拿默认结果写结论。等它 `load_recipe` 看到局限，
    那个结论已经写进上一轮推理了。

    ## ★★ 为什么断言打在 `load_system_prompt()` 上而不是 `render_index()` 上

    这是第一次写这条时留下的真洞，被变异验证抓出来的：原来只断言
    `render_index()` 的输出里有「默认会漏」。把提示词里的
    `{{RECIPE_INDEX}}` 换成一句废话之后，渲染函数照样正确、
    这条照样绿 —— 而模型那边一个字都收不到。

    **渲染函数对 ≠ 内容到达模型。** 中间那个占位符替换才是会断的一环。
    所以现在一律从 `load_system_prompt()` 的返回值里取那一行。
    """
    from app.kernel.runner import load_system_prompt

    prompt = load_system_prompt()
    assert "默认会漏" in prompt, (
        "模型收到的系统提示词里没有「默认会漏」标记 —— "
        "清单要么没渲染进去，要么被渲染成了一列光秃秃的工具名"
    )
    # nmap 那一行必须自己承载"默认只扫一部分"这个判断。
    #
    # ⚠️ 必须**先切出配方清单那一节**再找 —— 技能清单里也有一行
    # `` - `nmap`：Canonical Nmap CLI syntax… ``，直接取"第一个含 nmap 的行"
    # 会抓到技能那一行，然后断言在一条本来就不该有局限的行上失败。
    # （这个坑是跑出来的：它让这条用例在**正确实现**上变红。）
    marker = "可用工具配方"
    start = prompt.find(marker)
    assert start >= 0, "提示词里没有「可用工具配方」这一节标题"
    section = prompt[start:]
    nmap_lines = [line for line in section.splitlines() if line.startswith("- `nmap`")]
    assert nmap_lines, "配方清单里没有 nmap 那一行"
    nmap_line = nmap_lines[0]
    assert "默认会漏" in nmap_line, f"nmap 那一行没带局限：{nmap_line}"
    assert "1000" in nmap_line or "端口" in nmap_line, (
        f"nmap 的局限被渲染成了空话，看不出「默认只扫一部分」：{nmap_line}"
    )


def test_load_recipe_is_dispatchable_and_returns_full_recipe():
    """★ `load_recipe` 必须在 `TOOL_NAMES` 里**且** dispatch 真的有分支。

    两个失效位不同：
      · 不在 `TOOL_NAMES` → 提示词说"你有这个工具"，调用却被当成未知工具；
      · 在 `TOOL_NAMES` 但 dispatch 没分支 → 同上，而且更隐蔽
        （工具表里看得见，调起来回「未知工具」）。

    这里用 `inspect.getsource` 确认分支存在 —— 因为 dispatch 是
    一个 async 大函数，没有可独立调用的入口（它要 sandbox/spec）。
    """
    import inspect

    from app.kernel import runner

    assert "load_recipe" in runner.TOOL_NAMES, "load_recipe 不在工具表里"

    src = inspect.getsource(runner.dispatch_tool)
    assert 'if name == "load_recipe"' in src, (
        "dispatch_tool 里没有 load_recipe 分支 —— 工具表说它存在，"
        "实际调用会落到「未知工具」"
    )

    # 全文形态：必须含可复制的命令行与完整局限
    full = recipes_mod.render_recipe("sqlmap")
    assert "sqlmap -u" in full, f"全文里没有可复制的命令行：{full[:200]}"
    assert "默认参数下它做不到什么" in full, "全文里没有局限那一节"
    assert "<url>" in full, (
        "必填参数没渲染成 <占位符> —— 填一个像样的值会让模型直接复制走"
    )


def test_load_recipe_unknown_name_returns_available_list():
    """名字拼错时回**可用清单**，不是空串。

    与 `skills.load_skill_text` 同一条纪律：空串会让模型反复猜名字，
    每猜一次烧一个回合。
    """
    got = recipes_mod.render_recipe("no-such-recipe-xyz")
    assert "没有名为" in got
    assert "nmap" in got and "sqlmap" in got, "没回可用清单，模型无从纠正"


def test_recipes_module_imports_cleanly():
    """模块本身能干净导入（一致性断言在 import 时执行，见模块末尾）。

    ⚠️ 这条**故意不再是**「app/ 下有没有字符串 RECIPE」那种文本搜索。
    那种断言在 v0.98——配方完全没进提示词的状态——就是绿的，
    因为 `service.py` 的 API 列表满足了它。测"有人提过这个名字"
    与测"知识到了模型手上"是两件事，后者见本节上面四条。
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
