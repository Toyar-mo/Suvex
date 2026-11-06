"""一轮对话的执行状态：持久化 + 谁来接手（v1.19）。

见 `app.chat.models.ChatTurnRun` 的类注释说明为什么需要这张表。这里只放
"怎么读写它"和"哪些该被接手"的判据。

## 两条互不重叠的判据

1. **该不该被接手**（`is_resumable`）—— 只看状态、心跳、重试次数，
   外加本进程的活跃租约 `is_active()`。后者的意义：心跳是 90 秒粒度，
   慢工具期间会显得过期，但本进程里那一轮其实还在跑；不跳过它，界面
   会把「正在跑」标成「可继续」。这套判据的失败模式是静默的（该接手
   没接手 = 界面卡住；不该接手却接手 = 两轮并发写同一个会话），只能
   靠测试逐条钉住。
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

#: ★ v1.67：**硬过期**阈值（秒）。心跳超过这么久没刷新，即便这一轮 id 还挂在
#:   本进程的 `_ACTIVE` 里，也认定那只是一具僵尸、允许后台接手。
#:
#: ## 为什么需要它（线上卡死事故）
#:
#: 会话 `ctf` 与"加一个 MCP 工具连接"两个轮次 `status=running` 但心跳冻了
#: 49~53 分钟 —— 它们卡在 `_call_model` 的模型流上（上游只发 keepalive 不吐
#: 正文），卡死协程一直占着 `_ACTIVE` 租约。而 `STALE_SECONDS=90` 那条安全网
#: 因为 `is_active()` 恒 True 而永远跳过它们 —— 自己把自己锁死了。
#:
#: `STALE_SECONDS`(90s) 的语义是"慢工具期间别抢"；本阈值(900s) 的语义是
#: "根本没有合法单步会 15 分钟不心跳，那就不是慢、是死了"。一回合最长 =
#: 一次模型调用 + 一次工具（`DEFAULT_SHELL_TIMEOUT=300`），远小于 900s。所以
#: 超过 900s 必是僵尸，回收它不会造成"合法两轮并发"。
#:
#: 真正的止血在 engine 的 `STREAM_IDLE_SECONDS`（v1.67 同批）——本阈值是
#: 纵深防御：即便某个 await 点没加超时，僵尸租约也会在 900s 后被回收。
HARD_STALE_SECONDS = 900

#: 一轮最多被自动接手几次。到上限就放弃并把状态改成 `failed`。
#:
#: 为什么必须有上限：某些失败是**确定性的**（例如提示词让模型每次都写出
#: 超长回复而被截断），没有上限就是一个死循环，每 90 秒烧一次模型调用，
#: 而用户在界面上只会看到同一条错误反复出现。
MAX_RESUME_ATTEMPTS = 3

#: 还没结束的状态。`done` / `failed` 都不在其中。
OPEN_STATUSES = ("running", "interrupted")

#: 本进程里正在跑的轮次 id（v1.33）。
#:
#: 心跳是跨进程的唯一判据，但它有 90 秒的粒度：一次合法但很慢的工具调用
#: （例如本地跑 exploit）期间心跳不会刷新，后台扫描就会把**本进程里还活着**
#: 的一轮当成死行抢走，造成同一轮被两个执行体各写一条结论。
#:
#: 这个集合只做同一进程内的补充判据：`stale_runs()` 和 `claim()` 都会跳过
#: 它。跨进程/重启后的接手仍然完全依赖数据库里的心跳。
_ACTIVE: set[str] = set()


def mark_active(run_id: str) -> None:
    """标记这一轮在本进程里正在跑。"""
    _ACTIVE.add(run_id)


def unmark_active(run_id: str) -> None:
    """这一轮在本进程里跑完了（成功、失败或取消都要调用）。"""
    _ACTIVE.discard(run_id)


def is_active(run_id: str) -> bool:
    """这一轮是否正在本进程里跑。"""
    return run_id in _ACTIVE


def _is_hard_stale(
    run: ChatTurnRun, *, now: datetime, hard_seconds: int = HARD_STALE_SECONDS
) -> bool:
    """心跳是否久到"必是僵尸"——超过硬阈值就觉得它即便挂着 `_ACTIVE` 也可回收。

    见 `HARD_STALE_SECONDS` 的长注释：这是给"卡在某个没有超时的 await 上、
    永远不返回、却仍占着进程内租约"的僵尸轮次留的最后一扇门。
    """
    return run.heartbeat_at <= now - timedelta(seconds=hard_seconds)


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


def running_session_ids(db: Session, *, session_ids: list[str]) -> set[str]:
    """这一批会话里，哪些**此刻**有一轮在跑（`status='running'`）。

    ★ v1.66：侧栏要在每个条目上画「运行中」转圈点。判据必须是**服务端**
      事实（`chat_turn_runs.status == 'running'`），不能用前端本地 SSE 布尔
      —— 刷新页面/换个浏览器后本地不知道别的会话在跑，而服务端知道。

    只认 `running`，不认 `interrupted`：后者是"被中断、等着后台接手"的
    中间态，多数会在几秒内变回 `running`（那时再亮），也可能因为额度用尽
    永久停在 `interrupted`（那时不该显示在跑）。只认 `running` 最诚实。

    入参 `session_ids` 由调用方先按归属过滤好（列表接口只会传当前用户
    自己的会话 id），这里不再重复鉴权 —— 但**只查这些 id**，避免把别人
    会话的运行态泄漏出来。
    """
    if not session_ids:
        return set()
    stmt = (
        select(ChatTurnRun.session_id)
        .where(
            ChatTurnRun.session_id.in_(session_ids),
            ChatTurnRun.status == "running",
        )
        .distinct()
    )
    return set(db.scalars(stmt))


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
    """这一轮该不该被后台接手。

    判据本身只看持久化状态；唯一一处进程内信息是 `is_active()`：
    本进程明明还在跑这一轮时，前端不能把它标成「可继续」，否则用户
    点黄条会得到一个必然失败的 422（v1.33.1）。跨进程仍靠心跳 + `claim`
    的条件 UPDATE 兜底。
    """
    if run.status not in OPEN_STATUSES:
        return False
    if run.resume_attempts >= max_attempts:
        return False
    # ★ v1.67：本进程在跑的一轮通常不能接手（慢工具期间心跳会显得过期）。
    #   但若心跳已硬过期（僵尸卡死），仍然可接手 —— 否则它永远占着租约、
    #   永远没人救。
    if is_active(run.id) and not _is_hard_stale(run, now=now):
        return False
    return run.heartbeat_at <= now - timedelta(seconds=stale_seconds)


def is_blocking_new_turn(
    run: ChatTurnRun,
    *,
    now: datetime,
    stale_seconds: int = STALE_SECONDS,
    hard_seconds: int = HARD_STALE_SECONDS,
    max_attempts: int = MAX_RESUME_ATTEMPTS,
) -> bool:
    """「发新消息」时，这条最近的开着的轮次是否该把会话**堵住**（TURN_ALREADY_RUNNING）。

    ## 为什么需要这个判据（v1.68.4）

    实测（2026-11-05 用户截图）：一条 CTF 轮跑到墙钟 60 分钟上限停下 → 落
    `interrupted`；后台 resumer 接手 3 次仍失败（每次都撞上限）→ 停在
    `interrupted` 但 `resume_attempts` 已达上限。此后用户发「继续」，被
    `TURN_ALREADY_RUNNING` **永远**挡住：它既不在跑、又接不了手（resume 按钮
    也没有），可它就是 `interrupted`（在 OPEN_STATUSES 里）→ 会话彻底卡死，
    用户既不能发消息也不能继续。

    正确语义：只有**真正在跑**（running 且未僵尸）的一轮才该挡住新消息。
      * `running` 且未硬过期 → 挡住（真在跑，别并发写）。
      * `running` 但已硬过期（僵尸）→ 不挡（让新消息推进，旧僵尸由 resumer/claim 回收）。
      * `interrupted` → **一律不挡**：它本来就没在跑，只是"没答完、等着接手"。
        用户发消息时由调用方决定是把它判死（resume 额度已尽）还是别的处理。
    """
    if run.status != "running":
        return False
    return not _is_hard_stale(run, now=now, hard_seconds=hard_seconds)


def is_abandoned_open(
    run: ChatTurnRun,
    *,
    now: datetime,
    max_attempts: int = MAX_RESUME_ATTEMPTS,
) -> bool:
    """一条仍开着的轮次是否已「死了」：`interrupted` 且重试额度用尽。

    这种轮次永远不会再被 resumer 接手（`stale_runs` 按 `resume_attempts < max`
    过滤），却又卡在 OPEN_STATUSES 里，会一直占着「还有一轮在跑」的位置。调用方
    应当把它判成 `failed`（见 `give_up`）后放行新消息。
    """
    return run.status == "interrupted" and run.resume_attempts >= max_attempts



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
    )
    # ★ v1.33.1：先过滤再截断。SQL 的 LIMIT 放在过滤前，活跃轮次一多
    #   就会把真正过期的行挤出窗口。
    # ★ v1.67：本进程活跃的轮次默认跳过（慢工具期间心跳会显得过期）；但心跳
    #   硬过期（僵尸卡死 > HARD_STALE_SECONDS）时仍收进来 —— 见 `is_active` 注释。
    return [
        run
        for run in db.scalars(stmt)
        if (not is_active(run.id)) or _is_hard_stale(run, now=now)
    ][:limit]


def claim(
    db: Session,
    run: ChatTurnRun,
    *,
    now: datetime,
    max_attempts: int = MAX_RESUME_ATTEMPTS,
) -> bool:
    """抢占这一轮：条件 UPDATE，返回是否抢到。

    条件里重复检查"还是未结束状态 + 额度未用尽 + 心跳确实过期"，因为从
    `stale_runs()` 到真正开跑之间有时间差，而这段时间里原来的 worker 可能
    刚好活过来继续写了进度。抢不到就跳过，绝不要带着过期的状态去跑。

    同一进程内还额外看 `is_active()`：心跳是 90 秒粒度，慢工具期间会显得
    过期，但那一轮其实还在跑。★ v1.67：例外是心跳硬过期（僵尸卡死）——
    此时放行，否则僵尸会永远占着租约。
    """
    if is_active(run.id) and not _is_hard_stale(run, now=now):
        return False
    result = db.execute(
        update(ChatTurnRun)
        .where(
            ChatTurnRun.id == run.id,
            ChatTurnRun.status.in_(OPEN_STATUSES),
            ChatTurnRun.resume_attempts < max_attempts,
            ChatTurnRun.heartbeat_at <= now - timedelta(seconds=STALE_SECONDS),
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


#: 一轮停在计划句上时的记账原因（v1.35）。
#:
#: 它同时是 `turn_run.last_error` 的内容与"后台接手时知道为什么又跑起来了"的
#: 唯一线索 —— 与真实异常不同，这里没有栈，只有一句事实。
NEEDS_CONTINUE_REASON = "本轮停在下一步计划上，尚未给出可交付结论"


def finish_incomplete(db: Session, run: ChatTurnRun, *, reason: str = NEEDS_CONTINUE_REASON) -> None:
    """一轮**停在计划句上**（v1.35）：落 `interrupted`，不能落 `done`。

    ## 为什么需要单独的入口

    `finish()` 只接受 done/failed/cancelled（见它的 docstring）：这三种状态的
    语义是"这一问有结果了"。而"模型说完"接下来写 exploit。"就停了"既不是
    成功、也不是失败、更不是用户取消 —— 它的正确语义是**没答完，可以接着做**，
    也就是 `interrupted`（与客户端断开、进程重启同一类，见 `finish_from_error`）。

    不直接用 `finish_from_error` 是为了让调用点读起来是意图（"没答完"）而
    不是异常路径；两者共享同一个终态与脱敏口径。
    """
    finish_from_error(db, run, reason=reason)


def give_up(db: Session, run: ChatTurnRun, *, reason: str) -> None:
    """重试额度用尽：落 `failed`，把原因写进去。"""
    run.status = "failed"
    run.last_error = redact(reason)
    run.heartbeat_at = now_utc()
    db.flush()
