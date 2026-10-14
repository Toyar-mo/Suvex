"""ID 生成。

契约 §1.1 定了**主键是字符串**，风格形如 `scan_01J8X2`、`agent-03`、`evt_09`。
这里统一按「前缀 + `_` + ULID」产出，理由：

* **不用自增整数**：契约明确说了前端 `id` 全是 `string`。改成整数要动前端类型，
  没有任何收益。
* **不用 UUID4**：UUID 是 36 字符且无顺序，做游标（`Last-Event-ID` 补发）时
  只能再查时间列。ULID 前 48 位是毫秒时间戳，**字典序 = 时间序**，
  同一个 ID 列既当主键又当排序键。
* **不用 UUIDv7**：Python 3.12 标准库没有，而多引一个依赖不值得 ——
  ULID 的实现只有十几行，且没有依赖。

ULID 的 Crockford base32 刻意去掉了 I / L / O / U：
前三个是为了「肉眼抄写时 1/0 不分」的歧义（这是要出现在 URL 里的东西），
U 是为了避免拼出脏词。所以它是 **32 个符号**、26 字符表示 128 位。
"""

from __future__ import annotations

import secrets
import time
from datetime import UTC, datetime

# Crockford base32：无 I / L / O / U
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

_TIMESTAMP_BITS = 48
_RANDOM_BITS = 80
# 26 个 5 位组 = 130 位，覆盖 48+80=128 位并留 2 位高位填充
_ULID_CHARS = 26


def new_ulid() -> str:
    """26 字符 ULID。同一毫秒内多次调用也不会重复（80 位随机）。"""
    timestamp_ms = int(time.time() * 1000) & ((1 << _TIMESTAMP_BITS) - 1)
    randomness = int.from_bytes(secrets.token_bytes(_RANDOM_BITS // 8), "big")
    value = (timestamp_ms << _RANDOM_BITS) | randomness
    return "".join(_CROCKFORD[(value >> (5 * shift)) & 0x1F] for shift in range(_ULID_CHARS - 1, -1, -1))


def new_id(prefix: str) -> str:
    """带前缀的字符串主键，例如 `scan_01J8X2ABCDEFGHJKMNPQRSTV`。"""
    return f"{prefix}_{new_ulid()}"


def agent_id(ordinal: int) -> str:
    """agent 用序号而不是 ULID。

    它**总是属于某次扫描**，序号在扫描内唯一且可读 ——
    契约举例是 `agent-03`，Activity 页名册上人也要看它。
    用 ULID 的话界面上会出现 `agent_01J8X2ABCDEF…`，纯粹是噪音。
    """
    return f"agent-{ordinal:02d}"


def report_code(created_at: datetime | None = None) -> str:
    """报告编号 `RPT-YYYYMMDD-XXXX`（契约 §2.7）。

    ⚠️ 由**生成动作**分配，不是草稿阶段。前端 `ReportDocument.meta.provisional`
    记过这个坑：早期版本预览显示 `RPT-...-3253`、下载得到 `RPT-...-3310`，
    客户拿编号对账时对不上。所以这个函数只在真正落库那一刻调用一次，
    之后编号跟着报告行走，不再重算。

    后 4 位用 Crockford 字母表（大写、无歧义字符），保证是
    `[0-9A-HJKMNP-TV-Z]{4}` —— 前端 `reportCode()` 按这个形状解析。
    """
    moment = created_at or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    date_part = moment.astimezone(UTC).strftime("%Y%m%d")
    random_part = "".join(secrets.choice(_CROCKFORD) for _ in range(4))
    return f"RPT-{date_part}-{random_part}"
