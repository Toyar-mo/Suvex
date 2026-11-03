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


# ─────────────────────────────────────────────────────────────────────
# v1.49：DeepSeek 的 DSML 全角标签（`</｜DSML｜ parameter>`）
#
# 线上实测（2026-11-02，会话 chat_01M6BWQ69MZJNBAB7C7QMVXPV2 的
# turn_01M6E99VZ…）：模型用全角竖线 U+FF5C 包的分隔符写
# `</｜DSML｜ parameter>` / `</｜DSML｜ invoke>` / `</｜DSML｜ calls>`。
# v1.41/v1.44/v1.48 的正则都只认半角 `<parameter>`/`<invoke>`，这套
# 一个都不命中 → 该轮被判「无动作」，空转 75 轮反复说「现在真正运行」。
# ─────────────────────────────────────────────────────────────────────

_DSML = "\uff5c"  # 全角竖线 ｜


def test_dsml_closing_only_marks_are_removed_and_not_actions() -> None:
    """只有收尾标记、没有开标签：标记删掉，**不猜**成动作。"""
    text = (
        "我继续。\n\n"
        "cd /work/r && ls -l p.bin"
        f"\n</{_DSML}DSML{_DSML} parameter>"
        f"\n</{_DSML}DSML{_DSML} invoke>"
        f"\n</{_DSML}DSML{_DSML} calls>"
    )
    out = _normalize_xml_actions(text)
    assert "DSML" not in out
    assert _DSML not in out
    # 命令体（人话/正文）保留下来，且没有被误当成动作
    assert "ls -l p.bin" in out
    assert parse_actions(out) == []


def test_dsml_parameter_open_tag_becomes_action() -> None:
    """有 `<｜DSML｜ parameter name="...">` 开标签时规范成半角并合成动作。"""
    text = (
        f'<{_DSML}DSML{_DSML} invoke name="run_shell">'
        f'<{_DSML}DSML{_DSML} parameter name="command">echo dsml-ok</{_DSML}DSML{_DSML} parameter>'
        f'</{_DSML}DSML{_DSML} invoke>'
    )
    out = _normalize_xml_actions(text)
    assert "DSML" not in out
    assert parse_actions(out) == [("run_shell", {"command": "echo dsml-ok"})]


def test_dsml_mixed_named_and_closing() -> None:
    """开标签 + 收尾标记混用（真实形态）：能出动作，残留标记清干净。"""
    text = (
        "先跑一下。\n"
        f'<{_DSML}DSML{_DSML} invoke name="run_shell">'
        f'<{_DSML}DSML{_DSML} parameter name="command">id</{_DSML}DSML{_DSML} parameter>'
        f'</{_DSML}DSML{_DSML} calls>'
    )
    out = _normalize_xml_actions(text)
    assert "DSML" not in out
    assert parse_actions(out) == [("run_shell", {"command": "id"})]


def test_has_protocol_marker_or_xml_detects_dsml() -> None:
    assert _has_protocol_marker_or_xml(f"x</{_DSML}DSML{_DSML} calls>") is True


def test_dsml_plain_text_untouched() -> None:
    """普通回复（无 DSML）逐字节不变。"""
    s = "这是一段普通的话，没有 DSML，也没有竖线。"
    assert _normalize_xml_actions(s) == s


# ---------------------------------------------------------------------------
# v1.52：只有收尾 `</parameter>`、没有开标签的正文，曾经原样漏出（守卫集合缺
#   `</parameter>`），线上甚至被拼进 shell 命令（bash 报 `/parameter: No such
#   file or directory`）。以及模型自创的 `<ds_safety>` 安全自检块。
# ---------------------------------------------------------------------------

#: 线上 (2026-11-02, 会话 chat_01M6BWQ69MZJNBAB7C7QMVXPV2) seq794 正文的形态：
#: 一段裸命令 + 结尾孤零零的 `</parameter> 继续：…`
_LIVE_SEQ794 = (
    "容器又被回收了。关键产物 `scriptobjects.bin` 已保存到持久区，我用它继续工作。\n"
    "\n"
    "cd /work && ls -l /work/persist/chisaki/scriptobjects.bin 2>/dev/null; "
    "echo done</parameter> 继续：确认持久区里的拿到了 scriptobjects.bin，并重建 so。\n"
)


def test_lone_closing_parameter_tag_is_removed() -> None:
    """只有收尾 `</parameter>` 也不能漏出（回归 v1.48 守卫集合缺项）。"""
    out = _normalize_xml_actions(_LIVE_SEQ794)
    assert "</parameter>" not in out
    assert "<parameter" not in out
    # 命令正文与人话都在，只少了那个标记。
    assert "scriptobjects.bin" in out
    assert "继续：确认" in out


