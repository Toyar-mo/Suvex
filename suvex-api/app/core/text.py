"""文本清洗：把 PostgreSQL 存不下的字符挡在写库之前。

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
"""

from __future__ import annotations

from typing import Any

# PostgreSQL 拒绝的唯一控制字符。
_NUL = "\x00"


def strip_nul(value: str) -> str:
    """去掉字符串里的 NUL 字节。"""
    return value.replace(_NUL, "")


def strip_nul_opt(value: str | None) -> str | None:
    """可空版本 —— `poc_code`、`remediation` 这类字段本来就允许是 None。"""
    return None if value is None else strip_nul(value)


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
