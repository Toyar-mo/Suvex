"""应急响应场景包（v0.41）：分类门禁、提示词追加、镜像路由、工单只出不做。

## 这个文件守的是五条**互相独立**的承诺

| # | 承诺 | 失效时会怎样 |
| --- | --- | --- |
| 1 | 开关关着时，三个 `ir-*` 分类**不合法** | IR 题悄悄入库，行为按解题模式跑 |
| 2 | 开关关着时，普通五类 + `agentsec-*` 的 prompt **逐字节不变** | 加一个场景包把前面所有场景的提示词都改了 |
| 3 | `ir-*` 分类路由到 `ctf2-sandbox-ir` 而不是 base | 复盘题跑在没有 `mactime` 的镜像里 → command not found |
| 4 | IR 提示词里**不能出现**攻击工具名 | 提示词自己教模型去打，纪律靠模型自觉 |
| 5 | IR 报告/处置建议**不出现**"已执行"类措辞 | 从"建议"越界成"处置"，这是安全边界 |

## 为什么第 2 条要连 `agentsec-*` 一起测

v0.41 的改法是"把 IR 塞进 agentsec 那条同构分支"。这种改法最容易出的错
是**顺手重排了公共部分** —— 比如把 `if agentsec:` 换成
`if agentsec or ir:` 时，把 `agentsec_enabled=False` 的早退分支写漏一个
条件。症状不是报错，是"某场比赛的提示词突然多了一段"。

所以三个场景包（普通 / agentsec / ir）要**两两交叉**地验：
只有本题属于某族、且该族开关打开时，才多加那一段；其余所有组合都是原样。

## 为什么第 3 条要单独测

`profile_for_category` 是**一处**分类→镜像的规则（`service._profile_for`
只是转调）。它有两个已知的静默失败面：

  · 子串匹配 → `forensics` 含 `re`，取证题全进逆向镜像；
  · 前缀写错 → 裸 `ir` 不以 `ir-` 开头，全进 base 镜像。

两者都不报错。第一个的症状是"取证题也能跑"（re 是 base 超集），第二个
的症状是"IR 题上 mactime 找不到"——而后者会被误读成"工具没装"。
"""

from __future__ import annotations

import pytest

from app.core.vocab import (
    AGENTSEC_CATEGORIES,
    IR_CATEGORIES,
    IR_CATEGORY_PREFIX,
    Ctf2SandboxProfile,
    IrCategory,
)
from app.ctf2 import notes
from app.ctf2.runner import (
    build_system_prompt,
    image_for_profile,
    load_agentsec_prompt,
    load_ir_prompt,
    load_system_prompt,
    profile_for_category,
)

# ══════════════════════════════════════════════════════════
# 词表本身
# ══════════════════════════════════════════════════════════


def test_three_ir_categories() -> None:
    """恰好三类，且全部带统一前缀。"""
    assert len(IrCategory) == 3
    assert IR_CATEGORIES == {"ir-root-cause", "ir-timeline", "ir-scope"}
    assert all(c.value.startswith(IR_CATEGORY_PREFIX) for c in IrCategory)


def test_ir_prefix_does_not_collide_with_agentsec() -> None:
    """两族前缀不能互相包含 —— 否则 `startswith` 判定会互相吃掉。

    这条不是理论风险：`ir-` 是 `agentsec-` 的子串吗？不是。但反过来的
    检查必须显式做一次，因为将来若有人把 IR 改叫 `asir-`，两族的
    `startswith` 判定会同时命中，而**顺序**决定一切。
    """
    assert not IR_CATEGORY_PREFIX.startswith("agentsec")
    assert not "agentsec-".startswith(IR_CATEGORY_PREFIX)
    assert not (IR_CATEGORIES & AGENTSEC_CATEGORIES)


def test_ir_profile_value_is_the_image_name() -> None:
    """profile 取值 == 镜像短名，不做第二层映射（`vocab` 的既有约定）。"""
    assert Ctf2SandboxProfile.IR.value == "ctf2-sandbox-ir"


def test_five_existing_categories_are_not_ir() -> None:
    """现有五类不能被误判成 IR —— 这是"不动老题"的第一道闸。"""
    for name in ("pwn", "web", "reverse", "crypto", "misc", "Pwn", "Web", ""):
        assert not notes.is_ir_category(name)


