"""时间：全后端唯一的「取当前时刻」与「格式化」出口。

两条规矩，别绕过：

1. **只用 tz-aware 的 UTC**。数据库列全部是 `TIMESTAMP WITH TIME ZONE`，
   代码里一律 `now_utc()`，不允许 `datetime.now()`（那是本地时间且 naive）。
   混用 naive/aware 的报错只在跨时区或跨夏令时的时候才出现，
   而那时数据已经写进去了。

2. **对外格式只有一个**：`2026-09-23T15:30:42.123Z`（毫秒 + `Z`）。
   契约 §1.1 定的是 ISO 8601 带时区，前端 `ActivityEvent.at` 还拿它排序。
   交给 Pydantic 默认序列化会得到 `+00:00` 而不是 `Z` —— 两者都是合法 ISO 8601，
   但前端 mock 里全是 `Z`，混着来会让「字符串比较排序」在某些写法下错位。
   所以统一用 `IsoDateTime` 注解，不靠框架默认。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated

from pydantic import PlainSerializer


def now_utc() -> datetime:
    """当前时刻，tz-aware UTC。全后端唯一的「现在」。"""
    return datetime.now(timezone.utc)


def to_iso(value: datetime) -> str:
    """格式化成 `YYYY-MM-DDTHH:MM:SS.mmmZ`。

    naive 值按 UTC 处理而不是报错：数据库驱动在某些配置下会返回 naive，
    这时如果抛异常，表现是「某个接口在某种驱动下 500」——
    换算成 UTC 至少是确定的行为。
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return f"{value.strftime('%Y-%m-%dT%H:%M:%S')}.{value.microsecond // 1000:03d}Z"


def parse_iso(value: str) -> datetime:
    """解析 ISO 8601。`Z` 后缀要能认 —— 前端发的都是这个形状。

    Python 3.11+ 的 `fromisoformat` 已经认识 `Z`，但不能认 `+0800`（无冒号）
    这类变体，所以补一个兜底替换。
    """
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:  # pragma: no cover - 由调用方转成 400
        raise ValueError(f"无法解析为 ISO 8601 时间：{value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def seconds_between(start: datetime, end: datetime) -> int:
    """两个时刻之间的秒数（向下取整，不为负）。"""
    return max(0, int((end - start).total_seconds()))


def hours_from_now(hours: float) -> datetime:
    return now_utc() + timedelta(hours=hours)


# 用在 Pydantic 模型上的时间类型。校验侧仍是 datetime，
# 只在序列化时套 to_iso —— 请求体里的 `Z` / `+00:00` 两种写法都能收。
IsoDateTime = Annotated[datetime, PlainSerializer(to_iso, return_type=str)]
