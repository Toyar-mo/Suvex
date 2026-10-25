"""守 `except` 里的 rollback 纪律：只在会话真的坏了才回。

## 守的是什么

`app/scans/service.py` 的 `build_spec` 与 `app/kernel/service.py` 的写卡
路径都有一段「读配置失败就降级」的 `except Exception`。降级本身是对的
（一次配置故障不该拦住启动扫描），但**回收方式**有两种错法：

1. **不回滚**：异常被吞、会话停在 DEACTIVE，之后任何 ORM 访问都抛
   `PendingRollbackError`，报错点指向毫不相干的地方。
   实测现场：`tests/test_scan_artifact.py::test_build_spec_hands_strix_a_local_directory`
   在 `if scan.run_id:` 炸掉，根因是 `pk_suvex_config` 主键冲突被吃掉。
   —— 这是**真是的线上 bug**，不是假想。

2. **无条件回滚**：会吞掉调用方未提交的改动。实测
   （`_tmp/er/probe_except_rollback.py`）：rollback 后 note 从 'modified'
   变回 'original'。而这两个函数的调用方正是带着未提交业务状态进来的。
   —— 我第一版就写成了这样，是自己引入的第二个 bug。

正确判据是 `db.is_active`：只有数据库层错误（`DBAPIError` 及其子类，
如主键冲突）才把它置 False。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.db import get_engine, get_session_factory
from app.settings.models import SINGLETON_ID, KernelConfig

_SCANS = Path(__file__).resolve().parents[1] / "app" / "scans" / "service.py"
_KERNEL = Path(__file__).resolve().parents[1] / "app" / "kernel" / "service.py"


def test_is_active_false_only_after_db_error(  # noqa: ANN201
    db_session,
):
    """`db.is_active` 才是正确判据 —— 先把这条事实钉住。

    如果哪天 SQLAlchemy 改了语义（普通异常也让 is_active 变 False），
    下面两条守卫的依据就不成立了，应该在这里先红。
    """
    db_session.add(KernelConfig(id=SINGLETON_ID))
    db_session.commit()

    other = get_session_factory()()
    try:
        # 普通异常：会话仍是好的
        try:
            raise ValueError("解析失败之类")
        except ValueError:
            pass
        assert other.is_active is True, "普通异常不该让会话变砖"

        # 数据库层错误：会话变砖
        with pytest.raises(IntegrityError):
            other.add(KernelConfig(id=SINGLETON_ID))
            other.commit()
        assert other.is_active is False, "主键冲突后会话应该是坏的"

        other.rollback()
        assert other.is_active is True, "rollback 后应恢复"
    finally:
        other.close()


def test_rollback_after_db_error_does_not_lose_pending_work():
    """反过来说明「为什么不能无条件 rollback」：

    rollback 会丢掉未提交改动 —— 所以只能在会话已经坏掉（本来就用不了）
    的时候回。这条钉住的是**权衡的另一半**。
    """
    # ⚠️ 必须把 Session **绑到我们显式持有的 Connection** 上。
    #
    # 踩过的坑：直接用 `get_session_factory()()`，单跑这条绿，和
    # test_singleton_race.py 同跑就红 —— `relation "_probe_guard" does not
    # exist`。实测（`_tmp/er/test_probe_temp_ctx.py`）：
    #     create pid=49672 → after_commit pid=30540
    # `Session.commit()` 结束事务并把连接还回 pool，下次 execute 重新
    # checkout 可能拿到**另一条物理连接**；TEMP TABLE 是连接级对象，于是
    # "消失"。单跑时池里只有一条连接、恰好拿回原连接，所以看不出问题。
    # 同一文件里先跑过起线程/多连接的用例，池里有多条，就会飘。
    #
    # 绑到显式 Connection 后，commit 只结束事务、不还池（实测 pid 不变、
    # insert_ok=True、rollback 后 count=0）。
    conn = get_engine().connect()
    other = Session(bind=conn)
    try:
        # ⚠️ 建表必须**先提交**。PostgreSQL 里 CREATE TEMP TABLE 也是事务内的，
        #    和后面的 INSERT 一起 rollback 会把表本身也撤掉（第一版就踩了这个：
        #    之后 SELECT 报 `relation "_probe_guard" does not exist`）。
        other.execute(text("CREATE TEMP TABLE _probe_guard (v int)"))
        other.commit()

        other.execute(text("INSERT INTO _probe_guard VALUES (42)"))
        # 不 commit —— 这才是"未提交的业务改动"
        assert other.execute(text("SELECT count(*) FROM _probe_guard")).scalar() == 1

        other.rollback()

        n = other.execute(text("SELECT count(*) FROM _probe_guard")).scalar()
        assert n == 0, "rollback 没有丢掉未提交数据？那这条测试的前提变了，请复核"
    finally:
        other.close()
        conn.close()


@pytest.mark.parametrize(
    ("path", "marker"),
    [
        (_SCANS, "读 MCP 连接/工具开关失败"),
        (_KERNEL, "读 CTF2 配置失败"),
    ],
)
def test_degradation_path_guards_rollback_with_is_active(path: Path, marker: str) -> None:
    """★ 真正的守卫：这两处 `except` 必须写成 `if not db.is_active: db.rollback()`。

    用源码断言而不是行为断言，因为要**精确**守住「条件判断存在」这一点 ——
    行为断言只能证明「某种情况下没崩」，证明不了「不会误伤调用方」。
    """
    src = path.read_text(encoding="utf-8")

    idx = src.find(marker)
    assert idx != -1, f"{path} 里找不到降级日志 {marker!r}（代码被改过，请更新测试锚点）"

    # 往回找最近的 except 子句
    window = src[max(0, idx - 1200) : idx]
    assert "except Exception" in window, f"{path} 的 {marker!r} 不在 except Exception 块里了，请复核"

    # 断言守卫存在，且不是无条件 rollback
    assert "if not db.is_active:" in window, (
        f"{path} 的降级路径缺少 `if not db.is_active:` 守卫。\n"
        "无条件 rollback 会吞掉调用方未提交的改动（见本文件 docstring）。"
    )
    assert "db.rollback()" in window, f"{path} 的降级路径缺少 db.rollback()"

    # 确认 rollback 在守卫之内（缩进更深一层），不是同一层
    guard_pos = window.rfind("if not db.is_active:")
    rb_pos = window.rfind("db.rollback()")
    assert rb_pos > guard_pos, f"{path} 里 db.rollback() 不在 is_active 守卫之内"
    guard_indent = len(window[guard_pos:]) - len(window[guard_pos:].lstrip())
    rb_line_start = window.rfind("\n", 0, rb_pos) + 1
    rb_indent = rb_pos - rb_line_start
    assert rb_indent > guard_indent, f"{path} 里 db.rollback() 的缩进没有比守卫更深 —— 它可能在守卫外面"