def test_lone_closing_parameter_does_not_corrupt_command() -> None:
    """线上的致命细节：`</parameter>` 拼进 shell 命令会让 bash 报错。
    规范化之后，命令里不能再出现 `/parameter` 这种要被当成路径执行的碎片。"""
    out = _normalize_xml_actions(_LIVE_SEQ794)
    assert "/parameter" not in out


def test_has_protocol_marker_or_xml_detects_lone_parameter_close() -> None:
    assert _has_protocol_marker_or_xml("x</parameter>") is True


def test_ds_safety_block_is_removed_keeps_surrounding_text() -> None:
    text = (
        "开头一句人话。\n"
        "<ds_safety>[用户未成年]否\n"
        "[分类]其他\n"
        "[判定]用户输入为继续分析 CTF 的技术请求。\n"
        "[规则]无</ds_safety>\n"
        "结尾一句人话。\n"
    )
    out = _normalize_xml_actions(text)
    assert "ds_safety" not in out
    assert "用户未成年" not in out
    assert "[判定]" not in out
    assert "开头一句人话" in out
    assert "结尾一句人话" in out


def test_bare_ds_safety_closing_tag_is_removed() -> None:
    out = _normalize_xml_actions("人话。</ds_safety>后面的话。")
    assert "ds_safety" not in out
    assert "人话。" in out and "后面的话" in out


def test_has_protocol_marker_or_xml_detects_ds_safety() -> None:
    assert _has_protocol_marker_or_xml("<ds_safety>x</ds_safety>") is True


# ---------------------------------------------------------------------------
# v1.53：认不出工具名的 `<parameter>` 组，曾经只删标签、把 body 留成正文 —— 而
#   v1.51 的 extract_trailing_command 会把这个"裸命令 body"当成要执行的命令
#   直接跑掉。一个认不出的协议碎片绝不能变成一条被执行的真实命令。现在整组丢弃。
# ---------------------------------------------------------------------------

def test_unrecognized_parameter_group_is_dropped_not_leaked() -> None:
    """缺 tool 的 parameter 组：body 也不能当正文漏出来（否则会被 auto-synth 执行）。"""
    text = '接下来执行。\n<parameter name="command">rm -rf /tmp/x</parameter>'
    out = _normalize_xml_actions(text)
    assert "rm -rf /tmp/x" not in out, "认不出的参数组 body 不该漏成正文"
    assert "<parameter" not in out
    # 人话保留。
    assert "接下来执行" in out


def test_unrecognized_parameter_group_does_not_become_autosynth_command() -> None:
    """端到端：认不出的参数组不能让 v1.51 抽出命令来执行。"""
    from app.chat.nudge import extract_trailing_command

    text = '接下来执行。\n<parameter name="command">cat /etc/shadow</parameter>'
    out = _normalize_xml_actions(text)
    assert extract_trailing_command(out) is None


def test_recognized_parameter_group_still_becomes_action_after_dropping_rules() -> None:
    """丢掉认不出的组，不能影响认得出的组正常合成动作。"""
    text = (
        '<parameter name="command">BAD</parameter> 中间人话 '
        '<parameter name="tool">run_shell</parameter>'
        '<parameter name="command">echo ok</parameter>'
    )
    out = _normalize_xml_actions(text)
    assert "BAD" not in out
    assert parse_actions(out) == [("run_shell", {"command": "echo ok"})]


def test_dsml_unrecognized_group_dropped() -> None:
    text = f'接下来。\n<{_DSML}DSML{_DSML} parameter name="command">rm -rf /tmp/x</{_DSML}DSML{_DSML} parameter>'
    out = _normalize_xml_actions(text)
    assert "rm -rf /tmp/x" not in out
    assert "DSML" not in out


# ---------------------------------------------------------------------------
# v1.56：`<system>…</system>` —— 模型自创的第四套 XML 包装，把意图碎片包起来
#   刷屏（线上会话「Server Host: https://pro.dasctl.c…」）。
#   策略=整块丢弃 + 只留真 ACTION：含真 `ACTION: {...}` 的只保留那段 ACTION 行，
#   其余 `ACTION.`/`执行.`/`先.` 纯碎片整块删、绝不执行。
# ---------------------------------------------------------------------------

def test_system_fragment_blocks_are_removed() -> None:
    """`<system>ACTION.</system>` 这类刷屏碎片整块删掉，正文不留标签。"""
    text = (
        "我先看一下。\n"
        "<system>ACTION.</system>\n"
        "<system>执行.</system>\n"
        "<system>先.</system>\n"
        "接下来反汇编驱动。"
    )
    out = _normalize_xml_actions(text)
    assert "<system" not in out
    assert "</system>" not in out
    # 碎片文字（ACTION./执行./先.）不应残留成正文。
    assert "ACTION." not in out
    assert "执行." not in out
    assert "先." not in out
    # 人话保留。
    assert "我先看一下" in out
    assert "接下来反汇编驱动" in out