def test_forensics_is_not_ir_but_looks_similar() -> None:
    """`forensics` 与 IR 语义相近，但**不是** IR 分类。

    这是刻意留的一条：取证（题目里给你一个镜像让你取证）与应急响应
    （真实入侵后复盘）是两件事，前者走 base 镜像、后者走 ir 镜像。
    混起来的话，取证题的提示词会突然变成"你没有处置权限"那套话 ——
    而取证题本来就只是在分析附件。
    """
    assert not notes.is_ir_category("forensics")
    assert profile_for_category("forensics") == Ctf2SandboxProfile.BASE.value


# ══════════════════════════════════════════════════════════
# 分类门禁（承诺 1）
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize("category", sorted(IR_CATEGORIES))
def test_ir_category_rejected_when_disabled(category: str) -> None:
    """开关关着 → 三个分类一律拒绝（**抛错**，不是静默降级）。"""
    with pytest.raises(ValueError, match="未启用"):
        notes.validate_note_category(
        category, agentsec_enabled=False, ir_enabled=False
    )


@pytest.mark.parametrize("category", sorted(IR_CATEGORIES))
def test_ir_category_accepted_when_enabled(category: str) -> None:
    """开关开着 → 三个分类一律放行，且原样返回。"""
    assert notes.validate_note_category(
        category, agentsec_enabled=False, ir_enabled=True
    ) == category


@pytest.mark.parametrize(
    "category", ["pwn", "web", "reverse", "crypto", "misc", "Pwn", "MISC", ""]
)
def test_existing_categories_pass_regardless_of_ir_switch(category: str) -> None:
    """现有五类不受 IR 开关影响 —— 关着放行、开着也放行。"""
    assert notes.validate_note_category(
        category, agentsec_enabled=False, ir_enabled=False
    ) == category
    assert notes.validate_note_category(
        category, agentsec_enabled=False, ir_enabled=True
    ) == category


def test_unknown_ir_category_rejected_even_when_enabled() -> None:
    """拼错的 `ir-*` 必须被拒 —— 否则它会静默退回普通解题模式。"""
    with pytest.raises(ValueError, match="未知的应急响应分类"):
        notes.validate_note_category(
            "ir-typo", agentsec_enabled=False, ir_enabled=True
        )


def test_agentsec_still_gated_by_its_own_switch_when_ir_on() -> None:
    """★ 交叉用例：开着 IR 开关**不能**顺手放行 `agentsec-*`。

    这是"两族共用一个校验函数"这种重构最典型的溢出：把
    `if not agentsec_enabled` 改成 `if not (agentsec_enabled or ir_enabled)`
    之后，开 IR 就等于开 agentsec，而两者的冲突行为准则会同时进上下文。
    """
    with pytest.raises(ValueError, match="未启用"):
        notes.validate_note_category(
            "agentsec-prompt-injection", agentsec_enabled=False, ir_enabled=True
        )


def test_ir_still_gated_by_its_own_switch_when_agentsec_on() -> None:
    """反向交叉：开着 agentsec 不能顺手放行 `ir-*`。"""
    with pytest.raises(ValueError, match="未启用"):
        notes.validate_note_category(
            "ir-root-cause", agentsec_enabled=True, ir_enabled=False
        )


def test_ir_whitespace_is_normalized() -> None:
    """前后空白不该造出一个"看起来合法其实不匹配"的取值。"""
    assert notes.validate_note_category(
        "  ir-timeline  ", agentsec_enabled=False, ir_enabled=True
    ) == "ir-timeline"


# ══════════════════════════════════════════════════════════
# 提示词追加（承诺 2）
# ══════════════════════════════════════════════════════════

_NORMAL = ("pwn", "web", "reverse", "crypto", "misc", "")


@pytest.mark.parametrize("category", _NORMAL)
def test_system_prompt_byte_identical_when_ir_disabled(category: str) -> None:
    """承诺 2：关着时**逐字节**等于原提示词（含尾部空白）。"""
    assert build_system_prompt(category, agentsec_enabled=False, ir_enabled=False) == (
        load_system_prompt()
    )


@pytest.mark.parametrize("category", sorted(IR_CATEGORIES))
def test_ir_prompt_byte_identical_when_disabled(category: str) -> None:
    """关着时即便分类是 `ir-*` 也不追加 —— "关"就是全关。"""
    assert build_system_prompt(category, agentsec_enabled=False, ir_enabled=False) == (
        load_system_prompt()
    )


