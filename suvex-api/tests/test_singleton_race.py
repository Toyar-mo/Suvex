"""单例配置的并发 get-or-create 不该撞主键。

## 守的是什么

`KernelConfig` / `FindingSlaConfig` / `ScanRetentionConfig` 都是「全局只有一行」
的配置表。它们的读法原本是：

    row = db.get(Model, "default")      # ← 读到 None
    if row is None:
        row = Model(id="default")
        db.add(row)
        db.commit()                     # ← IntegrityError

两个会话同时执行，两边都看见 None，都去 INSERT 同一主键 →
`IntegrityError: duplicate key value violates unique constraint "pk_..."`。

## 为什么这事难查（这才是写这组测试的理由）

调用方通常这么写：

    try:
        cfg = get_ctf2_config(db)
    except Exception:
        logger.warning("读配置失败，按默认处理")

异常被吞了，**但没人 rollback**。SQLAlchemy 的 Session 因此停在
DEACTIVE 状态，之后任何一次属性访问都会抛

    PendingRollbackError: This Session's transaction has been rolled back due
    to a previous exception during flush.

而报错位置指向**下一个毫不相干的属性访问**。真实失败现场（后台全量
pytest，80 分钟那一轮）：

    tests/test_scan_artifact.py::test_build_spec_hands_strix_a_local_directory

    sqlalchemy.exc.PendingRollbackError: This Session's transaction has been
    rolled back due to a previous exception during flush. To begin a new
    transaction with this Session, first issue Session.rollback().
    Original exception was: (psycopg.errors.UniqueViolation) duplicate key
    value violates unique constraint "pk_suvex_config"
    DETAIL:  Key (id)=(default) already exists.

失败点是 `app/scans/service.py:938` 的 `if scan.run_id:` —— 和配置表毫无
关系的一行。链条是：`build_spec` → `get_ctf2_config(db)`（:913）→ 看见
None → `db.add` + `db.commit` → UniqueViolation → 被 :918 的
`except Exception: logger.warning(...)` 吞掉、**但没 rollback** →
会话 DEACTIVE → 后面的 `scan.run_id` 取过期属性当场炸。

单跑该文件永远通过 —— 因为只有全量跑时才会有别的用例先建了那行配置。

## 怎么造竞态（第一版写错在这里）

真实竞态的关键时序是「**get 读到 None 之后**，别人才提交」。测试里如果
先让别人 commit、再调 get_or_create_singleton，那么函数开头的 `db.get`
会读到那一行并直接返回 —— 插入分支根本不会执行，改坏了也不会红（实测
0/3，三条全是假绿）。

所以必须让那次 get **返回过期的 None**：用 `mock.patch` 劫持 `Session.get`，
只对目标模型+主键的**第一次**调用返回 None，此后走真实实现。这就精确复现
了「拿过期结果去 INSERT」这一步。
"""

from __future__ import annotations

from unittest import mock

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.db import get_engine, get_or_create_singleton, get_session_factory
from app.settings.models import SINGLETON_ID, KernelConfig

_REAL_GET = Session.get


def _stale_first_get():
    """返回一个 patch 上下文：对 KernelConfig 的第一次 get 返回 None。

    模拟「本会话先读、读到 None」的那一瞬间；真实并发下这个 None 会很快
    过期，但我们只关心「基于过期结果去 INSERT」会不会炸。
    """
    state = {"calls": 0}

    def wrapper(self, entity, ident, *args, **kwargs):
        if entity is KernelConfig and ident == SINGLETON_ID and state["calls"] == 0:
            state["calls"] += 1
            return None
        return _REAL_GET(self, entity, ident, *args, **kwargs)

    return mock.patch.object(Session, "get", wrapper)


@pytest.fixture()
def seeded(db_session):
    """确保那一行已由「别人」建好并提交。"""
    db_session.add(KernelConfig(id=SINGLETON_ID))
    db_session.commit()
    return db_session


def test_get_or_create_returns_existing_row_without_inserting(seeded):
    """已存在时直接返回，不该再建一条（也不该抛）。"""
    first = get_or_create_singleton(seeded, KernelConfig, SINGLETON_ID)
    assert first.id == SINGLETON_ID

    second = get_or_create_singleton(seeded, KernelConfig, SINGLETON_ID)
    assert second.id == SINGLETON_ID

    count = seeded.query(KernelConfig).count()
    assert count == 1, f"单例行建了 {count} 条"


def test_concurrent_create_loses_gracefully(seeded):
    """★ 核心用例：get 读到过期的 None，随后 INSERT 撞主键 ——
    必须**返回已存在的那一行**，而不是把 IntegrityError 抛给调用方。
    """
    other = get_session_factory()()
    try:
        with _stale_first_get():
            row = get_or_create_singleton(other, KernelConfig, SINGLETON_ID)

        assert row.id == SINGLETON_ID
        assert other.query(KernelConfig).count() == 1
    finally:
        other.close()


