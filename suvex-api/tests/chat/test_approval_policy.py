"""人工审批的工具集合可配置（v1.89）。

## 为什么这条要测

内置判据（`ChatToolSpec.consequential`）回答的是「改不改平台状态」，
所以 `run_shell` / `browser` 被**故意**排除。而"哪些动作要人点头"
是部署环境的事 —— 这条配置就是那个出口。

它有两个容易坏的形态，各钉一条：

1. **配了却没生效**：审批闸与提示词清单读的不是同一个集合。
   两边都走 `approval_gated_names()` 才不会分叉。
2. **拼错名字静默失效**：`run_shel` 拼错时应当忽略并记 warning，
   而不是让整条策略无声无息地不生效。
"""

from __future__ import annotations

from types import SimpleNamespace

from app.chat import engine, prompts
from app.core import config


def _configure(monkeypatch, value: str) -> None:
    """把配置替换成一个只带这个字段的替身。

    不摸环境变量与 `get_settings` 的 lru_cache —— 缓存清早了/清晚了都会
    让别的用例读到上一份配置，那种失败极难定位。
    """
    stub = SimpleNamespace(chat_approval_extra_tools=value)
    monkeypatch.setattr(config, "get_settings", lambda: stub)


def test_default_gate_still_excludes_shell_tools() -> None:
    """默认判据不变：执行类工具不进审批闸（边界是容器与出站守卫）。"""
    gated = prompts.approval_gated_names()

    assert "create_scan" in gated
    assert "run_shell" not in gated
    assert "browser" not in gated


def test_configured_tools_join_the_gate(monkeypatch) -> None:
    _configure(monkeypatch, "run_shell,browser")

    assert {"run_shell", "browser"} <= prompts.approval_gated_names()
    assert engine._needs_approval("run_shell") is True  # noqa: SLF001
    assert engine._needs_approval("browser") is True  # noqa: SLF001


def test_prompt_lists_the_configured_tools(monkeypatch) -> None:
    """提示词里那份清单必须与审批闸同一来源 —— 否则模型会以为自己能直接跑。"""
    _configure(monkeypatch, "run_shell")
    rule = prompts.render_approval_rule(True)

    assert "`run_shell`" in rule
    assert "`create_scan`" in rule


def test_unknown_tool_names_are_ignored(monkeypatch) -> None:
    """拼错的名字不会变成一条永远不触发的规则，也不会让别的规则失效。"""
    _configure(monkeypatch, "run_shel, browser ")

    gated = prompts.approval_gated_names()
    assert "browser" in gated
    assert "run_shel" not in gated
    assert "create_scan" in gated
