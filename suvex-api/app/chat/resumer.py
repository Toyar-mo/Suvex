"""后台接手：把"心跳停了但还没结束"的对话轮次接着跑完（v1.19）。

## 为什么必须有一个后台循环，而不是只在请求里兜底

用户原话是「没有长任务持久化啊」。请求内兜底只能覆盖"异常还在本进程里
抛得出来"的情况；而真正丢掉进度的场景是**进程没了**（重启、OOM、部署），
那时候没有任何栈帧可以兜。所以唯一的办法是：把进度落库，然后由**下一次
启动之后还活着的进程**去把它捡起来。

## 谁触发

- 启动时：`close_stale_on_startup()` 把遗留 `running` 改判 `interrupted`，
  避免"心跳很新"这个假象让它们永远不被接手（见 `turn_runs` 模块说明）；
- 之后由 `app.main` 的 lifespan 起一个后台任务，每 `POLL_SECONDS` 扫一次。

## 为什么用「心跳」而不是「进程内注册表」

进程内注册表在重启后就没了 —— 那正是需要接手的时刻。心跳在库里。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.chat import engine, turn_runs
from app.chat.models import ChatSession
from app.core.timeutil import now_utc

_log = logging.getLogger(__name__)

#: 扫描间隔。10 秒是刻意的：接手延迟 = 心跳阈值(90s) + 这个间隔，
#: 用户能感觉到"自己接上了"，而 6 次/分钟的空查询代价可以忽略。
POLL_SECONDS = 10.0


def _identity_for(db: Session, session: Any) -> Any:
    """给后台接手造一个身份。

    ⚠️ 必须按**会话所有者当前的角色**算 scope，不能给 admin。
    后台任务没有请求上下文，但它替用户执行工具 —— 如果一律给 admin，
    一个 viewer 的会话在崩溃后重启会被提权成管理员去跑 `run_shell`。
    """
    from app.auth import service as auth_service
    from app.auth.deps import Identity
    from app.core.vocab import scopes_for_role

    owner = getattr(session, "created_by", None)
    account = auth_service.get_account_by_id(db, owner) if owner else None
    if account is None:
        return None
    return Identity(
        kind="session",
        label=f"resume:{session.id}",
        scopes=scopes_for_role(account.role),
        user_id=account.id,
        session_id=None,
        username=account.username,
        role=account.role,
    )


async def resume_one(db: Session, run: Any) -> bool:
    """接手一轮。返回是否真的跑了。"""
    if not turn_runs.claim(db, run, now=now_utc()):
        return False

    session = db.get(ChatSession, run.session_id)
    if session is None or session.archived:
        turn_runs.give_up(db, run, reason="会话已删除或已归档，不再接手。")
        db.commit()
        return False

    identity = _identity_for(db, session)
    if identity is None:
        turn_runs.give_up(db, run, reason="会话所有者的账号已不存在，无法接手。")
        db.commit()
        return False

    _log.info("接手中断的一轮：%s（第 %d 次）", run.id, run.resume_attempts)
    try:
        await engine.run_turn(
            db,
            session=session,
            content="",
            identity=identity,
            turn_run=run,
            resume_from=run,
        )
    except BaseException as exc:  # noqa: BLE001 - 与 engine._run_guarded 同因
        reason = turn_runs.redact(f"{type(exc).__name__}: {exc}")
        try:
            db.rollback()
            if run.resume_attempts >= turn_runs.MAX_RESUME_ATTEMPTS:
                turn_runs.give_up(db, run, reason=reason)
            else:
                turn_runs.finish_from_error(db, run, reason=reason)
            db.commit()
        except Exception:  # noqa: BLE001
            _log.warning("接手失败后记账也失败：%s", run.id, exc_info=True)
        return False

    turn_runs.finish(db, run, status="done")
    db.commit()
    return True


async def resume_stale_once(db: Session) -> int:
    """扫一遍并接手，返回接手了几轮。"""
    done = 0
    for run in turn_runs.stale_runs(db, now=now_utc()):
        if await resume_one(db, run):
            done += 1
    return done


async def run_forever(
    session_factory: Any, *, poll_seconds: float = POLL_SECONDS
) -> None:
    """后台常驻循环。`app.main` 的 lifespan 负责起停。

    ## ★ 参数是 **sessionmaker**，不是"能返回 sessionmaker 的函数"

    这里踩过一个坑，而且是最难自查的那一类：`app.main` 原来传的是
    `get_session_factory`（一个**函数**），循环里写 `db = db_factory()`，
    拿到的却是 `sessionmaker` 本身 —— 于是 `db.scalars(...)` 每 10 秒抛
    `AttributeError: 'sessionmaker' object has no attribute 'scalars'`。

    它被下面那个 `except Exception` 降级成一条 WARNING，日志看起来
    岁月静好，而**自动接手从来没有发生过一次**。线上表现就是"对话停了
    就永远停着"，与没做这个功能一模一样。

    所以：签名收窄成 `session_factory`（`db.sessionmaker` 实例），循环里
    `session_factory()` 才是一次真正的会话；并把"拿到的是不是 Session"
    钉进 `tests/chat/test_turn_persistence.py` 的回归用例里。
    """
    while True:
        try:
            await asyncio.sleep(poll_seconds)
            db = session_factory()
            try:
                await resume_stale_once(db)
            finally:
                db.close()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 单次扫描失败不能打死整个循环
            _log.warning("接手扫描失败（下一轮继续）", exc_info=True)


__all__ = [
    "POLL_SECONDS",
    "resume_one",
    "resume_stale_once",
    "run_forever",
]
