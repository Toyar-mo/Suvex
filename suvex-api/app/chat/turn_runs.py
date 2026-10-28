"""一轮对话的执行状态：持久化 + 谁来接手（v1.19）。

见 `app.chat.models.ChatTurnRun` 的类注释说明为什么需要这张表。这里只放
"怎么读写它"和"哪些该被接手"的判据。

## 两条互不重叠的判据

1. **该不该被接手**（`is_resumable`）—— 纯函数，只看状态、心跳、重试次数。
   纯函数的意义：这套判据的失败模式是静默的（该接手没接手 = 界面卡住；
   不该接手却接手 = 两轮并发写同一个会话），只能靠测试逐条钉住。
2. **谁来接手**（`claim`）—— 带条件的 UPDATE：只有"仍然是未结束状态"
   的行才会被改成 running 并把 `resume_attempts` +1。这是为了防止两个
   worker 同时捞到同一行（本项目是单 worker，但测试和多进程部署下会发生）。

## 为什么心跳而不是"看 updated_at"

`updated_at` 会被 `onupdate` 在**任何**写入时刷新（包括用户改个标题），
拿它当"这轮还活着吗"的判据会误判：一个卡住的轮次只要用户改了个设置，
就被当成"刚动过"，于是永远不被接手。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.chat.models import ChatTurnRun
from app.core.ids import new_id
from app.core.timeutil import now_utc

#: 心跳超过这么久没刷新，就认为那一轮"没人跑了"。
#:
#: 一次模型调用 + 一次工具执行在本项目的实测上限是几十秒（`run_shell`
#: 默认 300 秒超时那一类不考虑 —— 那种情况心跳会在工具返回后才更新，
#: 所以阈值必须大于"最慢一次合法单步"，否则会把正在跑的一轮当成死的，
#: 造出两轮并发写同一个会话）。取 90 秒：比最慢的合法单步长，又短到
#: 用户还能忍受"自动接着跑"的等待。
STALE_SECONDS = 90

#: 一轮最多被自动接手几次。到上限就放弃并把状态改成 `failed`。
#:
#: 为什么必须有上限：某些失败是**确定性的**（例如提示词让模型每次都写出
#: 超长回复而被截断），没有上限就是一个死循环，每 90 秒烧一次模型调用，
#: 而用户在界面上只会看到同一条错误反复出现。
MAX_RESUME_ATTEMPTS = 3

#: 还没结束的状态。`done` / `failed` 都不在其中。
OPEN_STATUSES = ("running", "interrupted")


def list_runs(db: Session, *, session_id: str, limit: int = 20) -> list[ChatTurnRun]:
    """按时间倒序列出一个会话的执行记录。

    ## 为什么界面需要看到这个

    用户报的「对话又没反应了」在界面上原本是**零信息**的：没有任何地方
    能回答"刚才那轮到底跑完了没有"。把执行记录暴露出去，最差也能让用户
    自己判断"这轮断了、可以点继续"，而不是只能刷新页面碰运气。

    `limit` 默认 20：这是给界面用的，不是审计报表 —— 一次会话通常只有
    几十轮，20 条足够覆盖用户会回头看的那一段。
    """
    stmt = (
        select(ChatTurnRun)
        .where(ChatTurnRun.session_id == session_id)
        .order_by(ChatTurnRun.updated_at.desc(), ChatTurnRun.id.desc())
        .limit(max(1, limit))
    )
    return list(db.scalars(stmt))


def get(db: Session, *, run_id: str) -> ChatTurnRun | None:
    """按 id 取一行（不抛 —— 是否 404 由调用方决定）。"""
    return db.get(ChatTurnRun, run_id)


def start(db: Session, *, session_id: str) -> ChatTurnRun:
    """开一轮，落一行 `running`。调用方负责 commit。"""
    stamp = now_utc()
    run = ChatTurnRun(
        id=new_id("turn"),
        session_id=session_id,
        status="running",
        heartbeat_at=stamp,
        started_at=stamp,
        updated_at=stamp,
    )
    db.add(run)
    db.flush()
    return run


def reset_for_manual_retry(db: Session, run: ChatTurnRun) -> None:
    """人工点「重试」时把一轮 `failed` 放回可接手状态（v1.22）。

    ## 为什么 `failed` 也能重试

    `failed` 的语义是「**自动**接手别再来」（重试额度用尽、或凭证错这种
    确定性失败）。但人点一下是**新的决定**：他可能刚按提示改好了密钥。
    拒绝他这个请求，等于告诉他"改完也没用"。

    ## 为什么要把心跳往回拨

    `claim` 只要求行处于未结束状态，但如果不拨心跳，这一行看起来"刚动过"，
    会让并发的后台扫描与这次点击产生不必要的竞态。拨到 `STALE_SECONDS + 1`
    秒之前，语义就是"确定没人跑"，与本函数调用者的意图一致。
    """
    run.status = "interrupted"
    run.resume_attempts = 0
    run.heartbeat_at = now_utc() - timedelta(seconds=STALE_SECONDS + 1)
    db.flush()


def heartbeat(
    db: Session,
    run: ChatTurnRun,
    *,
    round_index: int,
    nudges_used: int,
    tools_used: int,
    truncations_used: int = 0,
    tool_nudges_used: int = 0,
) -> None:
    """每轮更新一次进度与心跳。调用方负责 commit（与消息落库同一个事务）。"""
    run.round_index = round_index
    run.nudges_used = nudges_used
    #: `nudges_used` 的一个子集，见 `app.chat.engine.MAX_TRUNCATION_CONTINUES`。
    #: 持久化的理由与 `nudges_used` 相同：接手的那一轮要接着用**同一个**
    #: 额度，否则每次自动接手都把额度重置，切断的长回复可以无限续下去。
    run.truncations_used = truncations_used
    #: 见 `app.chat.engine.MAX_TOOL_NUDGES`。同样是"接手要接着用同一个额度"。
    run.tool_nudges_used = tool_nudges_used
    run.tools_used = tools_used
    run.heartbeat_at = now_utc()
    db.flush()


def finish(db: Session, run: ChatTurnRun, *, status: str, error: str | None = None) -> None:
    """收尾。`status` 只能是 `done` / `failed` / `cancelled`（v1.25）。

    `cancelled` = 用户主动「彻底停止」。它与 `failed` 的区别：失败是"没做成"，
    取消是"用户不让做了"。两者都**不**会被后台接手；但 `cancelled` 不该在
    界面上被标成红色错误。
    """
    if status not in ("done", "failed", "cancelled"):
        raise ValueError(
            f"finish() 不接受 {status!r}，它只收 done/failed/cancelled"
        )
    run.status = status
    run.last_error = redact(error) if error else None
    run.heartbeat_at = now_utc()
    db.flush()


def redact(error: str) -> str:
    """把异常压成一句给用户看的话。

    ⚠️ 不能整段回显：异常里经常带 URL、token、容器名。用户需要知道
    "哪一类失败、要不要重试"，不需要看到栈。
    """
    line = (error or "").strip().splitlines()[0] if error else ""
    return line[:300]


def is_resumable(
    run: ChatTurnRun,
    *,
    now: datetime,
    stale_seconds: int = STALE_SECONDS,
    max_attempts: int = MAX_RESUME_ATTEMPTS,
) -> bool:
    """这一轮该不该被后台接手。纯函数（见模块 docstring）。"""
    if run.status not in OPEN_STATUSES:
        return False
    if run.resume_attempts >= max_attempts:
        return False
    return run.heartbeat_at <= now - timedelta(seconds=stale_seconds)


def stale_runs(
    db: Session,
    *,
    now: datetime,
    stale_seconds: int = STALE_SECONDS,
    max_attempts: int = MAX_RESUME_ATTEMPTS,
    limit: int = 20,
) -> list[ChatTurnRun]:
    """心跳停了、还没结束、且还有重试额度的行。"""
    cutoff = now - timedelta(seconds=stale_seconds)
    stmt = (
        select(ChatTurnRun)
        .where(
            ChatTurnRun.status.in_(OPEN_STATUSES),
            ChatTurnRun.heartbeat_at <= cutoff,
            ChatTurnRun.resume_attempts < max_attempts,
        )
        .order_by(ChatTurnRun.heartbeat_at.asc())
        .limit(limit)
    )
    return list(db.scalars(stmt))


def claim(
    db: Session,
    run: ChatTurnRun,
    *,
    now: datetime,
    max_attempts: int = MAX_RESUME_ATTEMPTS,
) -> bool:
    """抢占这一轮：条件 UPDATE，返回是否抢到。

    条件里重复检查"还是未结束状态 + 额度未用尽"，因为从 `stale_runs()`
    到真正开跑之间有时间差，而这段时间里原来的 worker 可能刚好活过来
    继续写了进度。抢不到就跳过，绝不要带着过期的状态去跑。
    """
    result = db.execute(
        update(ChatTurnRun)
        .where(
            ChatTurnRun.id == run.id,
            ChatTurnRun.status.in_(OPEN_STATUSES),
            ChatTurnRun.resume_attempts < max_attempts,
        )
        .values(
            status="running",
            resume_attempts=ChatTurnRun.resume_attempts + 1,
            heartbeat_at=now,
            updated_at=now,
        )
    )
    db.commit()
    if result.rowcount != 1:
        return False
    db.refresh(run)
    return True


def close_stale_on_startup(db: Session) -> int:
    """进程启动时把遗留的 `running` 改判 `interrupted`。

    为什么不在启动时直接 resume：启动阶段的数据库/网络/沙箱都还没就绪，
    在那时候跑模型调用会把"服务起不来"和"一轮跑失败"两件事混在一起。
    改判状态、让后台循环去接手，责任边界清楚。
    """
    result = db.execute(
        update(ChatTurnRun)
        .where(ChatTurnRun.status == "running")
        .values(status="interrupted", updated_at=now_utc())
    )
    db.commit()
    return int(result.rowcount or 0)


def finish_from_error(db: Session, run: ChatTurnRun, *, reason: str) -> None:
    """一轮因异常/取消而中断：落 `interrupted`，**不要**落 `failed`。

    ## 为什么这条分支不能直接 `failed`

    `failed` 的语义是"重试也没用，别再来"。而这里的绝大多数中断是
    **可恢复**的：客户端断开、进程重启、docker 抖一下、模型一次 5xx。
    把它们一律判死，等于把"自动接着做"关掉 —— 用户就还是只能自己打
    「继续」，也就是这次改造要消灭的那个体验。

    真正判死（`give_up`）只发生在后台接手次数用尽时。
    """
    run.status = "interrupted"
    run.last_error = redact(reason)
    run.heartbeat_at = now_utc()
    db.flush()


def give_up(db: Session, run: ChatTurnRun, *, reason: str) -> None:
    """重试额度用尽：落 `failed`，把原因写进去。"""
    run.status = "failed"
    run.last_error = redact(reason)
    run.heartbeat_at = now_utc()
    db.flush()
