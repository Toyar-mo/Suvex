"""数据库：引擎、会话、`Base`。

**命名约定（`naming_convention`）是必须的，不是风格问题。**
SQLAlchemy 默认不给 CHECK / UNIQUE / FK 起名字，PostgreSQL 会自己编一个
（`findings_scan_id_fkey` 这种）。等到某个迁移要 `drop_constraint` 时，
就不得不去猜数据库当时编了什么名 —— 而名字在不同环境可能不同。
统一约定之后，约束名是**算出来的**，迁移脚本可复现。

引擎按需创建（`configure()` / `get_engine()`），不做模块级常量：
测试要能指向另一个库，`lru_cache` 的 settings 换掉之后引擎必须跟着换。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

# 约束命名模板。`%(constraint_name)s` 是显式命名的约束，其余按类型自动生成。
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """所有 ORM 表的基类。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def configure(database_url: str | None = None, *, echo: bool | None = None, force: bool = False) -> Engine:
    """建立（或重建）全局引擎。返回新引擎。"""
    global _engine, _session_factory

    if _engine is not None and not force:
        return _engine

    from app.core.config import get_settings

    cfg = get_settings()
    url = database_url or cfg.sync_database_url

    kwargs: dict = {"echo": cfg.db_echo if echo is None else echo, "pool_pre_ping": True}
    if url.startswith("sqlite"):
        # SQLite 只用于少数纯逻辑测试。`check_same_thread=False` 是必需的，
        # 因为 FastAPI 的同步端点跑在线程池里，连接会跨线程使用。
        from sqlalchemy.pool import StaticPool

        kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" in url:
            kwargs["poolclass"] = StaticPool
    else:
        kwargs["pool_size"] = cfg.db_pool_size
        kwargs["max_overflow"] = cfg.db_max_overflow

    _engine = create_engine(url, **kwargs)
    _session_factory = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
    return _engine


def get_engine() -> Engine:
    return _engine if _engine is not None else configure()


def get_session_factory() -> sessionmaker[Session]:
    if _session_factory is None:
        configure()
    assert _session_factory is not None
    return _session_factory


def reset() -> None:
    """丢掉全局引擎与事务级缓存。

    给测试用：换库、改配置之后必须调它，否则会复用旧连接池 ——
    表现是「测试里写进了临时库，读出来的却是主库的数据」。
    """
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：每个请求一个会话。

    **不在这里 commit** —— 提交由 service 层决定。自动提交会让
    「校验失败但已经写了半条记录」这类问题在事务边界上无法回滚。
    只在异常时 rollback，请求结束一律 close。
    """
    session = get_session_factory()()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """脚本 / 后台任务用的会话：进出各一次事务，异常回滚。"""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def ping() -> bool:
    """探活。健康检查用它，而不是靠「能建 Session」——
    建 Session 是惰性的，不会真的连数据库。"""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
