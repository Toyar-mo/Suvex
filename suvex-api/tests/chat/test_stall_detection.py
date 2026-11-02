"""「原地打转」检测（v1.49）。

## 这个文件为什么存在

线上实测（2026-11-02，会话 `chat_01M6BWQ69MZJNBAB7C7QMVXPV2` 的
`turn_01M6E99VZ6G3CYW5XY5GAX80WZ`）：模型在同一个死胡同里连跑几十轮 ——
`round_index` 涨到 75、`tools_used` 到 101，可**没有一条新信息进账**：
它反复写「我意识到一个从未做过的测试…现在真正运行」，跑的却是同一条命令、
拿到一模一样的结果。用户看到的就是「一直在循环 / 问一句停一次」。

修法（`engine.run_turn` 里）：给每个执行过的动作算一个**稳定签名**
（工具 + 参数完全相同），连续 `MAX_REPEATED_ACTIONS`（默认 5）次相同就
判定「原地打转」，喂回一句 `render_stall_reminder` 让它换思路/交结论。
只认「完全相同」这个最硬的信号 —— 不掺长度/语义启发式（技术性长文里
误判率太高，见 `nudge.looks_like_plan` 那些坑）。

## 这里钉住的三件事

- `_action_signature`：同工具同参数（字典顺序无关）→ 同签名；命令改一个
  字节（换路径/换参数）→ 不同签名（那是"尝试了不同策略"，不算打转）。
- 阈值语义：连续 5 次才触发、4 次不触发、穿插不同命令不触发。
- `render_stall_reminder` 文案：把工具名与次数带进去（给模型量感）。
"""

from __future__ import annotations

from app.chat.engine import MAX_REPEATED_ACTIONS, _action_signature
from app.chat.prompts import render_stall_reminder


def _same_streak(sigs: list[str]) -> bool:
    """复刻引擎里的判定条件，方便表驱动测试（不依赖跑整轮模型）。"""
    window = sigs[-MAX_REPEATED_ACTIONS:]
    return len(window) >= MAX_REPEATED_ACTIONS and len(set(window)) == 1


def test_max_repeated_actions_default_is_five() -> None:
    """默认阈值 5 —— 容许偶发"重跑一次对比"，但连跑 5 次还期待新结果就是死路。"""
    assert MAX_REPEATED_ACTIONS == 5


def test_signature_is_stable_across_dict_key_order() -> None:
    """参数字典顺序不同但内容相同 → 同一个签名（否则模型换个顺序写就绕过）。"""
    a = _action_signature("run_shell", {"command": "ls -la", "cwd": "/work"})
    b = _action_signature("run_shell", {"cwd": "/work", "command": "ls -la"})
    assert a == b


def test_signature_differs_when_command_text_differs() -> None:
    """命令文本改一个字节就是不同动作（尝试了不同策略，不算打转）。"""
    a = _action_signature("run_shell", {"command": "cat /work/a.txt"})
    b = _action_signature("run_shell", {"command": "cat /work/b.txt"})
    assert a != b


def test_signature_differs_when_tool_differs() -> None:
    a = _action_signature("run_shell", {"command": "ls"})
    b = _action_signature("browser", {"command": "ls"})
    assert a != b


def test_signature_handles_non_json_args_without_raising() -> None:
    """怪参数（不可 JSON 化）也不能让检测崩 —— 退化成 repr 签名。"""
    sig = _action_signature("run_shell", {"x": object()})
    assert isinstance(sig, str) and sig.startswith("run_shell\x00")


def test_five_identical_actions_trigger() -> None:
    sigs = [_action_signature("run_shell", {"command": "python3 x.py"})] * 5
    assert _same_streak(sigs) is True


def test_four_identical_actions_do_not_trigger() -> None:
    sigs = [_action_signature("run_shell", {"command": "python3 x.py"})] * 4
    assert _same_streak(sigs) is False


def test_interleaved_different_actions_do_not_trigger() -> None:
    """中间穿插了别的命令 = 在试不同策略，不该判打转。"""
    s = _action_signature("run_shell", {"command": "python3 x.py"})
    other = _action_signature("run_shell", {"command": "python3 y.py"})
    sigs = [s, s, other, s, s]
    assert _same_streak(sigs) is False


def test_stall_reminder_names_tool_and_count() -> None:
    text = render_stall_reminder("run_shell", 5)
    assert "run_shell" in text
    assert "5" in text
    # 必须给出"怎么办"，不能只抱怨。
    assert "换个" in text or "换一个完全不同的思路" in text
