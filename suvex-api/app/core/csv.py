"""CSV 单元格的安全渲染：公式注入防护 + RFC 4180 引号转义。

Excel/Sheets 会把以 `= + - @` 或制表符/换行开头的单元格当公式执行。
漏洞标题、目标、扫描摘要、审计 User-Agent 都来自外部输入，不能进入公式
解析器。纯数字（包括负数）保留原样，避免把数值列变成文本。
"""

from __future__ import annotations

import re

_NUMERIC = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")
_UNSAFE_START = ("=", "+", "-", "@", "\t", "\r", "\n")


def guard_formula(value: str) -> str:
    """只做公式前缀防护，不负责 CSV 引号；给 `csv.writer` 用。"""
    text = str(value)
    if text.startswith(_UNSAFE_START) and _NUMERIC.fullmatch(text) is None:
        return "'" + text
    return text


def csv_cell(value: str) -> str:
    """公式防护 + CSV 引号转义；给手写 CSV 拼装用。"""
    text = guard_formula(value)
    if any(ch in text for ch in (",", '"', "\n", "\r")):
        return '"' + text.replace('"', '""') + '"'
    return text


__all__ = ["csv_cell", "guard_formula"]
