"""数据库：引擎、会话、`Base`。

**命名约定（`naming_convention`）是必须的，不是风格问题。**
SQLAlchemy 默认不给 CHECK / UNIQUE / FK 起名字，PostgreSQL 会自己编一个
（`findings_scan_id_fkey` 这种）。等到某个迁移要 `drop_constraint` 时，
就不得不去猜数据库当时编了什么名 —— 而名字在不同环境可能不同。
统一约定之后，约束名是**算出来的**，迁移脚本可复现。

引擎按需创建（`configure()` / `get_engine()`），不做模块级常量：
测试要能指向另一个库，`lru_cache` 的 settings 换掉之后引擎必须跟着换。

## ★ 附带写入（统计 / 审计 / 遥测 / 题解）不许 `db.rollback()`

`Session.rollback()` 是**会话级**的：它不知道、也不区分哪部分改动是调用方
的、哪部分是本函数自己的。所以只要一个函数

  · 被调用时会话里可能还压着调用方**未提交**的业务改动，且
  · 自己失败时调了 `db.rollback()`，

那它就会把调用方那笔业务改动一起吞掉 —— 而它的 docstring 通常写着
"失败不影响主流程"，**正好相反，且安静**：主流程照常返回成功，日志只有
一条 warning，只有事后翻数据才会发现状态被降级了。

实测（v0.45）：`ctf2.record_solve_telemetry` 就是这么写的，一次"遥测写不进去"
把一次**成功的解题**记成了未解出。修法是 `db.begin_nested()`（SAVEPOINT）：
失败只退到保存点，调用方未提交的改动原样保留。

    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except Exception:
        with contextlib.suppress(Exception):
            db.expunge(row)      # 详见 ctf2.service 里的说明
        logger.warning(...)
        return False

**并且不要自己 `db.commit()`**：提交是外层事务边界的事。自己 commit 会让
"这次成功了但后面某步失败"变成"一半落了盘一半没有"，也让失败时的
`rollback` 变成上面那个 bug。

### 已经排查过的调用点（2026-10-18，用 `grep` 扫 `app/` 下所有 `.rollback()`）

真实调用点 12 处，逐个判定：

| 位置 | 判定 |
| --- | --- |
| `get_db`（本文件） | 安全 —— 回滚后 `raise`，会话本就是请求边界 |
| `session_scope`（本文件） | 安全 —— 同上 |
| `findings/attachments.py`（写附件失败） | 安全 —— 回滚后 `raise`，调用方拿到异常 |
| `members/service.py`（IntegrityError） | 安全 —— 回滚后 `raise ApiError`，无未提交业务改动 |
| `audit/service.py` `record()` | 安全 —— 36 个调用点都在 service 已 commit 之后 |
| `scans/service.py` `emit_lifecycle_event` | 安全 —— 3 个调用点都先 commit 过 |
| `findings/service.py` `_refresh_scan_rollups` | 安全 —— 唯一调用点在 `db.commit()` 之后 |
| `scans/scheduler.py` 11 处 | 安全 —— 每处都在自己的 try 段里，段前已 commit 或本段就是全部改动 |
| `ctf2/service.py` `record_solve_telemetry` | **已修**（SAVEPOINT，v0.45） |
| `ctf2/service.py` `record_solve_usages` | **已修**（v0.46，同一段同模式） |
| `ctf2/service.py` `save_writeup` | **已修**（v0.46，同一段同模式） |

⚠️ `scans/service.py` 的 `refresh_scan_rollup` 会自己 `db.commit()`，而它是被
`ctf2.events.finalize_solve_scan` 在**别人的事务中间**调用的 —— 那会提前提交
调用方未提交的改动。目前 4 个调用点都无害（甚至帮了倒忙式的忙），所以不改；
但它是同一类反模式（内层函数替外层定事务边界），将来若被新路径复用要先想清楚。
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
