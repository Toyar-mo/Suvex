"""提示词里的 `record_fact` 示例必须**真的能通过后端校验**。

## 为什么值得一条常驻测试

模型对示例的信任度高于对规则的信任度 —— 它会照抄示例。所以示例如果
过不了 `validate_fact`，模型拿到的是一条报错，它会认为"工具坏了"，
然后**不再尝试**（v0.80 那 42 次工具调用里 record_fact 为 0，有一部分
原因就是它压根没拿到可照抄的形状）。

这个测试把提示词里每段 ``` 围栏中的 `ACTION: {"tool": "record_fact", ...}`
抠出来，按 `app.facts.models` 的真实常量校验：

- `fact_key` 符合 `^[a-z0-9-]+/[a-z0-9-]+$`，前缀是六类之一
- `summary` 非空且 ≤300 字
- `confidence` 是三态之一
- `args` 非空（缺 `args` 包装 = 模型拿到空参数，v0.83 的 3 次 `{}` 就是这个）

骨架里的占位符（`<category>/<slug>`）跳过，不算示例。
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from app.facts.models import FACT_CATEGORIES, FACT_CONFIDENCES

PROMPT = pathlib.Path(__file__).resolve().parents[2] / "prompts" / "kernel_pentest.md"
KEY_RE = re.compile(r"^[a-z0-9-]+/[a-z0-9-]+$")
FENCE_RE = re.compile(r"```[a-z]*\n(.*?)```", re.S)


def _examples(text: str) -> list[tuple[int, dict]]:
    """抠出所有围栏代码块里的 record_fact ACTION，带行号。"""
    out: list[tuple[int, dict]] = []
    for m in FENCE_RE.finditer(text):
        block = m.group(1)
        if "ACTION:" not in block:
            continue
        line_no = text[: m.start()].count("\n") + 1
        body = block.split("ACTION:", 1)[1].strip()
        obj = json.loads(body)  # 解析失败就该炸 —— 模型照抄会失配
        if obj.get("tool") == "record_fact":
            out.append((line_no, obj))
    return out


@pytest.fixture(scope="module")
def text() -> str:
    return PROMPT.read_text(encoding="utf-8")


def test_examples_exist(text):
    """至少要有真实示例（不是只有占位骨架）。"""
    real = [
        (n, o)
        for n, o in _examples(text)
        if not str(o["args"].get("fact_key", "")).startswith("<")
    ]
    assert len(real) >= 3, (
        f"只有 {len(real)} 个真实示例 —— 模型需要不止一个类别可抄，"
        "否则它会以为所有事实都长一个样"
    )


def test_every_example_has_args_wrapper(text):
    """★ 缺 `"args"` 包装时 `parse_actions` 会回落成 `{}`。"""
    for line_no, obj in _examples(text):
        args = obj.get("args")
        assert isinstance(args, dict) and args, (
            f"第 {line_no} 行的示例缺 args 包装或 args 为空"
        )


def test_every_example_key_is_valid(text):
    for line_no, obj in _examples(text):
        key = str(obj["args"].get("fact_key", ""))
        if key.startswith("<"):
            continue  # 骨架占位符
        assert KEY_RE.match(key), (
            f"第 {line_no} 行：fact_key {key!r} 不符合 ^[a-z0-9-]+/[a-z0-9-]+$"
        )
        cat = key.split("/", 1)[0]
        assert cat in FACT_CATEGORIES, (
            f"第 {line_no} 行：category {cat!r} 不在 {FACT_CATEGORIES}"
        )


def test_every_example_summary_fits(text):
    for line_no, obj in _examples(text):
        summary = obj["args"].get("summary", "")
        assert summary.strip(), f"第 {line_no} 行：summary 是空的"
        assert len(summary) <= 300, (
            f"第 {line_no} 行：summary {len(summary)} 字，超过后端 300 上限 —— "
            "模型照抄会被拒"
        )


def test_every_example_confidence_is_valid(text):
    for line_no, obj in _examples(text):
        conf = obj["args"].get("confidence")
        if conf is None:
            continue  # 可选，默认 tentative
        assert conf in FACT_CONFIDENCES, (
            f"第 {line_no} 行：confidence {conf!r} 不在 {FACT_CONFIDENCES}"
        )


def test_examples_cover_more_than_one_category(text):
    """单一类别的示例会让模型把一切塞进同一个前缀。"""
    cats = {
        str(o["args"]["fact_key"]).split("/", 1)[0]
        for _, o in _examples(text)
        if not str(o["args"].get("fact_key", "")).startswith("<")
    }
    assert len(cats) >= 2, f"示例只覆盖了 {cats} 一类"


# ── v0.84 #4：`record_coverage` 也要有可照抄的示例 ────────────────
#
# 起因是实测数据：v0.84 那轮扫描 `list_coverage` 被调 2 次，
# `record_coverage` **0 次** —— 模型会去**读**覆盖表，却从不往里写。
# 追下来提示词里 `record_coverage` 只出现 1 次，还是挂在
# 「已排除要写下来」这一条的从句里，既没有参数形状也没讲什么时候用。
#
# 「工具在、提示词提到过、模型不用」是这个项目已经踩过三次的坑
# （`add_fact` 0 次、`record_coverage` 2→0 次、空参数 `{}` 3 次）。
# 三次的共同解法都是同一个：**给一段能直接照抄的ACTION**。


def _coverage_examples(text: str) -> list[tuple[int, dict]]:
    out: list[tuple[int, dict]] = []
    for m in FENCE_RE.finditer(text):
        block = m.group(1)
        if "ACTION:" not in block:
            continue
        line_no = text[: m.start()].count("\n") + 1
        obj = json.loads(block.split("ACTION:", 1)[1].strip())
        if obj.get("tool") == "record_coverage":
            out.append((line_no, obj))
    return out


def test_record_coverage_has_examples(text):
    """`record_coverage` 必须有可照抄的 ACTION 示例。

    没有示例时，模型即使想记也不知道参数长什么样，只能 `{}`——
    和 v0.83 那三次空 `record_fact` 是同一个失败模式。
    """
    assert len(_coverage_examples(text)) >= 2, (
        "提示词里 `record_coverage` 的 ACTION 示例不足 —— "
        "模型会转而去调 `list_coverage`（只读），覆盖表永远长不大"
    )


def test_record_coverage_examples_have_surface(text):
    """`surface` 是唯一必填字段，示例里不能漏。"""
    for line_no, obj in _coverage_examples(text):
        args = obj.get("args")
        assert isinstance(args, dict) and args, (
            f"第 {line_no} 行：`record_coverage` 示例缺 args 包装"
        )
        surface = args.get("surface")
        assert isinstance(surface, str) and surface.strip(), (
            f"第 {line_no} 行：`record_coverage` 示例缺 `surface`（唯一必填字段）"
        )


def test_record_coverage_examples_show_both_outcomes(text):
    """示例要同时给出「成立」和「已排除」两种结果。

    只给一种，模型会以为覆盖表是给"发现"用的；
    而 v0.84 要纠正的正是"干净的结果也得记"。
    """
    joined = ""
    for _, obj in _coverage_examples(text):
        joined += str(obj["args"].get("outcome", ""))
    assert "成立" in joined, "示例里没有「成立」的结果"
    assert "已排除" in joined, "示例里没有「已排除」的结果 —— 干净的结果也必须记"


def test_explains_why_reading_is_not_enough(text):
    """必须明说「只看不写，表永远是空的」。

    这是 v0.84 实测那轮的**主要失败模式**（list 2 次 / record 0 次），
    不点破的话模型会一直以为"查一下就知道有没有人测过"。
    """
    assert "list_coverage" in text
    assert "读一百次" in text or "先写" in text, (
        "缺少「只读不写没用」的说明"
    )

