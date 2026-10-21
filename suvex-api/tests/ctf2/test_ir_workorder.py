"""处置建议工单（v0.41，会话 C 工单）。

## 这个文件守什么

工单（m00878 会话 C）的四条要求逐条对应：

1. **四段齐全才算完整，缺段强制降级** —— `test_missing_field_forces_downgrade`；
2. **只出工单，不执行** —— `test_no_execution_path_in_module`（AST 扫，
   不是 grep 字符串）+ `test_render_never_claims_executed`；
3. **工单文件头必须是否定句** —— `test_header_denies_execution`；
4. **复用现有报告导出，不新建格式** —— `test_renders_markdown_reusable_by_existing_export`。

## ★ 为什么 2 用 AST 而不是 grep

grep 只能查"有没有出现某个字符串"，而这条约束要否证的是**能力**：
没有 `subprocess`、没有 `os.system`、没有 `requests`。用 grep 写，
漏一次（比如写成 `__import__("subprocess")`）就永远查不出来；
用 AST 则必须**显式列出**所有 import 与所有 `Call` 的 func 名，
漏掉的形状会以"未在白名单里"的形式暴露出来。

反过来，这条测试也是**唯一**能防止"半年后有人顺手接一下 EDR"的机制
—— 见 `workorder` 模块 docstring 的「为什么不留后门」。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.ctf2 import workorder

MODULE_PATH = Path(workorder.__file__)


# ══════════════════════════════════════════════════════════
# 一、解析容错
# ══════════════════════════════════════════════════════════


def test_missing_workorders_key_is_empty_not_error() -> None:
    """整个 `workorders` 键缺失 → 空列表，不是异常。

    普通 CTF 题的 `FINAL` 里根本没有这个键（提示词没让它写），
    而那是**绝大多数**任务。这条路径抛异常等于把普通解题搞坏。
    """
    assert workorder.parse_workorders({}) == []
    assert workorder.parse_workorders(None) == []
    assert workorder.parse_workorders({"workorders": None}) == []


def test_non_list_workorders_is_empty() -> None:
    """模型把 `workorders` 写成对象或字符串 → 空列表。

    ⚠️ 不能"尽力理解"：一个 dict 可能是单条工单、也可能是
    `{"1": {...}, "2": {...}}`，两种猜法给出不同结果，而猜错
    不会有任何信号。
    """
    assert workorder.parse_workorders({"workorders": {"action": "x"}}) == []
    assert workorder.parse_workorders({"workorders": "封 IP"}) == []


def test_one_bad_item_does_not_drop_the_batch() -> None:
    """一条不是对象 → 跳过它，其余照收。

    这是"整批丢弃"与"丢一条"的分界线：一条坏数据不该让另外三条
    真实发现凭空消失。
    """
    orders = workorder.parse_workorders(
        {
            "workorders": [
                "这不是对象",
                {"action": "封禁 203.0.113.45", "basis": "b", "risk": "r", "permission": "p"},
                None,
                {"action": "隔离 web-01", "basis": "b", "risk": "r", "permission": "p"},
            ]
        }
    )
    assert [o.action for o in orders] == ["封禁 203.0.113.45", "隔离 web-01"]


def test_item_without_action_is_dropped() -> None:
    """**唯一**一种丢弃：连 `action` 都空。

    没有动作的"工单"不是工单，是感想 —— 它没法被审批，也没法被执行。
    对照 `test_missing_field_forces_downgrade`：缺其他三段**保留**。
    """
    orders = workorder.parse_workorders(
        {"workorders": [{"basis": "b", "risk": "r", "permission": "p"}, {"action": "  "}]}
    )
    assert orders == []


def test_missing_field_kept_but_flagged() -> None:
    """缺 `risk` 的工单**保留**，但记进 `missing`。

    理由（见 `parse_workorders` docstring）：一条缺 `risk` 的建议
    仍然值得人看（"他们想到了要封这个 IP，只是没写风险"），
    丢掉它等于让一条真实发现消失。
    """
    orders = workorder.parse_workorders(
        {"workorders": [{"action": "封禁 203.0.113.45", "basis": "b", "permission": "p"}]}
    )
    assert len(orders) == 1
    assert orders[0].missing == ["risk"]
    assert orders[0].complete is False


def test_missing_field_forces_downgrade() -> None:
    """★ 缺段的工单**级别被强制降为 `needs_follow_up`**。

    这是工单要求的"四段齐全"从提示词变成代码。反例（改坏的样子）：
    一段缺 `risk` 的工单保留 `confirmed`，人审批时看到"证据充分"，
    而它没告诉你误做会怎样。
    """
    orders = workorder.parse_workorders(
        {
            "workorders": [
                {
                    "action": "封禁 203.0.113.45",
                    "basis": "auth.log:1423",
                    "permission": "防火墙运维",
                    "grade": "confirmed",  # ← 模型自己写了 confirmed
                }
            ]
        }
    )
    assert orders[0].grade == "confirmed"  # 原始值不改
    assert orders[0].approved_grade == "needs_follow_up"  # 但对外一律降级


def test_complete_order_keeps_grade() -> None:
    """四段齐全时级别原样通过 —— 上面那条降级不能变成"一律降"。"""
    orders = workorder.parse_workorders(
        {
            "workorders": [
                {
                    "action": "封禁 203.0.113.45",
                    "basis": "auth.log:1423 暴力破解成功",
                    "risk": "若为办公出口会断网",
                    "permission": "防火墙运维",
                    "grade": "confirmed",
                }
            ]
        }
    )
    assert orders[0].complete is True
    assert orders[0].approved_grade == "confirmed"


@pytest.mark.parametrize("raw", ["ruled_out", "确认", "", None, "high", "confirmed!"])
def test_grade_is_whitelisted(raw: object) -> None:
    """`grade` 只认 `confirmed` / `needs_follow_up`，其余一律降级。

    `ruled_out` 特别列出来：它**不是**处置级别（排除掉的事不需要处置），
    但模型很可能会写。降级是唯一安全的方向 —— 宁可低估不可高估。

    ⚠️ 大小写与首尾空白**不算错**（见 `test_grade_case_is_normalized`）：
    把它归到这一组会把"模型写了 `CONFIRMED`"和"模型写了中文"混成
    同一类失败，而前者只是格式，后者是理解错了。
    """
    orders = workorder.parse_workorders(
        {
            "workorders": [
                {
                    "action": "封禁 203.0.113.45",
                    "basis": "b",
                    "risk": "r",
                    "permission": "p",
                    "grade": raw,
                }
            ]
        }
    )
    assert orders[0].approved_grade == "needs_follow_up"


@pytest.mark.parametrize("raw", ["CONFIRMED", "Confirmed", "confirmed ", "  confirmed  "])
def test_grade_case_is_normalized(raw: str) -> None:
    """大小写与空白是格式问题，归一化后接受 —— 不因此降级。

    判据：这几种写法的**语义是确定的**（都指"证据充分"），把它降级
    等于让一个纯粹的大小写差异改变一条处置建议的可信级别。
    """
    orders = workorder.parse_workorders(
        {
            "workorders": [
                {
                    "action": "封禁 203.0.113.45",
                    "basis": "b",
                    "risk": "r",
                    "permission": "p",
                    "grade": raw,
                }
            ]
        }
    )
    assert orders[0].approved_grade == "confirmed"


def test_max_workorders_cap() -> None:
    """超过上限的截断，不是全收。

    处置建议超过 10 条通常说明分类没做，不是真的需要 10 个动作。
    """
    raw = [
        {"action": f"动作 {i}", "basis": "b", "risk": "r", "permission": "p"}
        for i in range(workorder.MAX_WORKORDERS + 5)
    ]
    orders = workorder.parse_workorders({"workorders": raw})
    assert len(orders) == workorder.MAX_WORKORDERS


@pytest.mark.parametrize(
    "value",
    [
        ["a", "b"],  # 模型实测会写成列表
        {"k": "v"},
        123,
        True,  # ⚠️ bool 是 int 的子类，必须单独排除
        None,
    ],
)
def test_field_shapes_are_coerced_not_crashed(value: object) -> None:
    """字段被写成列表/对象/数字 → 收成可读字符串，不抛。

    模型实测会把 `"risk"` 写成 `["a","b"]`。这里**不猜结构**
    （与 `extract_final_analysis` 同一条纪律），只做"能变字符串就变"。
    """
    orders = workorder.parse_workorders(
        {"workorders": [{"action": "封禁", "basis": value, "risk": value, "permission": value}]}
    )
    assert len(orders) == 1
    assert isinstance(orders[0].basis, str)


def test_list_field_joins_by_newline() -> None:
    """列表按行拼 —— 那是模型写得最多的形状，且拼接不引入歧义。"""
    orders = workorder.parse_workorders(
        {
            "workorders": [
                {
                    "action": "封禁",
                    "basis": ["auth.log:1423", "syslog:88"],
                    "risk": "r",
                    "permission": "p",
                }
            ]
        }
    )
    assert orders[0].basis == "auth.log:1423\nsyslog:88"


# ══════════════════════════════════════════════════════════
# 二、渲染：必须是否定句 + 不声称已执行
# ══════════════════════════════════════════════════════════


def _orders(*, grade: str = "confirmed", complete: bool = True) -> list:
    action = "封禁 203.0.113.45"
    basis = "auth.log:1423 暴力破解成功"
    risk = "若为办公出口会断网"
    permission = "防火墙运维"
    missing = [] if complete else ["risk"]
    return [
        workorder.WorkOrder(
            action=action,
            basis=basis,
            risk=risk if complete else "",
            permission=permission,
            grade=grade,
            missing=missing,
        )
    ]


def test_header_denies_execution() -> None:
    """★ 文件头第一句必须是否定句。

    读这份文件的人可能是两周后接手的人，也可能是审计。他看到一份
    "阻断 203.0.113.45"的清单，最坏的误读是"这些已经做了"。
    """
    md = workorder.render_workorders(_orders())
    assert "不是已执行的动作" in md
    # ⛔ 在纯文本里也醒目 —— 没有它，这条声明会被当成普通引言跳过。
    assert "⛔" in md


def test_render_never_claims_executed() -> None:
    """渲染结果里不许出现任何"已执行 / 已完成处置"的说法。

    ⚠️ 反例是"顺手写一句「已为您封禁」" —— 那会让整份工单的性质
    从建议变成记录，而系统根本没有那个能力。

    ## ★ 为什么要先剔掉文件头那一行

    文件头**必须**包含「不是已执行的动作」（见 `test_header_denies_execution`），
    所以「已执行」这个词在合法输出里必然出现一次。判据不是"这个词
    不出现"，而是"它只以否定形式出现"。

    直接把「已执行」列进黑名单会让这条测试**永远红**，于是下一个人
    第一反应是把断言删掉 —— 那才是真正的失守。所以这里剔除否定句
    再查：剩下的正文里任何一处正面宣称都算失败。
    """
    md = workorder.render_workorders(_orders())
    # 剔掉所有含否定词的行（文件头那一句），剩下的才是"正面宣称"区
    body = "\n".join(
        line
        for line in md.splitlines()
        if not any(neg in line for neg in ("不是", "没有", "不执行", "未执行", "不会"))
    )
    for lie in ("已为您", "已执行", "已封禁", "已隔离", "已阻断", "执行成功"):
        assert lie not in body, f"工单正文里出现了「{lie}」，与「只出建议不执行」矛盾"


def test_negation_is_the_only_mention_of_execution() -> None:
    """每一处「执行」都必须**要么是否定，要么明确归给人**。

    ## ★ 判据为什么不是"不许出现「执行」"

    工单里必须有一句「每一条都需要人工确认后再由对应权限的同事执行」
    —— 那是这份文件的核心说明，而它含「执行」二字。把词禁掉等于
    把该说的话也一起禁掉，下一个人只会把断言删了。

    真正要否证的是「**系统**执行了」。所以每处「执行」的上下文里必须有
    下列之一：
    - 否定词（不是 / 没有 / 不 / 未）；
    - 归给人的词（同事 / 人工 / 人 / 负责人 / 运维）。

    两者都没有 = 这句话的主语只能是系统，那就是在撒谎。
    """
    md = workorder.render_workorders(_orders())
    human = ("同事", "人工", "人", "负责人", "运维", "审批")
    neg = ("不是", "没有", "不", "未", "❌", "⚠️")
    # ★ 只查「执行」与「已 X」两类词，**不查「封禁 / 隔离 / 阻断」**
    #   本身 —— 那是动作描述里必须出现的动词（工单要告诉人"去封这个 IP"）。
    #   查它们会把整份文件判红，而红的原因是"文件在正常工作"。
    for keyword in ("执行", "已封禁", "已隔离", "已阻断", "已完成处置"):
        start = 0
        while (idx := md.find(keyword, start)) != -1:
            window = md[max(0, idx - 20) : idx + len(keyword) + 12]
            assert any(w in window for w in neg + human), (
                f"「{keyword}」出现在既无否定、也没归给人的上下文里：…{window}…"
                " —— 这种句子的主语只能是系统，而系统没有执行能力。"
            )
            start = idx + len(keyword)


def test_incomplete_order_flagged_in_render() -> None:
    """缺段的工单在渲染结果里**写明缺什么**，不只显示降级后的级别。

    只显示 `needs_follow_up` 而不说原因，读者会以为证据不足，
    而实际是"模型没写风险提示" —— 那是两回事。
    """
    md = workorder.render_workorders(_orders(complete=False))
    assert "本工单不完整" in md
    assert "风险提示" in md


def test_empty_orders_produces_explanation_not_empty_file() -> None:
    """★ 没有建议时**不产出空文件**，而是一份带解释的说明。

    与 `history.render_history_block` 同一条纪律：空文件与"没有建议"
    在读的人眼里是两件事 —— 前者像"生成失败了"。而且 `workorders: []`
    本身是个结论，值得写下来。
    """
    md = workorder.render_workorders([])
    assert md.strip(), "空列表不能渲染成空字符串"
    assert "本次没有需要处置的事项" in md
    # ★ 必须点出"可能是判断不了"这第二种解释 —— 否则读者会把
    #   "没建议"一律读成"没事"。
    assert "还判断不了" in md


def test_empty_orders_count_is_zero() -> None:
    """空结果说明**不算**一条工单。

    ⚠️ 这是 `count_orders` 数 `### ` 而不是 `## ` 的全部理由：
    数 `## ` 会把「本次没有需要处置的事项」也数进去，交出一份
    "1 条工单"而实际一条都没有 —— 且界面上看起来完全正常。
    """
    assert workorder.count_orders(workorder.render_workorders([])) == 0


def test_count_matches_rendered_orders() -> None:
    orders = _orders() * 3
    assert workorder.count_orders(workorder.render_workorders(orders)) == 3


def test_table_cells_escape_pipe() -> None:
    """表格单元格里的裸 `|` 会被拆成两列。

    与 `scans/writeup.py` 的 `_cell()` 是同一类问题 —— 工单里的
    `action` 常常是命令行片段，带 `|` 很常见（`grep x | awk y`）。
    """
    orders = [
        workorder.WorkOrder(
            action="执行 `ps aux | grep evil` 确认进程",
            basis="b",
            risk="r",
            permission="p",
        )
    ]
    md = workorder.render_workorders(orders)
    # 表格行里的裸 `|` 已被转义
    for line in md.splitlines():
        if line.startswith("| 动作描述 |"):
            assert "\\|" in line
            # 原始内容里的 `|` 不能原样出现两次以上
            assert line.count(" | ") <= 2


def test_renders_markdown_reusable_by_existing_export() -> None:
    """产出是**纯 markdown**，能被现有报告导出直接接住。

    会话 C 要求"复用现有报告导出，不新建格式"。判据就是：它是
    markdown、没有自定义容器语法、没有 HTML 包装。
    """
    md = workorder.render_workorders(_orders())
    assert md.startswith("# ")
    assert "<div" not in md and "<script" not in md
    # 至少有一张标准 GFM 表格（现有导出认这个）
    assert "| --- |" in md


def test_filename_is_markdown() -> None:
    assert workorder.filename_for_order("某次入侵").endswith(".md")
    assert workorder.filename_for_order("").startswith("IR-WorkOrders-")


@pytest.mark.parametrize(
    "title",
    ["a/b\\c:d*e?f", "  ", "【应急】Windows 域控被拿下", "../../etc/passwd"],
)
def test_filename_strips_unsafe_chars(title: str) -> None:
    """文件名清洗：不允许路径分隔符（它会被拼进下载响应头）。"""
    name = workorder.filename_for_order(title)
    assert "/" not in name and "\\" not in name
    assert not name.startswith(".")
    assert name.endswith(".md")


# ══════════════════════════════════════════════════════════
# 三、★★ 结构性约束：模块里不存在执行路径
# ══════════════════════════════════════════════════════════


#: 允许出现的 import 模块（顶层名）。**加任何一项都要先问"它能不能发起动作"**。
ALLOWED_IMPORTS = {"logging", "dataclasses", "pathlib", "typing", "app", "__future__"}

#: 允许出现的函数调用名（`Call.func` 的末段）。
#: ⚠️ 这不是穷举白名单 —— 它只列出**可能发起动作**的名字，见到就红。
FORBIDDEN_CALLS = {
    "system",
    "popen",
    "run",
    "call",
    "check_call",
    "check_output",
    "Popen",
    "spawn",
    "spawnl",
    "spawnv",
    "execv",
    "execve",
    "fork",
    "connect",
    "request",
    "urlopen",
    "HTTPConnection",
}

#: 允许出现的属性访问（`os.path.join` 里的 `path` 这种）。
#: 用来抓 `os.system` / `socket.connect` 这类"import 了但静态看不出用途"的。
FORBIDDEN_ROOTS = {"subprocess", "socket", "requests", "httpx", "urllib", "os.system", "shutil"}


def _module_imports(tree: ast.Module) -> set[str]:
    """收集所有 import 的**顶层模块名**。

    ⚠️ 只收模块名，不收被导入的符号（`from dataclasses import dataclass`
    只算 `dataclasses`）。理由：判据是"这个模块能干什么" —— 而能力由
    **包**决定，不由名字决定。把 `dataclass` 也算进去会让白名单变成
    一份需要天天维护的符号清单，然后大家开始往里加东西图省事，
    那份清单就失去意义了。
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            # 相对 import（`from . import x`）的 `module` 是 None —— 直接取
            # 会 AttributeError。合成一个条件而不是嵌套 if：两者是**同一个**
            # 判断（"这是一个能取到模块名的 import"），不是两层语义。
            names.add(node.module.split(".")[0])
    return names


