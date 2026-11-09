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
from app.chat import service as chat_service
from app.chat.models import ChatSession
from app.core.timeutil import now_utc

_log = logging.getLogger(__name__)

#: 扫描间隔。10 秒是刻意的：接手延迟 = 心跳阈值(90s) + 这个间隔，
#: 用户能感觉到"自己接上了"，而 6 次/分钟的空查询代价可以忽略。
POLL_SECONDS = 10.0

#: 应用正在关闭（由 `app.main` 的 lifespan 在取消后台任务**之前**置位）。
#:
#: 取消协程是进程生命周期的一部分，不是这一轮的失败。接手逻辑据此把
#: claim 用掉的额度还回去 —— 否则每次部署都消耗一次自动接手，三次重启
#: 就能把一轮本身健康的长任务判死（2026-11-09 实测事故）。
_shutting_down = False


def mark_shutting_down() -> None:
    """标记进程正在关闭。必须早于 `resume_task.cancel()` 调用。"""
    global _shutting_down
    _shutting_down = True

#: 自动接手彻底放弃时补进对话的那句话（v1.79）。
#:
#: ★ 起因（2026-11-09 实测）：会话 `chat_01M6YYF7AQ8YR6B27F24C4BHRB` 的一轮
#:   跑了 32 分钟 / 70 步 / 77 次工具后，模型接入点连续返回 500；首次失败时
#:   引擎写了「我在后台接着做」，后台也确实接手了 3 次 —— 但三次都失败、
#:   `give_up` 把状态落成 `failed`，**对话里却没有任何后续消息**。
#:   于是最后一句永远停在那句承诺上，而实际上什么都不会再发生。
#:
#: 放弃时必须补一句：用户看到的是"到底还跑不跑、我下一步该做什么"。
GIVE_UP_NOTE = (
    "（自动接手 {attempts} 次仍未成功：{reason}。"
    "这一轮先停在这里 —— 发「继续」可以再试；如果反复这样，先检查模型接入点的状态。）"
)


def _append_give_up_note(
    db: Session, session: ChatSession | None, *, reason: str, attempts: int
) -> None:
    """在放弃接手后补一条说明。

    失败不影响状态本身（`give_up` 已经落库）—— 记账失败只记 warning，
    不能因为"多写一句话"把后台循环弄崩。
    """
    if session is None or getattr(session, "archived", False):
        return
    try:
        chat_service.append_assistant_message(
            db,
            session=session,
            content=GIVE_UP_NOTE.format(attempts=attempts, reason=reason),
        )
    except Exception:  # noqa: BLE001 - 见上
        _log.warning("放弃接手后补说明失败：%s", getattr(session, "id", "?"), exc_info=True)


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
        reason = "会话所有者的账号已不存在，无法接手。"
        turn_runs.give_up(db, run, reason=reason)
        _append_give_up_note(db, session, reason=reason, attempts=run.resume_attempts)
        db.commit()
        return False

    _log.info("接手中断的一轮：%s（第 %d 次）", run.id, run.resume_attempts)
    # ★ v1.33.1：resumer 走的也是本进程里的一轮真实执行。不登记的话，
    #   它跑长工具时心跳过期，前端会看到「可继续」黄条；用户一点，
    #   `claim()` 因为看不到活跃租约而放行，同进程就出现两个执行体。
    turn_runs.mark_active(run.id)
    try:
        try:
            result = await engine.run_turn(
                db,
                session=session,
                content="",
                identity=identity,
                turn_run=run,
                resume_from=run,
            )
            # ★ v1.32：后台接手走的是 `run_turn` 本身，不经过 `_run_guarded`；
            #   不在这里折叠，被接手的一轮就会留下多条结论气泡。
            engine._collapse_turn_answers(db, result)
        except BaseException as exc:  # noqa: BLE001 - 与 engine._run_guarded 同因
            if _shutting_down and isinstance(exc, asyncio.CancelledError):
                # 进程关闭不是这一轮的失败：退还 claim 用掉的额度，心跳置为过期，
                # 重启后的接手循环立刻能捡起它 —— 而不是把三次部署当成三次失败。
                try:
                    db.rollback()
                    turn_runs.release_for_restart(
                        db, run, reason="服务重启，这一轮将在启动后自动接手。"
                    )
                    db.commit()
                except Exception:  # noqa: BLE001
                    _log.warning("关停时记录接手状态失败：%s", run.id, exc_info=True)
                raise
            reason = turn_runs.redact(f"{type(exc).__name__}: {exc}")
            # ★ v1.22：凭证类失败（密钥无效）是**确定性**的 —— 再跑一百次都是
            #   同一个 401，而每一轮要白等 90 秒心跳窗口，用户只会看到同一条
            #   错误反复出现。直接放弃，把"去改密钥"写进 `last_error`。
            retryable = bool(getattr(exc, "retryable", True))
            try:
                db.rollback()
                if not retryable:
                    _log.warning("这一轮失败不可重试（凭证/权限），不再接手：%s", run.id)
                    turn_runs.give_up(db, run, reason=reason)
                    # ★ 已经判死：最后一条消息还停在"我在后台接着做"就是假话
                    _append_give_up_note(
                        db, session, reason=reason, attempts=run.resume_attempts
                    )
                elif run.resume_attempts >= turn_runs.MAX_RESUME_ATTEMPTS:
                    turn_runs.give_up(db, run, reason=reason)
                    _append_give_up_note(
                        db, session, reason=reason, attempts=run.resume_attempts
                    )
                else:
                    turn_runs.finish_from_error(db, run, reason=reason)
                db.commit()
            except Exception:  # noqa: BLE001
                _log.warning("接手失败后记账也失败：%s", run.id, exc_info=True)
            return False

        # ★ v1.35：与 `_run_guarded_active` 保持同一条收尾语义。
        #   后台接手走的是 `run_turn` 本身、不经过 `_run_guarded_active`，
        #   所以「停在计划句上」的分流必须在这里再做一次 —— 否则被接手
        #   的一轮即使又一次停在计划上也会被记成 `done`，用户看到的仍是
        #   「没解出又结束」（G1-review P1_RESUMER_MARKS_DONE）。
        if result.stopped:
            turn_runs.finish(db, run, status="cancelled")
        elif result.needs_continue:
            turn_runs.finish_incomplete(db, run)
        else:
            turn_runs.finish(db, run, status="done")
        db.commit()
        return True
    finally:
        turn_runs.unmark_active(run.id)


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
