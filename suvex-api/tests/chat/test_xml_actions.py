"""自创 XML 协议（`<action>` / `<analysis>`）归一化与泄漏防护（v1.41）。

## 这个文件为什么存在

线上实测（2026-11-01，会话 `chat_01M690GSFE4SSP0DB5BHR0BNDK`）：
模型 deepseek-v4.1-flash 在长链解题里**没走 `ACTION:` 文本协议**，而是自创了
XML 风格：

    <analysis> I need to just run one command. </analysis>
    <action> {"tool": "run_shell", "args": {"command": "..."}} </action>

后果有三层，全部静默：

1. **不执行**：`parse_actions` 只认 `ACTION:`，XML `<action>` 解析不出来 →
   动作从未执行（该会话 576 个 `<action>` 标签，只有 54 条工具结果）。
2. **不隐藏**：`_narration_before_action` 也只切 `ACTION:`/`FINAL:` →
   整段 XML 原样落库进 process 面消息，用户看到裸 `<action>` + JSON。
3. **烧纠偏**：模型以为做了事、系统判它"只说话没行动"，反复纠偏 →
   该轮 `nudges_used=95`，最后仍停在计划句（用户截图「停在下一步的计划上」）。

修法：在对话侧把 XML 改写成文本协议（`ACTION: {...}`）、删掉 `<analysis>`，
让后面所有"解析 / 剥离 / 推送"逻辑原样复用。

## 这里钉住的三件事

- `_normalize_xml_actions`：`<action>` → `ACTION:`，`<analysis>` → 删；
- 归一化后的文本能被 `parse_actions` 真正解析出动作（**执行得起来**）；
- 归一化后的文本经 `_narration_before_action` 剥离后不含任何 XML/JSON（**不泄漏**）。
"""

from __future__ import annotations

from app.chat.engine import (
    _has_protocol_marker_or_xml,
    _narration_before_action,
    _normalize_xml_actions,
)
from app.kernel.runner import parse_actions

SAMPLE = (
    "脚本没有输出，可能是解码时出错了。我捕获 stderr，再运行一次。\n"
    '<action> {"tool": "run_shell", "args": {"command": "python3 /work/suc.py"}} </action>\n'
    "运行并查看完整错误。\n"
    "<analysis> Wait for the result. </analysis>\n"
    '<action> {"tool": "run_shell", "args": {"command": "echo EXIT=$?"}} </action>\n'
)


def test_normalize_rewrites_single_xml_action_to_text_protocol() -> None:
    """单个 `<action>{...}</action>` → 一行 `ACTION: {...}`。"""
    raw = '<action> {"tool": "run_shell", "args": {"command": "ls"}} </action>'
    out = _normalize_xml_actions(raw)
    assert "ACTION:" in out
    assert "<action>" not in out
    # parse_actions 现在能认（这才是关键：动作能被执行）。
    assert parse_actions(out) == [("run_shell", {"command": "ls"})]


def test_normalize_drops_analysis_blocks() -> None:
    """`<analysis>...</analysis>` 整块删除（推理不进正文、不执行）。"""
    raw = "看这里。\n<analysis> 内心推理，包含 { 花括号 } 也不该影响 </analysis>\n继续。"
    out = _normalize_xml_actions(raw)
    assert "<analysis>" not in out
    assert "内心推理" not in out
    assert "看这里。" in out and "继续。" in out


def test_normalize_multi_action_all_parsed() -> None:
    """多个 XML 动作全部被改写成可解析的 `ACTION:` 行。"""
    out = _normalize_xml_actions(SAMPLE)
    assert "<action>" not in out
    assert "<analysis>" not in out
    assert parse_actions(out) == [
        ("run_shell", {"command": "python3 /work/suc.py"}),
        ("run_shell", {"command": "echo EXIT=$?"}),
    ]


def test_narration_after_normalize_has_no_protocol_leak() -> None:
    """★ 归一化后再剥离，正文里一个 XML / JSON 都不能剩。"""
    out = _normalize_xml_actions(SAMPLE)
    narration = _narration_before_action(out)
    assert "<action>" not in narration
    assert "<analysis>" not in narration
    assert "run_shell" not in narration
    assert '"tool"' not in narration
    # 人话必须留下（不能因为修泄漏把正文也删了）。
    assert "脚本没有输出" in narration


def test_normalize_is_noop_without_xml() -> None:
    """普通回复（无 `<action>`）逐字节不变 —— 不能误伤正常内容。"""
    raw = '好的，最近的扫描一共 3 条。\nACTION: {"tool": "list_scans", "args": {}}'
    assert _normalize_xml_actions(raw) == raw


def test_has_protocol_marker_or_xml_detects_xml() -> None:
    """`_with_native_actions` 的判据：正文已有 XML 动作时不再合成 `ACTION:`。"""
    assert _has_protocol_marker_or_xml('<action> {"tool": "x"} </action>') is True
    assert _has_protocol_marker_or_xml('ACTION: {"tool": "x"}') is True
    assert _has_protocol_marker_or_xml("只是一句普通的话。") is False
