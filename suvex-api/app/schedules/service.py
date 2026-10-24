"""定时扫描：到点建扫描、推进下次时间、修改变更计划。

## 一条计划的生命周期

```
新建（enabled, next_run_at = now + interval）
   │
   ├─ tick 发现 next_run_at <= now
   │     ├─ 建出一条 queued 扫描（参数取自计划快照）
   │     └─ next_run_at += interval × n   ← 推进
   │
   └─ 用户停用 → enabled=false, next_run_at=NULL
```

## ★ 三个必须想清楚的地方

### 1. 服务停机期间错过的那些轮次怎么办

如果计划是「每 15 分钟」，而服务停了 6 小时，那么下一次启动时
`next_run_at` 落后了 24 个周期。

**不补跑**（不做 24 次 catch-up）：这个功能是「定期复查」，
攒了 24 条连在一起的扫描除了把并发打满、让目标方看到一个突发流量，
没有任何安全价值 —— 而突发流量本身就是一次事故。

**也不跳过**（把下次直接设成 now + interval 而不管已经过了多少）：
那样「每周一 3 点」的计划在服务重启后会永久漂到重启时刻。

采取的做法：`next_run_at = now + interval × ceil(落后周期数)` 的**反向**写法 ——
即把 `next_run_at` 从它的旧值按整数倍 `interval` 一路推到「第一个未来时刻」。
这样计划在时间轴上始终落在 `基准 + n×interval` 的那些点上（不漂移），
而错过的轮次只跑**一次**（因为第一次推进后它就落在未来，不会重复触发）。

代价如实记：如果一次停机跨过了好几个周期，中间那几次就是真的没跑。

### 2. 建扫描失败怎么办

`create_scan` 可能因为 provider 被删、目标不合法而抛错。这时：

* **不推进** `next_run_at` 之外的状态 —— 事实上仍然推进，否则一个
  持续失败的计划会每 2 秒重试一次，把日志刷爆；
* 记 `failure_count += 1` 与原因，**但不自动停用**。
  静默停用一个用户以为在跑的计划，比"一直在失败"危险得多 ——
  后者至少能在界面上看见。

### 3. 为什么推进 `next_run_at` 必须在**建扫描之前**

先建扫描再推进，中间如果抛异常（commit 失败、进程被杀），
下一次 tick 会看到同一个 `next_run_at` 再建一条 —— 那是**重复扫描**。
反过来先推进（并在同一个事务里提交）再建扫描，最坏情况是
"这一次没扫但下次照常" —— 少扫一次，而不是多扫一次。

多扫一次是有后果的：它真的对目标发包。
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta

from sqlalchemy import func, select, text

from app.core.ids import new_id
from app.core.timeutil import now_utc
from app.schedules.models import MAX_INTERVAL_MINUTES, MIN_INTERVAL_MINUTES, ScanSchedule

logger = logging.getLogger("suvex.schedules")

#: 一次 tick 最多建几条扫描。
#:
#: 防止"一堆计划同时到点"把整个队列瞬间灌满。剩下的下一轮继续 ——
#: 它们仍在 `next_run_at <= now` 的状态，不会丢。
MAX_DISPATCH_PER_TICK = 10

#: 认领计划时用的 PG 咨询锁 key。与调度器的 `_CLAIM_LOCK_KEY` 不同值 ——
#: 两个锁保护的是不同的事，共用 key 会让它们互相阻塞。
_DISPATCH_LOCK_KEY = 0x5A17_0002


class ScheduleError(Exception):
    """业务规则不满足（转成 400/409）。"""


def _advance(past: datetime, interval: timedelta, *, moment: datetime) -> datetime:
    """把 `next_run_at` 从 `past` 按整数倍 `interval` 推到第一个**未来**时刻。

    见模块 docstring §1：不补跑错过的轮次，但也不让计划在时间轴上漂移。

    ⚠️ 用 `math.floor` 算"还差几个完整周期"而不是 while 循环：
    一次跨年停机（`interval=5min`，停了 1 年）是一个约 10 万次的循环，
    而它在**调度线程**里跑 —— 那会让整个调度器卡住几百毫秒。
    """
    if interval.total_seconds() <= 0:  # pragma: no cover - 由 CHECK 约束挡住
        return moment + timedelta(minutes=MIN_INTERVAL_MINUTES)

    behind = (moment - past).total_seconds()
    if behind < 0:
        # 还没到点。理论上不会走到这里（调用方只处理 due 的），
        # 但保留这个分支：负数喂给 ceil 会得到 0，然后 next == past，
        # 于是下一轮立刻又 due —— 那是一个死循环。
        return past

    steps = math.floor(behind / interval.total_seconds()) + 1
    return past + interval * steps


def next_run_after(start: datetime, interval_minutes: int) -> datetime:
    """新建计划时的第一次到点时刻。

    刻意**不**设成 `start`：建完计划立刻就跑一次，用户会以为
    "我点保存它就扫了"。他要的是"从现在起每 N 分钟一次"，
    第一次应该在一个周期之后。
    """
    return start + timedelta(minutes=interval_minutes)


def validate_interval(minutes: int) -> None:
    """间隔的上下界。

    ⚠️ 这里**不重复写**上下界的数值，而是从 `models` 取 ——
    数据库 CHECK 约束与这个校验必须一致，而它们不可能不一致：
    只有一个地方定义了这两个常数。
    """
    if not isinstance(minutes, int) or isinstance(minutes, bool):
        raise ScheduleError("间隔必须是整数分钟数")
    if minutes < MIN_INTERVAL_MINUTES:
        raise ScheduleError(f"间隔不能少于 {MIN_INTERVAL_MINUTES} 分钟")
    if minutes > MAX_INTERVAL_MINUTES:
        raise ScheduleError(f"间隔不能超过 {MAX_INTERVAL_MINUTES // (24 * 60)} 天")


# ══════════════════════════════════════════════════════════
# 读
# ══════════════════════════════════════════════════════════


def list_schedules(db, *, enabled: bool | None = None) -> list[ScanSchedule]:  # noqa: ANN001
    """全部计划。

    排序在 SQL 里：按 `next_run_at IS NULL`（停用的排后面）→ `next_run_at`。
    停用的计划没有 `next_run_at`，如果直接 `ORDER BY next_run_at`，
    NULL 在 PostgreSQL 上默认排在**最后**（`NULLS LAST` 是 ASC 的默认），
    这恰好是想要的效果 —— 但不依赖它更好：显式写 `nulls_last()`，
    换数据库方言时不会静默变成另一种顺序。
    """
    stmt = select(ScanSchedule)
    if enabled is not None:
        stmt = stmt.where(ScanSchedule.enabled.is_(enabled))
    stmt = stmt.order_by(
        ScanSchedule.next_run_at.asc().nulls_last(),
        ScanSchedule.created_at.asc(),
        ScanSchedule.id.asc(),
    )
    return list(db.execute(stmt).scalars())


def get_schedule(db, schedule_id: str) -> ScanSchedule:  # noqa: ANN001
    schedule = db.get(ScanSchedule, schedule_id)
    if schedule is None:
        raise ScheduleError("这条定时计划不存在（可能已被删除）")
    return schedule


def count_due(db) -> int:  # noqa: ANN001
    """还有多少条到点没跑（给界面的"积压"提示用）。"""
    return int(
        db.execute(
            select(func.count())
            .select_from(ScanSchedule)
            .where(ScanSchedule.enabled.is_(True))
            .where(ScanSchedule.next_run_at.is_not(None))
            .where(ScanSchedule.next_run_at <= now_utc())
        ).scalar_one()
    )


# ══════════════════════════════════════════════════════════
# 写
# ══════════════════════════════════════════════════════════


def create_schedule(
    db,  # noqa: ANN001
    *,
    name: str,
    target: str,
    target_kind: str,
    goal: str,
    interval_minutes: int,
    provider_id: str,
    provider_label: str | None,
    skills: list[str],
    brief: str | None,
    authorized_by: str | None,
    authorization_statement: str | None,
    authorization_confirmed_at: datetime | None,
    now: datetime | None = None,
) -> ScanSchedule:
    """新建一条计划。**目标必须已经由调用方归一化**（`targets.normalize_*`）。

    这里不归一化：调用方（router）已经走了一遍 `create_scan` 期间那套
    校验，而再做一次意味着两处可能对"什么叫同一个目标"给出不同答案。
    """
    validate_interval(interval_minutes)
    moment = now or now_utc()

    schedule = ScanSchedule(
        id=new_id("sched"),
        name=name.strip()[:200],
        target=target,
        target_kind=target_kind,
        goal=goal,
        interval_minutes=interval_minutes,
        enabled=True,
        next_run_at=next_run_after(moment, interval_minutes),
        last_scan_id=None,
        last_run_at=None,
        failure_count=0,
        last_failure_reason=None,
        provider_id=provider_id,
        provider_label=provider_label,
        skills=list(skills),
        brief=(brief or "").strip() or None,
        authorized_by=authorized_by,
        authorization_statement=authorization_statement,
        authorization_confirmed_at=authorization_confirmed_at,
        created_at=moment,
        updated_at=moment,
    )
    db.add(schedule)
    db.commit()
    db.refresh(schedule)
    logger.info("已创建定时计划 %s（目标 %s，每 %d 分钟）", schedule.id, schedule.target, interval_minutes)
    return schedule


def set_enabled(db, schedule_id: str, enabled: bool, *, now: datetime | None = None) -> ScanSchedule:  # noqa: ANN001
    """启用/停用。

    ## 重新启用时从**现在**起算，不用旧的 `next_run_at`

    一条停了三个月的计划，它的 `next_run_at` 还在三个月前 ——
    原样恢复会让它在启用后**立刻**扫一次（`next_run_at <= now`）。
    用户点的是「启用」，不是「现在跑一次」。

    但也不该无视它原来的相位：如果用户是在"每 15 分钟"的节奏上停的，
    启用后也该落在那条 15 分钟的网格上。所以用 `_advance()` 从旧值
    推到未来 —— 相位保住，错过的不补。
    """
    schedule = get_schedule(db, schedule_id)
    moment = now or now_utc()

    if enabled:
        if schedule.enabled and schedule.next_run_at is not None:
            # 已经是启用的，且下次时间是未来 —— 不动它。
            # ⚠️ 这条分支不能让"点两次启用"把下次时间越推越远。
            return schedule
        base = schedule.next_run_at or schedule.last_run_at or moment
        schedule.next_run_at = _advance(base, timedelta(minutes=schedule.interval_minutes), moment=moment)
        schedule.enabled = True
    else:
        schedule.enabled = False
        # CHECK 约束要求停用必须没有下次时间。
        schedule.next_run_at = None

    schedule.updated_at = moment
    db.commit()
    db.refresh(schedule)
    logger.info("定时计划 %s 已%s", schedule.id, "启用" if enabled else "停用")
    return schedule


def update_schedule(
    db,  # noqa: ANN001
    schedule_id: str,
    *,
    name: str | None = None,
    interval_minutes: int | None = None,
    now: datetime | None = None,
) -> ScanSchedule:
    """改名字 / 改间隔。

    ⚠️ **刻意不允许改目标、provider、skills、brief** —— 那些是
    "这条计划在建什么扫描"的定义。允许改它们等于「静默地把一个
    已授权的东西换成另一个」，而授权留痕是绑定在**建计划那一刻**
    的参数上的（见 `models.py` 的说明）。要换目标就建一条新计划，
    旧的那条停用 —— 审计记录能分清这是两件事。
    """
    schedule = get_schedule(db, schedule_id)
    moment = now or now_utc()

    if name is not None and name.strip():
        schedule.name = name.strip()[:200]

    if interval_minutes is not None and interval_minutes != schedule.interval_minutes:
        validate_interval(interval_minutes)
        schedule.interval_minutes = interval_minutes
        # 改间隔之后，下次时间按**新间隔**从上一个已跑时刻重算 ——
        # 否则「改成每 5 分钟」要等旧的那个 1 小时周期走完才生效，
        # 而界面上的下次时间会显示一个与间隔无关的数。
        base = schedule.last_run_at or moment
        schedule.next_run_at = _advance(base, timedelta(minutes=interval_minutes), moment=moment)

    schedule.updated_at = moment
    db.commit()
    db.refresh(schedule)
    return schedule


def delete_schedule(db, schedule_id: str) -> None:  # noqa: ANN001
    schedule = get_schedule(db, schedule_id)
    db.delete(schedule)
    db.commit()
    logger.info("定时计划 %s 已删除", schedule_id)


# ══════════════════════════════════════════════════════════
# 调度（tick 调用）
# ══════════════════════════════════════════════════════════


def _lock_dispatch(session) -> None:  # noqa: ANN001
    """多 worker 时串行化「认领到期的计划」。

    与调度器的 `_lock_claiming` 同一个理由：两个 worker 各自看到
    同一条 due 的计划，就会为它建出**两条扫描**。
    `FOR UPDATE SKIP LOCKED` 能挡住同一行被重复认领，但挡住的是
    "同时"；提交时长不同时仍可能两个都读到。咨询锁把这一段彻底串起来。
    """
    if session.get_bind().dialect.name != "postgresql":
        return
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _DISPATCH_LOCK_KEY})


def claim_due(db, *, now: datetime | None = None, limit: int = MAX_DISPATCH_PER_TICK):  # noqa: ANN001
    """认领到期的计划并**推进它们的下次时间**。返回被认领的计划（已 detached 复制）。

    ## ★ 为什么这个函数只推进时间、不建扫描

    推进时间必须与"认领"在**同一个事务**里，而建扫描要走
    `scans.service.create_scan`（它会 commit，还会做可达性预检、建目录）。
    把两件事放进一个事务会让持锁窗口跨越网络探测 —— 见
    `scheduler._reap_finished` 里那次 `DeadlockDetected` 的教训：
    事务里不做 I/O。

    所以：第一步（本函数）在一个短事务里把 due 的计划"认领 + 推进"，
    第二步（调用方）拿着快照去建扫描。中间崩了最多少扫一次。
    """
    moment = now or now_utc()

    due = list(
        db.execute(
            select(ScanSchedule)
            .where(ScanSchedule.enabled.is_(True))
            .where(ScanSchedule.next_run_at.is_not(None))
            .where(ScanSchedule.next_run_at <= moment)
            .order_by(ScanSchedule.next_run_at.asc(), ScanSchedule.id.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).scalars()
    )
    if not due:
        return []

    claimed: list[ScanSchedule] = []
    for schedule in due:
        old_next = schedule.next_run_at
        assert old_next is not None  # noqa: S101 - 上面的 WHERE 保证
        schedule.next_run_at = _advance(old_next, timedelta(minutes=schedule.interval_minutes), moment=moment)
        schedule.updated_at = moment
        claimed.append(schedule)

    db.commit()
    # commit 之后对象会被 expire；取一份快照再 detach，避免调用方
    # 拿着一个"一碰就再查库"的对象跨事务使用。
    snapshots = [_snapshot(s) for s in claimed]
    for schedule in claimed:
        db.expunge(schedule)
    return snapshots


def _snapshot(schedule: ScanSchedule) -> ScanSchedule:
    """一份 detach 用的浅拷贝（只复制读取方会用到的字段）。

    ⚠️ 不返回 ORM 对象本身：`claim_due` 已经 commit，对象处于 expired
    状态，任何属性访问都会**重新开一个事务**。而调用方要用它建扫描 ——
    那又是一次 I/O 密集操作。两个事务交叠是死锁的温床。
    """
    return ScanSchedule(
        id=schedule.id,
        name=schedule.name,
        target=schedule.target,
        target_kind=schedule.target_kind,
        goal=schedule.goal,
        interval_minutes=schedule.interval_minutes,
        enabled=schedule.enabled,
        next_run_at=schedule.next_run_at,
        last_scan_id=schedule.last_scan_id,
        last_run_at=schedule.last_run_at,
        failure_count=schedule.failure_count,
        last_failure_reason=schedule.last_failure_reason,
        provider_id=schedule.provider_id,
        provider_label=schedule.provider_label,
        skills=list(schedule.skills or []),
        brief=schedule.brief,
        authorized_by=schedule.authorized_by,
        authorization_statement=schedule.authorization_statement,
        authorization_confirmed_at=schedule.authorization_confirmed_at,
    )


def record_dispatch(db, schedule_id: str, *, scan_id: str, now: datetime | None = None) -> None:  # noqa: ANN001
    """记下"这一轮建出了哪条扫描"，并清掉失败计数。"""
    schedule = db.get(ScanSchedule, schedule_id)
    if schedule is None:
        return
    moment = now or now_utc()
    schedule.last_scan_id = scan_id
    schedule.last_run_at = moment
    schedule.failure_count = 0
    schedule.last_failure_reason = None
    schedule.updated_at = moment
    db.commit()


def record_failure(db, schedule_id: str, reason: str, *, now: datetime | None = None) -> None:  # noqa: ANN001
    """记一次"建扫描失败"。

    ⚠️ **不停用计划**（见模块 docstring §2）。`next_run_at` 已经在上一步
    推进过了，所以不会每 2 秒重试一次 —— 它会在下一个正常周期再试。
    """
    schedule = db.get(ScanSchedule, schedule_id)
    if schedule is None:
        return
    moment = now or now_utc()
    schedule.failure_count = (schedule.failure_count or 0) + 1
    # 截断：失败原因可能是一大段 SQL 报错，而界面只显示一行。
    schedule.last_failure_reason = (reason or "")[:2000]
    schedule.updated_at = moment
    db.commit()
    logger.warning("定时计划 %s 建扫描失败（连续第 %d 次）：%s", schedule_id, schedule.failure_count, reason)