def test_no_execution_path_in_module() -> None:
    """★★ 模块里**不存在**任何能发起外部动作的 import。

    这是会话 C「代码里找不到任何会直接调用外部执行接口的路径」那条
    验收的机器判据。用 AST 而不是 grep：grep 漏一次
    （`__import__("subprocess")`、`getattr(os, "sys"+"tem")`）就
    永远查不出来，而 AST 至少要求作者**显式**写出那些形状。

    ⚠️ 这条测试的价值不在它能证明"绝对安全"（它不能），而在
    **它会在有人加 import 的那一刻红**，把"要不要接执行通道"这个
    决定重新推到人面前。见 `workorder` 模块 docstring 的
    「为什么不留后门」。
    """
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    imports = _module_imports(tree)
    unexpected = imports - ALLOWED_IMPORTS
    assert not unexpected, (
        f"workorder 模块新增了 import：{sorted(unexpected)}。"
        "这个模块的全部职责是把处置建议变成文件 —— 任何能发起网络/进程"
        "动作的依赖都会让它变成执行通道。如果确实需要，请先改 "
        "ALLOWED_IMPORTS 并在 review 里说明理由。"
    )


def test_no_forbidden_calls() -> None:
    """AST 扫所有 `Call`，禁止 `system`/`Popen`/`connect` 这类名字。

    比 `test_no_execution_path_in_module` 更细一层：它抓的是
    "已允许的模块里有没有被调用的危险函数"（比如 `pathlib.Path.run`）。
    """
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = ""
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        if name in FORBIDDEN_CALLS:
            hits.append(f"{name} @ line {node.lineno}")
    assert not hits, f"workorder 模块里出现了可能的执行调用：{hits}"


