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


# ══════════════════════════════════════════════════════════════════════
# v1.44：模型伪造 `<result>...</result>`（工具结果只能由平台返回）
# ══════════════════════════════════════════════════════════════════════
#
# 线上实测（2026-11-01 会话 `chat_01M6BWQ69MZJNBAB7C7QMVXPV2`，单轮 40 回合 /
# 77 工具 / 37 次纠偏）：模型在正文里写「执行。<result>ok</result>现在真正调用。
# <result>ok</result>…」——它把自己"假装成工具和结果"，于是以为已经执行过，
# 不断空转。这些 `<result>` 既泄漏给用户看，又会随历史回灌把模型带进循环。


def test_normalize_drops_fabricated_result_blocks() -> None:
    """`<result>ok</result>` 整块删掉（工具结果只能由平台返回）。"""
    raw = "现在真正调用。\n<result>ok</result>\n继续执行。"
    out = _normalize_xml_actions(raw)
    assert "<result>" not in out
    assert "</result>" not in out
    assert "ok" not in out  # 伪造的结果内容也一起没了
    assert "现在真正调用。" in out and "继续执行。" in out


def test_normalize_drops_many_fake_results_keeps_human_words() -> None:
    """一整段 `执行。/<result>ok</result>` 反复，删干净伪造结果、保留人话。"""
    raw = (
        "抱歉，前面那串空转是我的问题。现在直接执行。\n"
        "<result>ok</result>\n现在真正调用。\n"
        "<result>ok</result>\n"
        "我需要真正写出 ACTION 行，先 dump 字节：\n<result>ok</result>\n好，执行：\n"
        "<result>ok</result>"
    )
    out = _normalize_xml_actions(raw)
    assert "<result>" not in out and "ok" not in out
    assert "现在直接执行。" in out
    assert "先 dump 字节" in out


def test_normalize_result_with_leftover_stray_tag() -> None:
    """未闭合的半截 `<result>` 也被兜底清掉（增量截断时的残留）。"""
    raw = "人话一句。<result>\n后面还有内容"
    out = _normalize_xml_actions(raw)
    assert "<result>" not in out
    assert "人话一句。" in out


def test_has_protocol_marker_or_xml_detects_result() -> None:
    """含 `<result>` 时也算"已有 XML"，不该再合成原生动作。"""
    assert _has_protocol_marker_or_xml("执行。<result>ok</result>") is True


def test_real_tool_result_still_kept_as_narration_prefix() -> None:
    """★ 不能误伤：真实 `<result>` 外的正文必须保留（删块不删话）。"""
    raw = "我先跑一下 ls。\nACTION: {\"tool\": \"run_shell\", \"args\": {}}\n等结果。"
    assert "<result>" not in raw
    assert _normalize_xml_actions(raw) == raw



# ══════════════════════════════════════════════════════════════════════
# v1.48：Anthropic 风格 <invoke>/<parameter> 工具调用
#
# 线上实测（2026-11-02，同一会话 chat_01M6BWQ69MZJNBAB7C7QMVXPV2 的下一轮
# turn_01M6E99VZ6G3CYW5XY5GAX80WZ，round=101、tools_used=50、nudges=45）：
# 模型改用 Claude 工具调用的 XML：
#
#     <parameter name="command">cd /work && python3 - <<'EOF' ... EOF</parameter>
#     <parameter name="tool">run_shell</parameter>
#     </invoke>
#
# 特点：没有 JSON 外层、参数体是纯文本（可含换行/bash heredoc/引号/花括号），
# 开标签 <invoke> 常常缺失，只有 </invoke> 收尾。v1.41/v1.44 都不认这套 ->
# 结果与老问题相同：不执行 + 不隐藏 + 烧纠偏（该轮出现 9 次以上
# 「参数错误：需要 `command`」= 工具被调起但没收到参数）。
# ══════════════════════════════════════════════════════════════════════


def test_normalize_parameter_style_call_becomes_action() -> None:
    """<parameter name=tool/command> 合成 ACTION:，命令原文一字不差。"""
    raw = (
        "我先确认工作区。\n"
        '<parameter name="command">ls -la /work/r/ 2>&1 | head -20</parameter>\n'
        '<parameter name="tool">run_shell</parameter>\n'
        "</invoke>"
    )
    out = _normalize_xml_actions(raw)
    assert "<parameter" not in out and "</invoke>" not in out
    assert "我先确认工作区。" in out  # 人话保留
    assert parse_actions(out) == [("run_shell", {"command": "ls -la /work/r/ 2>&1 | head -20"})]


def test_normalize_parameter_body_with_heredoc_and_braces_is_verbatim() -> None:
    """参数体含换行/heredoc/引号/花括号（bash 脚本）——原样保留，不当 JSON 切。"""
    script = "cd /work/chunks && python3 - <<'EOF'\nd={'a': 1}\nprint(d)\nEOF"
    raw = (
        '<parameter name="command">' + script + "</parameter>\n"
        '<parameter name="tool">run_shell</parameter>\n</invoke>'
    )
    out = _normalize_xml_actions(raw)
    actions = parse_actions(out)
    assert len(actions) == 1
    name, args = actions[0]
    assert name == "run_shell"
    assert args["command"] == script  # 逐字节相等（含换行与花括号）


def test_normalize_invoke_open_tag_present() -> None:
    """带 <invoke> 开标签也认（模型有时写全）。"""
    raw = (
        '<invoke name="run_shell">'
        '<parameter name="command">uname -a</parameter>'
        "</invoke>"
    )
    out = _normalize_xml_actions(raw)
    assert parse_actions(out) == [("run_shell", {"command": "uname -a"})]


def test_normalize_parameter_does_not_touch_plain_text() -> None:
    """普通回复（无任何协议标签）逐字节不变，绝不误伤。"""
    raw = "这是一段普通说明，里面有 < 号和 > 号，还有 {花括号}。"
    assert _normalize_xml_actions(raw) == raw


def test_has_protocol_marker_or_xml_detects_parameter_style() -> None:
    """带 <parameter 时也算已有协议，不该再合成原生动作。"""
    assert _has_protocol_marker_or_xml('<parameter name="tool">run_shell</parameter>') is True
    assert _has_protocol_marker_or_xml("</invoke>") is True
