"""列类型：可移植 JSON 与统一的时间列。

`JsonColumn` 在 PostgreSQL 上是 **JSONB**、在别处是普通 JSON。
JSONB 不是「更快的 JSON」，它**解析后存储**，所以：

* 可以直接 `@>` 查询与建 GIN 索引（将来「找出所有含某个 skill 的扫描」用得上）；
* 重复键会被规范化（普通 `json` 列会原样保留两份，读出来是哪份看运气）。

而 SQLite 上没有 JSONB，用 `.with_variant` 让同一份模型定义两边都能建表 ——
少数纯逻辑测试不需要起 PostgreSQL，这个便利换来的是「同一份 DDL 两种后端」，
代价是测试覆盖不到 PG 专有行为。所以**涉及数据库语义的测试一律对 PG 跑**，
SQLite 只用于不碰数据库的纯逻辑用例。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime
from sqlalchemy.dialects.postgresql import JSONB

# 可移植 JSON/JSONB
JsonColumn = JSON().with_variant(JSONB(), "postgresql")

# 所有时间列都用它。tz-aware 是硬要求（见 core/timeutil 的说明）。
#
# ⚠️ 不用 `server_default=func.now()`：默认值由应用层给（`now_utc`），
# 这样「事件发生时刻」与「入库时刻」在测试里可控，也能保证同一事务里
# 多条记录的时间戳完全一致（用数据库 now() 会在事务边界上拿到不同值，
# 而 `transaction_timestamp()` 虽然稳定但要写 SQL 表达式，不划算）。
UtcDateTime = DateTime(timezone=True)


def iso_or_none(value: datetime | None) -> str | None:
    """可空的 ISO 格式化，给不走 Pydantic 的场合（日志、脚本）用。"""
    if value is None:
        return None
    from app.core.timeutil import to_iso

    return to_iso(value)