def test_system_fragment_is_not_executed() -> None:
    """碎片里的词绝不能被当成命令执行（parse_actions 不认、auto-synth 也抽不出）。"""
    from app.chat.nudge import extract_trailing_command

    out = _normalize_xml_actions("<system>执行.</system><system>先.</system>")
    assert out.strip() == ""
    assert parse_actions(out) == []
    assert extract_trailing_command(out) is None


def test_system_block_with_real_action_is_unpacked() -> None:
    """`<system>ACTION: {...}</system>` → 展开成裸 ACTION 行，可被解析执行。"""
    text = (
        "现在跑。\n"
        '<system>ACTION: {"tool": "run_shell", "args": {"command": "echo hi"}}</system>'
    )
    out = _normalize_xml_actions(text)
    assert "<system" not in out
    assert parse_actions(out) == [("run_shell", {"command": "echo hi"})]
    # 人话保留。
    assert "现在跑" in out


def test_system_mixed_fragments_and_real_action() -> None:
    """混合：碎片块被删、真 ACTION 块被展开，两者共存时各走各的路。"""
    text = (
        "<system>先.</system>\n"
        '<system>ACTION: {"tool": "run_shell", "args": {"command": "id"}}</system>\n'
        "<system>执行.</system>"
    )
    out = _normalize_xml_actions(text)
    assert "<system" not in out
    assert parse_actions(out) == [("run_shell", {"command": "id"})]
    assert "执行." not in out and "先." not in out


def test_system_action_command_not_corrupted() -> None:
    """展开后命令体逐字节保留（不能被拼接污染）。"""
    cmd = "cd /work && objdump -dr note.ko > note.disasm.txt 2>&1"
    text = '<system>ACTION: {"tool": "run_shell", "args": {"command": "cd /work && objdump -dr note.ko > note.disasm.txt 2>&1"}}</system>'
    out = _normalize_xml_actions(text)
    assert parse_actions(out) == [("run_shell", {"command": cmd})]


def test_has_protocol_marker_or_xml_detects_system() -> None:
    assert _has_protocol_marker_or_xml("<system>x</system>") is True


def test_plain_text_without_system_is_unchanged() -> None:
    text = "这是一段普通回复，没有 system 标签。"
    assert _normalize_xml_actions(text) == text


# ── v1.57：整族 `system*` 标签 ─────────────────────────────────────
#   线上审计（chat_01M6BWQ69MZJNBAB7C7QMVXPV2）发现同族还有
#   `<system_warning>` / `<system_welcome/>` / `<system_call>{json}` 三种变体。
#   v1.56 的 `system\b` 正则因 `_` 是词字符，一个都没匹配到 → 31/33 条照样漏。


def test_system_warning_block_is_removed() -> None:
    """`<system_warning>…</system_warning>` 是平台纠偏提示，纯噪声，整块删。"""
    text = (
        "静态分析走到极限。\n"
        "<system_warning>你没写 ACTION，请补上。</system_warning>"
    )
    out = _normalize_xml_actions(text)
    assert "<system_warning" not in out
    assert "你没写 ACTION" not in out
    assert "静态分析走到极限" in out


def test_system_welcome_self_closing_is_removed() -> None:
    out = _normalize_xml_actions("开场。<system_welcome/>后续。")
    assert "<system_welcome" not in out
    assert "开场" in out and "后续" in out


def test_system_call_with_bare_tool_json_is_unpacked() -> None:
    """`<system_call>\n{"tool":…, "args":…}`（无闭合）→ 展开成可执行 ACTION。"""
    text = '<system_call>\n{"tool": "run_shell", "args": {"command": "ls -la /work"}}'
    out = _normalize_xml_actions(text)
    assert "<system_call" not in out
    assert parse_actions(out) == [("run_shell", {"command": "ls -la /work"})]


def test_system_call_empty_tag_is_dropped() -> None:
    """裸 `<system_call>`（后面没有 JSON）→ 删掉，不执行不残留。"""
    out = _normalize_xml_actions("做完了。<system_call>")
    assert "<system_call" not in out
    assert parse_actions(out) == []
    assert "做完了" in out


def test_system_call_invalid_json_is_dropped_not_executed() -> None:
    """`<system_call>{坏 JSON}` → 不猜、不执行，整块删。"""
    out = _normalize_xml_actions('<system_call>{"tool": broken json')
    assert "<system_call" not in out
    assert parse_actions(out) == []


def test_has_protocol_marker_or_xml_detects_system_family() -> None:
    for t in (
        "<system_warning>x</system_warning>",
        "<system_welcome/>",
        '<system_call>{"tool":"run_shell","args":{}}',
    ):
        assert _has_protocol_marker_or_xml(t) is True, t