def test_session_usable_after_losing_race(seeded):
    """★ 事故的**真实症状**：撞主键后若没清掉坏状态，下一个无关查询就炸
    `PendingRollbackError`。这里直接断言「下一个无关查询能跑」。

    如果有人把 get_or_create_singleton 改成裸 add+commit（不 rollback /
    不用 SAVEPOINT），这里会红。
    """
    other = get_session_factory()()
    try:
        with _stale_first_get():
            get_or_create_singleton(other, KernelConfig, SINGLETON_ID)

        # 一条和配置表毫无关系的查询 —— 事故里就是这种地方炸的
        assert other.execute(text("SELECT 1")).scalar() == 1
    finally:
        other.close()


def test_pending_changes_survive_the_race(seeded):
    """★ 调用方带着未提交的业务改动时，撞一次主键不该把那些改动吞掉。

    这条守的是「修法不能比问题更糟」：我第一版在 except 里调了
    `db.rollback()`，实测会把调用方未提交的改动一起丢掉
    （`_tmp/er/probe_savepoint.py` 的输出）。
    """
    # ⚠️ 绑到显式持有的 Connection：TEMP TABLE 是连接级对象，而
    # `Session.commit()` 会把连接还回 pool，下次 execute 可能 checkout 到
    # **另一条物理连接**，临时表就"消失"了（实测 pid 会变）。
    # 当前这条测试恰好走的是「撞主键 → 重读命中 → 提前 return」，
    # 没有 commit 发生，所以侥幸不飘；但一旦 `get_or_create_singleton`
    # 的成功路径被改动（或 seeded 未生效），就会变成难查的 flake。
    # 同修法见 tests/test_rollback_discipline.py。
    conn = get_engine().connect()
    other = Session(bind=conn)
    try:
        other.execute(text("CREATE TEMP TABLE _probe_pending (v int)"))
        # ⚠️ 建表必须先 commit，只让 INSERT 停在不提交状态。
        #
        # 否则被改坏点的 `db.rollback()` 会把**表本身**一起回滚掉，
        # 报错变成 `UndefinedTable: relation "_probe_pending" does not exist`
        # —— 测试确实红了，但诊断指向"基础设施坏了"，而不是"回滚吞掉了
        # 调用方的改动"。实测过这个假诊断，和我上一轮踩的
        # 「红了但不是因为那条断言」是同一类陷阱。
        # 提交建表后，改动只能落在 INSERT 那一行上，断言才会响亮。
        # （绑定的 Connection 不受影响：commit 只结束事务、不还池。）
        other.commit()
        other.execute(text("INSERT INTO _probe_pending VALUES (42)"))
        # 刻意不 commit —— 这就是「待提交的业务改动」

        with _stale_first_get():
            get_or_create_singleton(other, KernelConfig, SINGLETON_ID)

        n = other.execute(text("SELECT count(*) FROM _probe_pending")).scalar()
        assert n == 1, "撞主键把调用方未提交的改动吞掉了"
    finally:
        other.close()
        conn.close()


def test_fallback_branch_retries_inside_savepoint(seeded):
    """★ 「撞了却读不到」的兜底分支：重试的插入也必须在 SAVEPOINT 里。

    时序（READ COMMITTED 下极窄，但存在）：

        get → None
        INSERT → IntegrityError（对手已提交）
        重读 → None（对手在这中间又删了那一行）
        ← 兜底：再插一次，这次应该成功

    第一版兜底写的是裸 `db.add` + `db.commit()`，**没有 SAVEPOINT**。
    那样一旦重试的插入又撞上，IntegrityError 会不带保护地抛出去 ——
    正是本文件开头描述的那个「会话变砖」的 bug 原地复活。
    """
    other = get_session_factory()()
    try:
        state = {"calls": 0}

        def wrapper(self, entity, ident, *args, **kwargs):
            if entity is KernelConfig and ident == SINGLETON_ID:
                state["calls"] += 1
                if state["calls"] == 1:
                    return None  # 过期的 None → 去 INSERT → 撞
                if state["calls"] == 2:
                    # 模拟「对手提交后又删了它」：另一个会话真的删掉那行
                    killer = get_session_factory()()
                    try:
                        killer.query(KernelConfig).delete()
                        killer.commit()
                    finally:
                        killer.close()
                    return None  # 重读也读不到 → 进兜底
            return _REAL_GET(self, entity, ident, *args, **kwargs)

        with mock.patch.object(Session, "get", wrapper):
            row = get_or_create_singleton(other, KernelConfig, SINGLETON_ID)

        assert row is not None, "兜底分支返回了 None —— 调用方会在远处炸"
        assert row.id == SINGLETON_ID
        assert other.query(KernelConfig).count() == 1
    finally:
        other.close()


