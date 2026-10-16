"""NUL 字节清洗（`app/core/text.py`）——一个让导入永久卡死的字符。

## 为什么值得单独一个测试文件

PostgreSQL 的 `text` / `jsonb` **存不了 NUL 字节**（`\\u0000`），插进去是一条
硬错误（`UntranslatableCharacter: unsupported Unicode escape sequence`）。

它本身只是"一条数据写不进去"，但实测（2026-09-27）的杀伤力远大于此：

* 导入器靠 `source_row_id` 游标续读，那一条卡住 → **游标不再前进**；
* 同批里已经 add 但未提交的正常事件被一起回滚；
* 事件导入排在漏洞导入**之前**，于是一整轮下来 100 条事件连同那条漏洞
  全部丢失，而界面上只表现为"这次没扫到东西"。

触发它的是一个很普通的动作：agent 抓 HTTP 响应，图片/二进制的头部
（`\\x89PNG\\r\\n\\x1a\\n\\x00`）被原样记进了事件 detail。
"""

from __future__ import annotations

from app.core.text import split_fenced_code, strip_nul, strip_nul_deep, strip_nul_opt


def test_strips_nul_from_plain_string() -> None:
    assert strip_nul("a\x00b") == "ab"


def test_none_passes_through_optional_helper() -> None:
    """`poc_code` 这类字段本来就允许 None，别把 None 变成字符串 \"None\"。"""
    assert strip_nul_opt(None) is None
    assert strip_nul_opt("a\x00b") == "ab"


def test_strips_nul_inside_nested_json() -> None:
    """★ 递归是必须的。

    事件 `detail` 是 dict / list / str 混着的 JSON，NUL 通常埋在
    三四层里（比如 `detail["result"]["output"]`）。只洗顶层等于没洗。
    """
    payload = {
        "kind": "tool_call",
        "command": "curl -s http://t/favicon.ico",
        "result": {
            "output": "HTTP/1.1 200\r\n\r\n\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR",
            "nested": [{"raw": "x\x00y"}],
        },
    }

    cleaned = strip_nul_deep(payload)

    assert "\x00" not in repr(cleaned)
    # 结构本身不变 —— 清洗不是"把不认识的都扔掉"
    assert cleaned["kind"] == "tool_call"
    assert cleaned["result"]["nested"][0]["raw"] == "xy"


def test_only_nul_is_removed_not_other_control_chars() -> None:
    """★ 只删 NUL。

    PostgreSQL 只拒绝这一个控制字符；Tab 与换行是正文的一部分，
    删了会改变证据的原貌 —— 而证据完整性正是这个产品要保证的东西。
    """
    text = "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\nline1\n\tline2"
    assert strip_nul(text) == text


def test_strips_nul_from_json_keys_too() -> None:
    """jsonb 的键也是 text，同样存不了 NUL。"""
    assert strip_nul_deep({"k\x00ey": "v"}) == {"key": "v"}


def test_non_string_values_are_untouched() -> None:
    payload = {"n": 3, "f": 1.5, "b": True, "none": None}
    assert strip_nul_deep(payload) == payload


# ══════════════════════════════════════════════════════════
# 漏洞解析：所有文本字段的必经之处
# ══════════════════════════════════════════════════════════


def test_vulnerability_parse_strips_nul_from_every_text_field() -> None:
    """`_text()` 是漏洞字段的唯一入口，NUL 不该穿过它。

    漏洞的 `evidence` 里常常粘着原始 HTTP 响应（可能含二进制），
    所以这条路径和事件导入一样会撞上 NUL。
    """
    from app.findings import strix_vulns

    record = {
        "id": "vuln-0001",
        "title": "标题\x00带NUL",
        "severity": "high",
        "confidence": "high",
        "timestamp": "2026-09-27 11:49:43 UTC",
        "description": "描述\x00",
        "evidence": "响应体：\x89PNG\r\n\x1a\n\x00\x00",
        "poc_script_code": "print('\x00')",
        "cwe": "CWE-312\x00",
    }

    parsed = strix_vulns.parse([record], default_target="http://t")

    assert len(parsed) == 1
    item = parsed[0]
    for field in ("title", "description", "evidence", "poc_code", "cwe", "target"):
        assert "\x00" not in (getattr(item, field) or ""), f"{field} 仍含 NUL"
    # 内容没被整体丢掉，只是那个字符没了
    assert item.title == "标题带NUL"
    assert "PNG" in item.evidence


# ── 代码围栏（`split_fenced_code`）────────────────────────────
#
# strix 把 agent 生成的脚本**包在带语言标签的 markdown 围栏里**存
# （`report/writer.py` 的 docstring 原话：agent-generated code fields
# are stored wrapped in a markdown fence carrying the language）。
# 它自己的每个消费者都先拆围栏：TUI 用 `ParseFencedCode`、
# viewer 用 `parseFencedCode`、PDF 用 `_strip_code_fence`。
# 我们以前不拆 —— 围栏行连同 ```python 一起进了 `poc_code`。


def test_splits_a_fenced_code_block() -> None:
    language, code = split_fenced_code("```python\nimport requests\nprint(1)\n```")
    assert language == "python"
    assert code == "import requests\nprint(1)"


def test_a_fence_without_a_language_gives_none_not_text() -> None:
    """★ 没有语言标签 ≠ 语言是 `text`。

    `None` 表示"围栏没说语言"（调用方该退回启发式判断），
    而 `"text"` 表示"上游说了语言，就是纯文本"。把两者混成一个值，
    调用方就没法决定该不该再猜一次了。
    """
    language, code = split_fenced_code("```\nplain\n```")
    assert language is None
    assert code == "plain"


def test_unfenced_code_comes_back_untouched() -> None:
    """没有围栏时**原样返回**，连首尾空白都不动。

    调用方要靠"有没有围栏"来分流，所以这一侧不能顺手 `strip()` ——
    那会把一份本来就合法的裸代码改掉。
    """
    language, code = split_fenced_code("import requests\nprint(1)")
    assert language is None
    assert code == "import requests\nprint(1)"


def test_only_a_surrounding_fence_is_stripped() -> None:
    """围栏必须**整段包住**才算。

    代码体里自己带 ``` （比如一份讲 markdown 的 PoC）不该被腰斩 ——
    正则锚在 `^…$`，所以这种输入会走"没有围栏"那一支。
    """
    raw = "```python\nprint('```')\nprint('x')\n```"
    language, code = split_fenced_code(raw)
    assert language == "python"
    assert code == "print('```')\nprint('x')"


def test_crlf_fences_are_handled() -> None:
    """strix 在 Windows 上写出来的围栏是 CRLF 的（正则里 `\\r?\\n` 就是为它）。"""
    language, code = split_fenced_code("```python\r\nprint(1)\r\n```")
    assert language == "python"
    assert code == "print(1)"
