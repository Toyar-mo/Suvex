"""上下文预算（v1.26）：轮次上限、历史条数、历史总字符。

用户原话：「轮次预算太少了吧，遇到难点的题就解不出了」。

这里守三件事：
1. 默认预算足够跑一道难题（而不是 6 轮就收尾）。
2. 历史条数放宽之后，**总字符**仍有一道闸（否则一次调用就撑爆上下文）。
3. 超预算时的丢弃顺序：先丢最老的工具结果，永远保留最后一条和第一条 user。
"""

from __future__ import annotations

from types import SimpleNamespace as _NS

from app.chat import engine


def test_default_budgets_are_sized_for_hard_tasks() -> None:
    """默认值必须能撑住一道难题（否则就是"必然半路停"）。"""
    # 用户明确要求与 CyberStrikeAI 对齐（其 `max_iterations: 800`）。
    assert engine.MAX_TOOL_ROUNDS >= 800
    # ★ 纠偏额度不是轮次预算：300 会让"只说不做"的模型白烧 300 次
    #   （2026-11-09 线上会话正是 nudges=122 / 231 轮）。8 次足够跨过偶发抽风。
    assert 8 <= engine.MAX_TOOL_NUDGES <= 20
    assert engine.HISTORY_MESSAGE_LIMIT >= 200
    assert engine.MAX_HISTORY_CHARS >= 100_000


def _messages(sizes: list[int]) -> list[dict[str, str]]:
    return [{"role": "user", "content": "x" * n} for n in sizes]


def test_under_budget_is_untouched() -> None:
    out = _messages([10, 10, 10])
    assert engine._trim_history_to_budget(out) == out


def test_over_budget_drops_oldest_tool_results_first(monkeypatch) -> None:
    """先丢最老的工具结果 —— 它们最占地方，且价值随时间递减。"""
    monkeypatch.setattr(engine, "MAX_HISTORY_CHARS", 100)
    history = [
        {"role": "user", "content": "第一次提问" + "x" * 10},
        {"role": "assistant", "content": "好的，先跑一下。"},
        {"role": "user", "content": "[工具结果] `run_shell`\n" + "y" * 200},
        {"role": "assistant", "content": "拿到结果了。"},
        {"role": "user", "content": "[工具结果] `run_shell`\n" + "z" * 200},
    ]
    trimmed = engine._trim_history_to_budget(history)

    joined = "\n".join(m["content"] for m in trimmed)
    assert "y" * 200 not in joined, "最老的工具结果应该先被丢掉"
    # 结论（最后一条）必须保留 —— 模型要回的就是它
    assert trimmed[-1]["content"].startswith("[工具结果]")


def test_never_drops_the_first_user_message_or_the_last(monkeypatch) -> None:
    """用户的问题与最后一条永远保留；省略要有说明，不能让模型以为漏看。"""
    monkeypatch.setattr(engine, "MAX_HISTORY_CHARS", 50)
    history = [
        {"role": "user", "content": "我的问题：" + "q" * 20},
        {"role": "assistant", "content": "中间一大段。" + "a" * 200},
        {"role": "assistant", "content": "最后一条结论。"},
    ]
    trimmed = engine._trim_history_to_budget(history)

    assert trimmed[-1]["content"] == "最后一条结论。"
    assert "我的问题：" in trimmed[0]["content"], "用户的问题不能被丢"
    assert "上下文已省略" in trimmed[0]["content"], "省略必须有显式说明"


def test_render_history_actually_feeds_the_budget(monkeypatch) -> None:
    """★ 接线测试：`_render_history` 必须**真的**走到预算裁剪。

    ## 为什么必须单独钉

    这一项目的反复形态：纯函数写好了、单测也绿，但没人调用
    （本项目真实发生过的「配了却从未生效」）。上面那三条测的是
    `_trim_history_to_budget` 本身；这一条测的是它**被调用了**。

    变异：把 `_render_history` 结尾的 `return _trim_history_to_budget(out)`
    改回 `return out` → 这里红。
    """
    class _Row:
        def __init__(self, role, content, tool_name=None, tool_failed=False,
                     tool_args=None, model_extra=None):
            self.role = role
            self.content = content
            self.tool_name = tool_name
            self.tool_failed = tool_failed
            self.tool_args = tool_args
            self.model_extra = model_extra

    monkeypatch.setattr(engine, "MAX_HISTORY_CHARS", 120)
    rows = [
        _Row("user", "问题" + "q" * 10),
        _Row("tool", "t" * 300, tool_name="run_shell"),
        _Row("assistant", "结论"),
    ]
    rendered = engine._render_history(rows)
    joined = "\n".join(m["content"] for m in rendered)

    assert "t" * 300 not in joined, "超预算的工具结果没有被裁掉 —— 接线断了"
    assert any("上下文已省略" in m["content"] for m in rendered)


# ---------------------------------------------------------------------------
# v1.53：历史滑窗切开后，首条可能是 assistant / tool —— 必须以 user 开头。
#
# `list_messages(limit=N)` 是 `order_by(seq.desc()).limit(N)` 再 reverse，
# 会话超过 N 条时窗口从中间切开，第一条可能是 assistant（残句）或 tool。
# Anthropic 风格 Messages API **要求首条是 user**（否则 400），而平台明说
# "国内所有大模型均可用" —— 一换 provider 就炸；即便 provider 宽容，模型
# 也会"凭空接话"（看不到用户的问题）。
# ---------------------------------------------------------------------------


def _row(role: str, content: str, *, tool_name=None, tool_failed=False,
         tool_args=None, model_extra=None):
    return _NS(role=role, content=content, tool_name=tool_name,
               tool_failed=tool_failed, tool_args=tool_args, model_extra=model_extra)


def test_history_starts_with_user_when_window_opens_on_assistant() -> None:
    """窗口从 assistant 开始（滑窗切开）→ 必须丢掉开头的非 user 残句，
    保证送模型的 messages 以 user 开头。"""
    rows = [
        _row("assistant", "上一轮的半句话"),
        _row("user", "[工具结果] run_shell\n结果"),
        _row("assistant", "结论"),
        _row("user", "新问题"),
    ]
    msgs = engine._render_history(rows)
    assert msgs, "不能渲染成空"
    assert msgs[0]["role"] == "user", "首条必须是 user（Anthropic 硬要求）"


def test_history_starts_with_user_when_window_opens_on_tool() -> None:
    rows = [_row("tool", "裸工具输出", tool_name="run_shell"), _row("user", "新问题")]
    msgs = engine._render_history(rows)
    assert msgs[0]["role"] == "user"


def test_history_that_already_starts_with_user_is_unchanged() -> None:
    rows = [_row("user", "问题"), _row("assistant", "答"), _row("user", "追问")]
    msgs = engine._render_history(rows)
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[0]["content"] == "问题", "已经以 user 开头时不能动它"
