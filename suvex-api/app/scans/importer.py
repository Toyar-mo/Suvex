"""把 strix 的事件导入本地库（任务 4 要求 ③：先落库，再谈实时推送）。

## 两条硬规矩

**1. 落库优先于推送。**
`activity_events` 是唯一事实来源，SSE 只是它的一个视图。
反过来的话（先推再存）会出现"用户刷新一次，刚看到的那段就没了"——
而任务 4 的原话是「不是"错过了就没了"」。

**2. 幂等。**
导入器会被反复调用（扫描还在跑，每几秒读一次外部库）。
靠 `(scan_id, source_row_id)` 的部分唯一索引去重：
重复导入同一段只会命中冲突，不会产生第二条。

## 游标从哪来

`SELECT max(source_row_id) WHERE scan_id = ?` —— **不从内存里记**。
内存游标在服务重启后就丢了，而"重启后从头重读整个库"意味着
每次重启都要重新解析几十万行；更糟的是，如果去重逻辑有洞，
重复事件就会真的落库。数据库里的游标天然跨重启。

## 为什么不 tail `strix.log`

它是**纯文本**（`telemetry/logging.py` 的 `_FORMAT`：
`时间 级别 scan_id agent_id logger: 消息`），没有结构。
从文本里正则抠"这是一次工具调用"必然脆 —— 而结构化数据就在
`agents.db` 里躺着，strix 自己的 TUI 与 Web viewer 都读它。
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session

from app.core.ids import new_id
from app.core.text import strip_nul, strip_nul_deep
from app.core.timeutil import now_utc
from app.core.vocab import AgentRole, AgentStatus
from app.events.models import ActivityEvent, Agent
from app.scans import strix_events
from app.scans.models import Scan

logger = logging.getLogger("suvex.strix")

# 单次导入的事件上限。一次读太多会长时间占着数据库连接，
# 而扫描还在写、下一轮马上就能读到剩下的。
BATCH_LIMIT = 500


def import_events(db: Session, scan: Scan, *, limit: int = BATCH_LIMIT) -> int:
    """把这次扫描的外部事件导入本地库。返回新写入的条数。

    可以在扫描运行中反复调用（调度器的每一轮都会调），
    也可以在扫描结束后调一次做补齐。
    """
    if not scan.run_dir:
        return 0
    run_dir = Path(scan.run_dir)
    if not run_dir.is_dir():
        return 0

    cursor = db.execute(
        select(func.coalesce(func.max(ActivityEvent.source_row_id), 0)).where(
            ActivityEvent.scan_id == scan.id
        )
    ).scalar_one()

    rows, _error = strix_events.read_events(run_dir, after_row_id=int(cursor))
    if not rows:
        return 0

    # 名册一次读出来，给每个 agent 定角色与状态。它比事件库小得多，
    # 每轮重读一次也不贵；而且 agent 的状态是会变的（running → done），
    # 缓存它反而会显示过期的状态。
    roster = strix_events.read_agents_roster(run_dir)

    translated = strix_events.translate(rows[:limit])
    written = 0
    for event in translated:
        if _persist(db, scan, event, roster):
            written += 1

    if written:
        db.commit()
        logger.info("扫描 %s 导入了 %d 条外部事件", scan.id, written)
    return written


def _persist(db: Session, scan: Scan, event: strix_events.StrixEvent, roster: dict) -> bool:
    """写一条。返回是否真的写了（重复的不算）。

    ## 为什么用 SAVEPOINT 而不是裸 flush

    裸 flush 撞上错误时只能整批 `rollback()` —— 那会把本批里**已经排好队
    但还没提交**的正常事件一起丢掉。实测（2026-09-27）的后果是：一条含
    NUL 字节的事件（agent 抓到了 PNG 二进制）让某次扫描的 **100 条事件
    永远导不进来**（游标不前进），并且因为事件导入排在漏洞导入**之前**，
    连那条漏洞也一起没了。界面上只表现为"这次没扫到东西"。

    SAVEPOINT 把"坏的那一条"与"这一批"隔开：只回滚它自己，其余照常入
    库，游标照常前进。这样任何单条坏数据最多损失它自己。
    """
    agent = _ensure_agent(db, scan, event, roster)
    try:
        with db.begin_nested():
            db.add(
                ActivityEvent(
                    id=new_id("evt"),
                    scan_id=scan.id,
                    agent_id=agent.id,
                    agent_phase=event.agent_phase,
                    type=event.type,
                    # ★ 清洗见 `app.core.text`：agent 抓 HTTP 响应时会把二进制
                    #   （PNG 头部的 \x00）记进 detail，而 PostgreSQL 的
                    #   text/jsonb 存不了 NUL 字节，插进去是一条硬错误。
                    summary=strip_nul(event.summary),
                    detail=strip_nul_deep(event.detail),
                    at=event.at,
                    source_row_id=event.source_row_id,
                )
            )
            # 立刻 flush 而不是攒到 commit —— 靠**数据库**的唯一索引判重，
            # 而不是靠"先查一下有没有"。
            # 先查后写在并发下是有洞的（两个导入器同时查到"没有"），
            # 而唯一索引是原子的。撞了就是一个 IntegrityError，跳过即可。
            db.flush()
        return True
    except IntegrityError:
        return False
    except DataError:
        # 兜底：清洗没盖住的坏值也不该让整条事件流断在这里。
        # 记 warning 而不是 debug —— 这次排查时它正是"什么都看不见"的原因。
        logger.warning(
            "扫描 %s 的 1 条外部事件被数据库拒绝（source_row_id=%s），已跳过该条",
            scan.id,
            event.source_row_id,
            exc_info=True,
        )
        return False


def _ensure_agent(db: Session, scan: Scan, event: strix_events.StrixEvent, roster: dict) -> Agent:
    """找到（或建出）这条事件所属的 agent。

    本地的 `Agent.id` 与 strix 的 agent id 不同（长度、字符集都不保证），
    所以映射是**稳定哈希**：`stable_agent_id()`。同一次扫描里反复导入
    必须命中同一行，否则名册会被拆成好几份。
    """
    local_id = strix_events.stable_agent_id(event.agent_ref)
    agent = db.get(Agent, local_id)
    if agent is not None:
        # 状态会变（running → done），所以每轮都跟着外部名册刷新。
        info = roster.get(event.agent_ref)
        if info:
            agent.status = strix_events.status_for(info.get("status", ""))
            if info.get("error"):
                agent.status = AgentStatus.FAILED.value
        agent.event_count = int(agent.event_count or 0) + 1
        agent.last_active_at = max(agent.last_active_at, event.at)
        return agent

    info = roster.get(event.agent_ref) or {}
    name = str(info.get("name") or event.agent_ref)
    agent = Agent(
        id=local_id,
        scan_id=scan.id,
        # ⚠️ 角色是**启发式**推断的（见 strix_events 的说明），
        # 因为它只影响名册上的中文标签，猜错的代价是可接受的；
        # 而 `status` 来自权威的 `agents.json`。
        role=strix_events.role_for_name(name),
        status=strix_events.status_for(info.get("status", "")) if info else AgentStatus.RUNNING.value,
        target=scan.target,
        event_count=1,
        last_active_at=event.at,
    )
    db.add(agent)
    db.flush()
    return agent


def backfill_after_finish(db: Session, scan: Scan) -> int:
    """扫描结束后做一次补齐。

    为什么需要：调度器是在**进程退出之后**收尾的，而外部工具在退出前
    可能刚写完最后几行；那时最后一轮导入可能还没跑到。
    不收尾就永远差那几条 —— 而"历史扫描能完整回放"正是任务 4 的验收标准之一。

    循环读到没有为止（而不是只读一批）：扫描已经结束，库不会再长了，
    所以这里可以放心读干净。上限是防御性的 —— 真的一次扫描产生十万条事件，
    逐批读比一次全拉进内存安全。
    """
    total = 0
    for _ in range(50):
        written = import_events(db, scan)
        total += written
        if written < BATCH_LIMIT:
            break
    return total


def ensure_lifecycle_agent(db: Session, scan: Scan) -> Agent:
    """拿到（或建出）承载"扫描生命周期"事件的那个 agent。

    与 `scans/service.ensure_runner_agent` 是同一件事，但那个函数在
    scans 域里、用的是 `reporter` 角色；这里给**外部工具**的 agent 用，
    两者是不同的 agent，所以不复用。
    """
    existing = db.execute(
        select(Agent).where(Agent.scan_id == scan.id, Agent.role == AgentRole.REPORTER.value)
    ).scalars().first()
    if existing is not None:
        return existing
    agent = Agent(
        id=new_id("agent"),
        scan_id=scan.id,
        role=AgentRole.REPORTER.value,
        status=AgentStatus.RUNNING.value,
        target=scan.target,
        event_count=0,
        last_active_at=now_utc(),
    )
    db.add(agent)
    db.flush()
    return agent