def test_no_forbidden_module_use() -> None:
    """AST 扫属性访问的根名，禁止 `subprocess.*` / `socket.*` 等。"""
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            root = node
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name) and root.id in FORBIDDEN_ROOTS:
                hits.append(f"{root.id}.{node.attr} @ line {node.lineno}")
    assert not hits, f"workorder 模块里出现了被禁的模块使用：{hits}"


def test_module_has_no_executor_hook() -> None:
    """★★ 不存在"预留的执行接口" —— 参数、字段、公开函数都没有。

    会话 C 原文：「不留"将来自动执行"的后门逻辑」。最常见的三种留法：
    ① `render_workorders(..., executor=None)`；
    ② `WorkOrder.executed: bool = False`；
    ③ `def execute(...) -> None: raise NotImplementedError`。

    三种都会在半年后被人填上。这条测试逐个否证。
    """
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))

    # ① 任何函数参数里出现 executor/execute/handler/callback
    banned_params = {
        "executor",
        "execute",
        "handler",
        "callback",
        "on_execute",
        "dispatcher",
        "applier",
    }
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = {a.arg for a in node.args.args} | {a.arg for a in node.args.kwonlyargs}
            bad = args & banned_params
            assert not bad, f"{node.name}() 有疑似执行钩子的参数：{bad}"

    # ② `WorkOrder` 上不许有 executed/executed_at/status 这类字段
    banned_fields = {"executed", "executed_at", "execution", "applied", "applied_at"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "WorkOrder":
            fields = {
                s.target.id
                for s in node.body
                if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)
            }
            assert not (fields & banned_fields), (
                f"WorkOrder 上出现了执行状态字段：{fields & banned_fields}。"
                "工单是**待办**，它的执行状态属于工单系统，不属于本模块。"
            )

    # ③ 公开函数名里不许有 execute/apply/run 这类动词
    public = {
        n.name
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")
    }
    for verb in ("execute", "apply", "run", "perform", "dispatch"):
        bad = {name for name in public if verb in name.lower()}
        assert not bad, f"出现了疑似执行入口的公开函数：{bad}"


