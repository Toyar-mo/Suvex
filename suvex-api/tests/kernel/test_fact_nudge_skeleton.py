"""v0.84：`fact_debt_nudge()` 的**拼接顺序**守卫。

## 这条守的是哪个真 bug

v0.84 把 `record_fact` 的参数骨架内联进提醒时，一开始的写法是：

    FACT_DEBT_NUDGE = (
        "⚠️ **你已经 {n} 轮...**".format(...)      # 或者写完再统一 format
        + "...ACTION: " + _RECORD_FACT_SKELETON + ...
    )

然后调用点 `.format(n=6)` 直接崩：

    KeyError: '"tool"'

因为 `_RECORD_FACT_SKELETON` 里是**真**花括号（它不参与格式化），
`.format()` 把 `{"tool"` 当成一个名为 `"tool"` 的替换字段。

**为什么这不是小 bug**：`render_live_state` 在 `fact_debt >= 6` 时会走
这一行，所以任何跑满六轮不记账的扫描都会崩 —— 而那正是这条提醒
设计来服务的场景。也就是说：**它只在最该生效的时候崩**。

修法是把提醒拆三段，只对含 `{n}` 的那段 format，骨架原样接上。
这组测试锁住这个形状。
"""

from __future__ import annotations

import json

import pytest

from app.kernel import multiagent
from app.kernel.multiagent import FACT_DEBT_WINDOW, fact_debt_nudge
from app.kernel.runner import parse_actions


def test_nudge_does_not_raise_format_keyerror() -> None:
    """★ 曾经的崩点：拼接后整条 `.format()` 会 KeyError。

    这条直接钉住"用户报的那个异常不会再出现"。
    """
    # 若实现退回"整条 format"，这一行会抛 KeyError: '"tool"'
    text = fact_debt_nudge(6)
    assert isinstance(text, str)
    assert len(text) > 100


@pytest.mark.parametrize("n", [FACT_DEBT_WINDOW, FACT_DEBT_WINDOW + 1, 99])
def test_nudge_interpolates_the_number(n: int) -> None:
    """`{n}` 真的被替换掉了 —— 不是留在文本里当字面量。

    反面：如果实现改成"不 format、直接输出含 `{n}` 的原文"，
    模型会看到「你已经 {n} 轮没有...」，提醒的可信度直接塌掉
    （一个连数字都填不对的提醒，凭什么让人照做）。
    """
    text = fact_debt_nudge(n)
    assert f"{n} 轮" in text
    assert "{n}" not in text


def test_nudge_keeps_braces_intact() -> None:
    """骨架里的花括号**原样保留**，没有被 format 吃掉或转义坏。

    这条与上一条是一对：`{n}` 要被替换，而 `{"tool": ...}` 不能被碰。
    只有"先 format 前半段、骨架原样拼"这一种写法能同时满足。
    """
    text = fact_debt_nudge(6)
    assert '{"tool": "record_fact", "args": {' in text
    assert "{{" not in text, "出现了被转义的双花括号 —— 模型会照抄出错"


def test_skeleton_survives_parse_actions() -> None:
    """★ 模型**照抄**这段骨架时，`parse_actions` 必须能解析出参数。

    这是整个 v0.84 #1 的目的：让骨架可执行。如果骨架缺 `"args"` 包装，
    `parse_actions` 会回落成 `args={}` —— 也就是**又一个空参数**，
    等于什么都没改（v0.83 那 3 次 `{}` 就是这么来的）。
    """
    text = fact_debt_nudge(6)

    # 取出 ``` 围栏里的那段
    fence = text.split("```")
    assert len(fence) >= 3, "提醒里应该有代码块"
    block = fence[1]

    parsed = parse_actions(block)
    assert len(parsed) == 1, f"应该解析出 1 个动作，实际 {len(parsed)}：{parsed}"
    tool, args = parsed[0]
    assert tool == "record_fact"
    assert isinstance(args, dict)
    assert args, "args 是空的 —— 骨架缺了 `\"args\"` 包装"
    assert set(args) == {"fact_key", "summary", "confidence", "evidence"}


def test_skeleton_is_valid_json() -> None:
    """骨架本身是合法 JSON（模型会原样复制，多余逗号/引号都会让它失配）。"""
    obj = json.loads(multiagent._RECORD_FACT_SKELETON)
    assert obj["tool"] == "record_fact"
    assert obj["args"]["fact_key"] == "<category>/<slug>"


def test_module_constant_is_a_filled_example() -> None:
    """`FACT_DEBT_NUDGE` 这个兼容常量是**0 填充的示例**，不是带 `{n}` 的模板。

    ⚠️ 这条是在防"有人为了兼容又把模板改回去" —— 一旦它是模板，
    就必然又有人写 `.format()`，然后回到那个 KeyError。

    它保留的名字只服务于测试与文档引用（验文案内容，与数字无关）。
    """
    assert "{n}" not in multiagent.FACT_DEBT_NUDGE
    assert "0 轮" in multiagent.FACT_DEBT_NUDGE
