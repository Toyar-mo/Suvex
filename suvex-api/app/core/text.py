"""文本清洗：把存不进库的字符挡在写库之前，并拆开 strix 的代码围栏。

## 为什么需要它

PostgreSQL 的 `text` / `jsonb` **存不了 NUL 字节**（`\\u0000`）。
插进去得到的不是"存成空字符"，而是一个错误：

    psycopg.errors.UntranslatableCharacter: unsupported Unicode escape sequence
    DETAIL:  \\u0000 cannot be converted to text.

实测踩到（2026-09-27）：agent 抓 HTTP 响应时把一段**二进制**（PNG 头部
`\\x89PNG\\r\\n\\x1a\\n\\x00`）记进了事件的 `detail`。那一条**永远插不进去**，
而它的破坏力远不止自己：

* 导入器按 `source_row_id` 游标续读，那一条卡住 → **游标不再前进**；
* 同批里已经 add 但未提交的正常事件**被一起回滚**；
* 于是这次扫描后面的 100 条事件、连同**漏洞导入**（它在事件导入之后）
  全部丢失 —— 而界面上只表现为"这次没扫到东西"，失败只记在 debug 日志里。

## 为什么只删 NUL

PostgreSQL 只拒绝这一个控制字符。Tab、换行、CR 都是正文的一部分，
删了会改变证据的原貌 —— 而证据的完整性正是这个产品要保证的东西。

## 为什么围栏也在这里

`split_fenced_code` 处理的是**同一个形状**的两个消费者：活动流
（`scans/strix_events.py` 的 `poc_generated` 载荷）与漏洞落库
（`findings/strix_vulns.py` 的 `poc_code` / `poc_language`）。
形状由 strix 的 `report/writer.py` 单方面定义，两处各写一份正则
必然会漂移 —— 而漂移的那一侧只在真实扫描里才暴露。
"""

from __future__ import annotations

import re
from typing import Any

# PostgreSQL 拒绝的唯一控制字符。
_NUL = "\x00"

# strix 存 agent 生成的脚本时把它包在 markdown 围栏里，围栏**带语言标签**
# （`report/writer.py` 的 docstring：`poc_script_code` 之类的字段是
# ```` ```python\n…\n``` ```` 这样存的）。这个正则是**照抄它自己的**
# `_FENCE_RE` —— 同一个形状由上游定义，自己写一个近似的就会对不上。
_FENCE_RE = re.compile(r"^```([^\n`]*)\r?\n(.*?)\r?\n?```$", re.DOTALL)


def strip_nul(value: str) -> str:
    """去掉字符串里的 NUL 字节。"""
    return value.replace(_NUL, "")


def strip_nul_opt(value: str | None) -> str | None:
    """可空版本 —— `poc_code`、`remediation` 这类字段本来就允许是 None。"""
    return None if value is None else strip_nul(value)


def split_fenced_code(raw: str) -> tuple[str | None, str]:
    """把 strix 存的代码拆成 `(语言, 代码)`；没有围栏时语言是 `None`。

    ⚠️ **`None` 与 `"text"` 不是一回事**：`None` 表示"本来就没有围栏"
    （调用方该退回启发式判断），而围栏里写着 `text` 表示 strix 自己
    就不知道语言。把两者混成一个值，调用方就没法区分了。

    返回 `None` 而不是编一个 `python`：语言标签会被前端拿去做语法高亮，
    猜错会让整段代码变成一片红。
    """
    match = _FENCE_RE.match(raw.strip())
    if match is None:
        return None, raw
    return match.group(1).strip() or None, match.group(2)


def strip_nul_deep(value: Any) -> Any:
    """递归清洗。

    事件的 `detail` 是 JSON（dict / list / str 混着），必须整体过一遍 ——
    只清洗顶层字符串会漏掉嵌在里面的那一个，而一个就够了。
    """
    if isinstance(value, str):
        return strip_nul(value)
    if isinstance(value, dict):
        # 键也要洗：jsonb 的键同样是 text。
        return {strip_nul_deep(k): strip_nul_deep(v) for k, v in value.items()}
    if isinstance(value, list):
        return [strip_nul_deep(item) for item in value]
    if isinstance(value, tuple):
        return tuple(strip_nul_deep(item) for item in value)
    return value
