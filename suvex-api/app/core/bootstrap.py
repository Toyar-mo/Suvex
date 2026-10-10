"""启动自检。

**为什么启动时要查表存不存在**：`alembic upgrade head` 是一条独立的命令。
如果忘了跑（全新环境最常见），应用会正常起来、健康检查也返回 ok，
然后第一个业务请求才炸在 `relation "admin_accounts" does not exist`。
那个报错在日志里长得像代码 bug，而不是"少跑了一条命令"。

所以启动时直接查一次，并且给出一句**带命令的中文提示**。

刻意**不**在启动时自动跑迁移：

* 迁移失败会变成"应用起不来"而不是"迁移命令失败"，日志归属错了；
* 多副本时每个副本都会去抢着迁移；
* 迁移是有副作用的操作，把它藏在启动流程里，运维就看不到它发生了。

`docker-compose.yml` 里的做法是让后端容器的启动命令是
`alembic upgrade head && uvicorn ...` —— 这样 `docker compose up` 仍然是一步，
而迁移在日志里是独立且可见的一步。
"""

from __future__ import annotations

import logging

from sqlalchemy import inspect

from app.core.db import get_engine

logger = logging.getLogger("suvex.bootstrap")

# 只要这些表在，就认为迁移跑过了。取三张跨领域的表而不是全部 ——
# 检查的目的是"有没有跑过迁移"，不是"结构是否完全正确"（那是 alembic 的事）。
REQUIRED_TABLES = ("admin_accounts", "scans", "findings", "alembic_version")


def verify_database() -> None:
    """连库 + 查表。失败时抛 RuntimeError，消息里带修复命令。"""
    engine = get_engine()
    try:
        with engine.connect() as conn:
            existing = set(inspect(conn).get_table_names())
    except Exception as exc:  # noqa: BLE001 - 要把原始原因带进消息里
        raise RuntimeError(
            "无法连接数据库。请检查 SUPEX_DATABASE_URL 与数据库是否已启动。\n"
            f"原始错误：{exc}"
        ) from exc

    missing = [name for name in REQUIRED_TABLES if name not in existing]
    if missing:
        raise RuntimeError(
            "数据库结构不完整，缺少表："
            + "、".join(missing)
            + "。请先执行迁移：\n"
            "    alembic upgrade head\n"
            "（若用 docker compose，这一步已经在容器启动命令里自动执行。）"
        )

    logger.info("数据库连接正常，结构检查通过（%d 张表）", len(existing))