@pytest.mark.parametrize("category", _NORMAL)
def test_system_prompt_unchanged_for_normal_category_when_ir_enabled(
    category: str,
) -> None:
    """开着但本题是普通题 → 同样逐字节不变。

    ⚠️ 这条最容易被漏掉：开关打开后，**普通题也必须一个字不变**。
    """
    assert build_system_prompt(category, agentsec_enabled=False, ir_enabled=True) == (
        load_system_prompt()
    )


@pytest.mark.parametrize("category", sorted(IR_CATEGORIES))
def test_ir_prompt_is_appended_not_replacing(category: str) -> None:
    """承诺：原提示词是**前缀**，IR 块是**后缀**，中间不夹带。"""
    base = load_system_prompt()
    block = load_ir_prompt()
    got = build_system_prompt(category, agentsec_enabled=False, ir_enabled=True)

    assert got.startswith(base), "原提示词必须是前缀（不能被替换或重排）"
    assert got.endswith(block), "IR 块必须完整落在末尾"
    assert got == f"{base}\n{block}", "除了一个换行，不得插入任何别的字符"


@pytest.mark.parametrize("category", sorted(AGENTSEC_CATEGORIES))
def test_agentsec_prompt_unchanged_when_ir_enabled(category: str) -> None:
    """★ 交叉用例：开 IR 不能改变 `agentsec-*` 题的提示词形状。

    开着 IR、关着 agentsec 时，agentsec 题拿到的必须**只有** base。
    这条盯的是"两个开关合并成一个条件"的写法。
    """
    assert build_system_prompt(
        category, agentsec_enabled=False, ir_enabled=True
    ) == load_system_prompt()


@pytest.mark.parametrize("category", sorted(IR_CATEGORIES))
def test_ir_prompt_unchanged_when_agentsec_enabled(category: str) -> None:
    """反向交叉：开 agentsec 不能给 IR 题追加 agentsec 块。"""
    assert build_system_prompt(
        category, agentsec_enabled=True, ir_enabled=False
    ) == load_system_prompt()


def test_both_prompts_appended_when_category_matches_neither() -> None:
    """两个开关都开、但分类不属于任何一族 → 仍然只有 base。

    这一条把"开关开着就注入"这种写法彻底排除掉：注入的**第二个条件**
    永远是"本题属于这一族"。
    """
    for category in _NORMAL:
        assert build_system_prompt(
            category, agentsec_enabled=True, ir_enabled=True
        ) == load_system_prompt()


def test_ir_block_keeps_all_sections() -> None:
    """IR 块的六个小节标题必须都在 —— "完整"是可验证的，不是感觉。"""
    block = load_ir_prompt()
    for heading in (
        "## 应急响应与根因复盘",
        "### 一、",
        "### 二、",
        "### 三、",
        "### 四、",
        "### 五、",
        "### 六、",
    ):
        assert heading in block, f"IR 块缺了小节：{heading}"


def test_ir_block_states_the_three_verdicts() -> None:
    """三档证据分级必须写进提示词 —— 否则模型不知道 `ruled_out` 是什么。"""
    block = load_ir_prompt()
    for verdict in ("confirmed", "needs_follow_up", "ruled_out"):
        assert verdict in block, f"IR 块没提证据分级 {verdict!r}"


# ══════════════════════════════════════════════════════════
# 镜像路由（承诺 3）
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize("category", sorted(IR_CATEGORIES))
def test_ir_category_routes_to_ir_image(category: str) -> None:
    """承诺 3：三个 IR 分类都路由到 `ctf2-sandbox-ir`。"""
    assert profile_for_category(category) == Ctf2SandboxProfile.IR.value


def test_ir_image_name_comes_from_profile() -> None:
    """`image_for_profile` 对 IR profile 回 IR 镜像（不是退回 base）。"""
    assert (
        image_for_profile(Ctf2SandboxProfile.IR.value) == Ctf2SandboxProfile.IR.value
    )


def test_ir_routing_is_normalized() -> None:
    """大小写与空白不该影响路由 —— 分类名来自平台，格式不可控。"""
    assert profile_for_category("  IR-Root-Cause  ") == Ctf2SandboxProfile.IR.value


def test_unknown_category_still_routes_to_base() -> None:
    """认不出来时仍回 base（v0.41 不能改变这条既有行为）。"""
    assert profile_for_category("something-else") == Ctf2SandboxProfile.BASE.value
    assert profile_for_category(None) == Ctf2SandboxProfile.BASE.value