# ══════════════════════════════════════════════════════════
# 四、落盘（失败不抛、覆盖写、读不到返回 None）
# ══════════════════════════════════════════════════════════


def test_write_and_read_roundtrip(tmp_path, monkeypatch) -> None:
    """写进去能读出来，且路径在 `WORKORDER_DIR` 下。"""
    monkeypatch.setattr(workorder, "get_settings", _fake_settings(tmp_path))

    md = workorder.render_workorders(_orders())
    path = workorder.write_order_file("ctf2chal_x", md)
    assert path is not None
    assert path.name == "ctf2chal_x.md"
    assert workorder.WORKORDER_DIR in str(path)
    assert workorder.read_order_file("ctf2chal_x") == md


def test_write_overwrites_previous(tmp_path, monkeypatch) -> None:
    """★ 一题一份，重写覆盖（不是追加）。

    重跑一次复盘产出一份新工单；旧的留着只会让"批的是哪一版"
    变成新问题。
    """
    monkeypatch.setattr(workorder, "get_settings", _fake_settings(tmp_path))

    workorder.write_order_file("c1", "# 第一版")
    workorder.write_order_file("c1", "# 第二版")
    assert workorder.read_order_file("c1") == "# 第二版"


def test_read_missing_returns_none(tmp_path, monkeypatch) -> None:
    """★ 没有文件返回 `None`，**不是空串**。

    空串会让"没产出过工单"与"产出了一份空工单"变成同一件事，而
    前者该是 409、后者该是正常返回。
    """
    monkeypatch.setattr(workorder, "get_settings", _fake_settings(tmp_path))
    assert workorder.read_order_file("never_ran") is None


