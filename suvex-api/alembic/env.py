"""Alembic 运行环境。

两件事在这里定死：

1. **数据库 URL 从 `Settings` 读**，不从 `alembic.ini` 读。
   写在 ini 里等于把口令复制一份进 git，而且那份永远不会被更新 ——
   表现是「改了 .env 但迁移还是连旧库」。
   命令行覆盖用 `-x db_url=postgresql+psycopg://...`。

2. **`target_metadata` 来自 `app.models` 的汇合导入**。
   只 import `Base` 是不够的：metadata 只包含已导入的模型类，
   漏导入的领域在 autogenerate 眼里就是「要删掉的表」。
   这不是假想的风险，是 autogenerate 最常见的事故。
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

# 让 `alembic` 命令在项目根以外的 CWD 下也能 import 到 app
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.core.db import Base  # noqa: E402
from app.models import __all__ as _registered_models  # noqa: E402,F401  （副作用：注册所有表）

config = context.config

if config.config_file_name is not None:
    # `disable_existing_loggers=False` 是必要的：alembic 的 ini 里
    # root logger 是 WARNING，默认的 fileConfig 会**关掉**所有已存在的 logger，
    # 包括应用自己的。表现是「跑完迁移之后应用日志全没了」。
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    """命令行 `-x db_url=...` 优先，否则用 Settings。"""
    override = context.get_x_argument(as_dictionary=True).get("db_url")
    return override or get_settings().sync_database_url


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连数据库（`alembic upgrade head --sql`）。

    这个模式在 CI 里有用 —— 可以在没有数据库的情况下检查迁移脚本
    能否渲染成合法 SQL。注意它**不校验语义**（生成的 SQL 可能在真库上失败），
    所以它不能替代对着真库跑一次。
    """
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连真库执行。"""
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _database_url()

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # 让 autogenerate 能发现「列类型改了」与「server_default 改了」。
            # 两者默认是关的，而它们恰恰是最容易悄悄漂移的两类改动。
            compare_type=True,
            compare_server_default=True,
            # 迁移在一个事务里跑完 —— PostgreSQL 支持事务性 DDL，
            # 所以一条迁移中途失败不会留下半套表结构。
            transaction_per_migration=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