def test_fallback_branch_never_returns_none(seeded):
    """★ 两次都撞、两次都读不到时：必须响亮失败，**绝不返回 None**。

    返回 None 会让调用方拿着空对象在很远的地方炸 AttributeError
    （正是本文件开头那个「失败点指向毫不相干的一行」的症状）。
    而且失败之后会话必须仍然可用 —— SAVEPOINT 已回退，`is_active` 为 True。
    """
    other = get_session_factory()()
    try:

        def wrapper(self, entity, ident, *args, **kwargs):
            if entity is KernelConfig and ident == SINGLETON_ID:
                # 每次都返回 None，但库里那行**始终存在** → 每次 INSERT 都撞
                return None
            return _REAL_GET(self, entity, ident, *args, **kwargs)

        with (
            mock.patch.object(Session, "get", wrapper),
            pytest.raises(RuntimeError, match="反复主键冲突"),
        ):
            get_or_create_singleton(other, KernelConfig, SINGLETON_ID)

        # 失败之后会话必须还能用（不能留下 DEACTIVE）
        assert other.is_active is True, "失败把会话弄坏了"
        assert other.execute(text("SELECT 1")).scalar() == 1

        # 解开 mock 之后一切照常 —— 库里那行还在，读得到
        row = get_or_create_singleton(other, KernelConfig, SINGLETON_ID)
        assert row.id == SINGLETON_ID
    finally:
        other.close()


def test_sla_config_call_site_survives_the_race(db_session):
    """★ 走**真实调用点** `app.findings.service.sla_config`，不是裸函数。

    上面几条测的都是 `get_or_create_singleton` 本身；这条守的是
    「调用点接对了没有」。`sla_config` 原先是自己写的
    `if db.get() is None: db.add + commit + refresh`，改成走
    `get_or_create_singleton` 之后，如果谁把它改回去（或漏改某处），
    下面这个「读到过期 None」的场景会当场撞主键。
    """
    from app.findings.service import sla_config
    from app.settings.models import FindingSlaConfig

    # 先由「别人」建好并提交
    db_session.add(FindingSlaConfig(id=SINGLETON_ID))
    db_session.commit()

    other = get_session_factory()()
    try:
        real_get = Session.get
        state = {"calls": 0}

        def stale(self, entity, ident, *args, **kwargs):
            # 只让第一次读到过期的 None（模拟「我先读、别人才提交」）；
            # 之后的读取走真实实现 —— 否则重试也读不到，会走兜底分支
            # 报 RuntimeError，测的就不是「撞了能否优雅输」这条路径了。
            if entity is FindingSlaConfig and ident == SINGLETON_ID and state["calls"] == 0:
                state["calls"] += 1
                return None
            return real_get(self, entity, ident, *args, **kwargs)

        with mock.patch.object(Session, "get", stale):
            cfg = sla_config(other)

        # 返回的是别人的那一行，不是 None、也没抛
        assert cfg.id == SINGLETON_ID
        assert other.query(FindingSlaConfig).count() == 1
        # 会话没被弄坏
        assert other.is_active is True
        assert other.execute(text("SELECT 1")).scalar() == 1
    finally:
        other.close()


def test_sla_config_creates_with_documented_defaults(db_session):
    """库里没有时，`sla_config` 必须用 `DEFAULT_SLA_DAYS` 建出来。

    这条守的是「接线时别把 defaults 漏掉」——`get_or_create_singleton`
    的 `**defaults` 是纯关键字，漏传不会报错，只会在运行期给出全 None
    的 SLA 天数，然后 `sla_days_for` 算出一个荒唐的截止时间。
    """
    from app.findings.service import sla_config
    from app.settings.models import DEFAULT_SLA_DAYS, FindingSlaConfig

    assert db_session.query(FindingSlaConfig).count() == 0, "前置：库里不该有这一行"

    cfg = sla_config(db_session)

    assert cfg.id == SINGLETON_ID
    for severity, days in DEFAULT_SLA_DAYS.items():
        assert getattr(cfg, f"{severity}_days") == days, f"{severity} 的默认天数没落库"
    assert db_session.query(FindingSlaConfig).count() == 1

    # ★ 新建的那行必须**已经提交**：`sla_config` 的调用方（SLA 计算、
    # 配置页）拿到对象后要立刻能读，且下次调用必须命中同一行而不是再插一次。
    # 如果谁把 get_or_create_singleton 的 `db.commit()` 去掉（改成只 flush），
    # 下面这条会红：另一条连接看不见未提交的行 → 又建一条 → 撞主键。
    other = get_session_factory()()
    try:
        assert other.query(FindingSlaConfig).count() == 1, "新建的行没提交，别的会话看不见"
        again = sla_config(other)
        assert again.id == SINGLETON_ID
        assert other.query(FindingSlaConfig).count() == 1, "第二次调用又建了一行"
    finally:
        other.close()