def test_write_empty_markdown_is_noop(tmp_path, monkeypatch) -> None:
    """空内容不落盘 —— 空文件看起来像"生成失败了"。"""
    monkeypatch.setattr(workorder, "get_settings", _fake_settings(tmp_path))
    assert workorder.write_order_file("c1", "   \n\n  ") is None
    assert workorder.read_order_file("c1") is None


def test_write_failure_does_not_raise(tmp_path, monkeypatch) -> None:
    """落盘失败只记日志返回 `None`，不抛。

    理由与 `service.save_writeup` 一致：工单是解题结果的**附带产物**，
    它落盘失败不该毁掉一次已经跑完的复盘。
    """
    monkeypatch.setattr(workorder, "get_settings", _fake_settings(tmp_path))

    def _boom(self, *_args, **_kwargs):  # noqa: ANN001, ANN202
        raise OSError("磁盘满了")

    monkeypatch.setattr("pathlib.Path.write_text", _boom)
    assert workorder.write_order_file("c1", "# 工单") is None


def _fake_settings(tmp_path):  # noqa: ANN001, ANN202
    """把 `scan_root` 指到 tmp_path 的最小替身。"""

    def _get_settings():  # noqa: ANN202
        return type("S", (), {"scan_root": tmp_path})()

    return _get_settings
