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
| `audit/service.py` `record()` | **已加固**（v0.47）—— 契约从注释变成机制，见下 |
| `scans/service.py` `emit_lifecycle_event` | **已加固**（v0.47）—— 同上 |
| `findings/service.py` `_refresh_scan_rollups` | 安全 —— 唯一调用点在 `db.commit()` 之后 |
| `scans/scheduler.py` 11 处 | 安全 —— 每处都在自己的 try 段里，段前已 commit 或本段就是全部改动 |
| `ctf2/service.py` `record_solve_telemetry` | **已修**（SAVEPOINT，v0.45） |
| `ctf2/service.py` `record_solve_usages` | **已修**（v0.46，同一段同模式） |
| `ctf2/service.py` `save_writeup` | **已修**（v0.46，同一段同模式） |

### 从"约定"到"机制"（v0.47）

上表里两处标"安全"的，安全性原本只建立在**写代码的人记得这条规矩**上：
`audit.record()` 依赖"36 个调用点都在 service 已 commit 之后"，
`emit_lifecycle_event` 依赖"3 个调用点都先 commit 过"。判断是对的，但支撑它的是
约定，不是代码 —— 谁在旁边加一行调用，就会安静地违反它。

现在这两处开头都调 `warn_if_pending_changes()`：

- **不抛异常**。附带写入的基本原则是"绝不失败"，在审计路径上抛等于让业务 500。
  违约是**调用方的 bug**，不是业务错误，不该炸在用户脸上。
- **但行为真的不同**，不只是打条日志：违约时会话里压着别人未提交的改动，
  这时 `db.commit()` 会**替调用方把半截工作落盘**，`db.rollback()` 会**把它吞掉**，
  两种都不对 —— 所以改走 `begin_nested()`（SAVEPOINT），审计行等调用方自己的
  commit 一起落盘，失败了也只撤销这一行。
- 诚实记代价：走 SAVEPOINT 的收益是"绝不连累调用方"，代价是"调用方若不 commit，
  这条审计就跟着丢了"。但那是它违约在先 —— 符合本模块 docstring 开头的
  "**审计可能缺条**"。
- 日志带 `stack_info=True`，直接打出是谁在违约。

`tests/test_incidental_write_contract.py` 钉住这三件事（含"换了另一个连接看库、
确认 `record()` 没替调用方提交"这一条 —— 只看内存属性是看不出破的）。

⚠️ `scans/service.py` 的 `refresh_scan_rollup` 会自己 `db.commit()`，而它是被
`ctf2.events.finalize_solve_scan` 在**别人的事务中间**调用的 —— 那会提前提交
调用方未提交的改动。目前 4 个调用点都无害（甚至帮了倒忙式的忙），所以不改；
但它是同一类反模式（内层函数替外层定事务边界），将来若被新路径复用要先想清楚。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

logger = logging.getLogger("suvex.db")

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


def pending_change_count(db: Session) -> int:
    """会话里还有多少**未提交**的业务改动（新增 + 修改 + 删除）。

    ★ 存在的理由：`audit.record()` / `emit_lifecycle_event` 这类**附带写入**
    的安全性是建立在一条**只写在文档里、没有机制兜底**的约定上 ——
    "调用我的时候，你已经 commit 过了"。目前 36 个 + 3 个调用点全都遵守，
    但支撑它们的是"写代码的人记得这条规矩"，不是代码本身。

    这正是本项目反复踩的那类坑的另一种形态（文档/约定与实现脱节），
    只是这次还没出事。所以给出这个便宜探针：附带写入函数在动手前问一句，
    不满足就把契约被违反这件事**吵出来**（ERROR 级日志），
    而不是安静地把别人的未提交改动一起提交或一起回滚。

    为什么返回计数而不是 `raise`：附带写入的基本原则是"绝不失败"，
    在审计路径上抛异常等于让业务 500。吵出来 + 照常写，
    比"安静地做错"和"响亮地搞挂业务"都好。

    ⚠️ 返回计数而不是只记账，是为了让调用方**真的换一种写法**：
    违约时 `db.commit()` 会替别人把半截工作落盘、`db.rollback()` 会把它吞掉，
    两者都不对 —— 拿到这个计数后应改走 `begin_nested()`（SAVEPOINT）。
    """
    return len(db.new) + len(db.dirty) + len(db.deleted)


def warn_if_pending_changes(db: Session, *, where: str) -> int:
    """附带写入前调用：有未提交改动就记一条 ERROR，返回计数。

    只负责**喊出来**，不改变行为 —— 行为由调用方决定：拿到非 0 就应当
    放弃 `commit`/`rollback`，改走 `begin_nested()`（见本模块 docstring）。
    """
    count = pending_change_count(db)
    if count:
        logger.error(
            "附带写入发现会话里有 %d 处未提交的业务改动：%s —— "
            "这违反了「调用附带写入前必须先 commit」的约定（见本模块 docstring）。"
            "已改用 SAVEPOINT，不会替调用方提交、也不会吞掉它；"
            "但这条附带写入要等调用方 commit 才会落盘。"
            "请把调用点挪到 service 层 commit 之后。",
            count,
            where,
            stack_info=True,
        )
    return count


def ping() -> bool:
    """探活。健康检查用它，而不是靠「能建 Session」——
    建 Session 是惰性的，不会真的连数据库。"""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