def test_reverse_categories_still_route_to_reverse() -> None:
    """既有路由一个都不能坏。"""
    for category in ("reverse", "rev", "re", "逆向", "逆向工程", "反汇编"):
        assert profile_for_category(category) == Ctf2SandboxProfile.REVERSE.value


def test_forensics_does_not_leak_into_reverse() -> None:
    """★ `forensics` 含子串 `re` —— 这条守的是"别用子串匹配"。

    用 `"re" in category` 判的话，取证题会全进逆向镜像。症状不是报错，
    是"取证题也能跑"（re 镜像是 base 超集），于是**没人会发现**。
    """
    assert profile_for_category("forensics") == Ctf2SandboxProfile.BASE.value


# ══════════════════════════════════════════════════════════
# 安全边界（承诺 4 / 5）
# ══════════════════════════════════════════════════════════

#: 攻击工具的命令名。IR 提示词里**一个都不能出现** ——
#: 理由：模型会照着提示词里提到的工具名去试。提示词点名 sqlmap，
#: 就等于在"别打"的纪律旁边放了一把锤子。
_FORBIDDEN_TOOLS = (
    "nmap",
    "sqlmap",
    "ffuf",
    "gobuster",
    "dirb",
    "nikto",
    "hydra",
    "hashcat",
    "john ",
    "metasploit",
    "msfconsole",
    "msfvenom",
)


@pytest.mark.parametrize("tool", _FORBIDDEN_TOOLS)
def test_ir_prompt_never_names_attack_tool(tool: str) -> None:
    """承诺 4：IR 提示词不点名任何攻击工具。"""
    assert tool not in load_ir_prompt().lower(), (
        f"IR 提示词里出现了攻击工具 {tool!r} —— 那会让模型去试它"
    )


def test_ir_prompt_forbids_response_actions() -> None:
    """承诺 5：提示词必须明令禁止处置动作，且**写死**不留后门。"""
    block = load_ir_prompt()
    # 必须有明确的"不做处置"表述
    assert "不改配置" in block or "不做任何处置" in block or "不删文件" in block
    # 必须明确"只出建议，执行由人做"
    assert "执行永远由人做" in block or "只输出建议" in block


def test_ir_prompt_requires_evidence_anchor() -> None:
    """每条结论必须带 `文件:行号` 之类的锚点 —— 这是可复现的判据。"""
    block = load_ir_prompt()
    assert "文件:行号" in block or "文件：行号" in block or ":行号" in block


def test_ir_prompt_grading_is_per_claim_not_per_task() -> None:
    """★ 分级要落到**每条结论**，不是整个任务一个总分。

    这是评委批评"数据没说服力、只有数量"在机制层的对应修复：
    一个笼统的"完成"无法被追问，而每条 claim 带级别就可以。
    """
    block = load_ir_prompt()
    assert "每条" in block or "逐条" in block


def test_ir_prompt_no_false_execution_claim() -> None:
    """提示词不能教模型写"已阻断/已隔离"这类假执行声明。"""
    block = load_ir_prompt()
    assert "已阻断" in block and "不要" in block


def test_ir_prompt_documents_read_attachment_single_file_limit() -> None:
    """★ `read_attachment` 一次只收**一个**文件名 —— 必须写进提示词。

    ## 为什么这条值得单独一条用例

    这个限制来自包装层（`ctf2/notes.py:730` 附近说明的三个原子工具
    `run_shell`/`http_request`/`read_attachment` 是我们自己的包装，实际
    执行在 strix 容器里），所以它**不是模型能自己发现的**：模型按常识写
    `read_attachment("*.log")`，拿回一句"文件不存在"。

    而"文件不存在"在应急响应语境里有一个**看起来非常合理**的解读：
    「这条日志根本没采」。实测踩过这一步 —— 结论从"我没读到"变成
    "日志缺口"，再变成 `ruled_out`，而真实原因只是通配符不支持。

    提示词里那一段必须同时做到三件事：说清限制、给出替代动作（先 `ls`）、
    把这个坑的**后果**写出来（否则它读起来只是一条无关紧要的备注）。
    """
    block = load_ir_prompt()

    assert "read_attachment" in block
    # 限制本身
    assert "一次只收一个文件名" in block
    # 替代动作
    assert "ls" in block
    # 后果 —— 少了这句，读者不会当回事
    assert "日志没采" in block or "没有采" in block
