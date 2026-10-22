"""CTF2 题目调度器：队列、并发上限、worker 监督、孤儿回收。

## 它对齐 `scans/scheduler.py` 的**配额概念**，但不是一个东西

对齐的部分（这些是概念，必须一致）：

| 概念 | scans | ctf2 |
| --- | --- | --- |
| 并发上限 | `max_concurrent_scans` | **同一个值**（见下） |
| 超限排队 | FIFO | FIFO |
| 认领加锁 | `pg_advisory_xact_lock` | 同一个 key 空间，不同 key |
| 孤儿回收 | 服务重启时清理 `running` | 服务重启时清理 `solving` |
| 一个 tick | 回收 → 算名额 → 认领 | 回收 → 算名额 → 认领 |

不一致的部分，以及为什么：

| | scans | ctf2 |
| --- | --- | --- |
| 工作单元 | 一个子进程（`Popen`） | 一个 **Python 线程 + 事件循环** |
| 怎么判活着 | `Popen.poll()`（句柄） | 线程 `is_alive()` |
| 怎么杀 | `terminate_tree()` | 线程无法被杀 —— 见下面的说明 |

## ★ 并发上限**故意复用** `max_concurrent_scans`

不给 CTF2 单独一个配置项，理由是两者抢占的是**同一批资源**：

* Docker（容器数量、CPU、内存、磁盘）；
* LLM 的速率与费用（同一个 provider、同一个 key）；
* 本进程的线程与内存。

各给一份配额等于把总资源**超卖一倍** —— 设成各 1 就真的同时跑 2 个，
而用户的意图显然是"这台机器一次跑一个"。复用同一个数字让"上限"
这个词保持它字面的意思。

代价如实记：一道题在跑的时候，一个渗透扫描会排队等它，反之亦然。
如果将来需要"CTF2 可以跑 3 个但扫描只能 1 个"，那要引入**分层配额**
（总配额 + 各领域上限），而不是在这里加一个独立数字。

## ⚠️ 线程杀不掉 —— 这个限制必须写在明处

`scans` 能 `terminate_tree()`，因为工作单元是一个**进程**。
而这里的工作单元是一个线程，Python **没有**安全的杀线程手段
（`PyThreadState_SetAsyncExc` 是未定义行为，且对正在 `await` 的
协程无效）。

所以 `stop()` 的语义是"**不再启动新的**，并等正在跑的自己结束"
（给一个宽限期），而不是"立刻全停"。超时之后留下的线程是 daemon，
会随进程消失 —— 而**沙箱容器不会**，
所以每个 worker 的 `finally` 里那个 `sandbox.destroy()` 是唯一的
清理路径（见 `runner._run_worker`）。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.db import get_session_factory, session_scope
from app.core.timeutil import now_utc
from app.core.vocab import (
    DEFAULT_CTF2_MAX_CONCURRENT_SOLVES,
    DEFAULT_CTF2_SOLVE_POLL_SECONDS,
    MAX_CTF2_SOLVE_POLL_SECONDS,
    MIN_CTF2_SOLVE_POLL_SECONDS,
    Ctf2ChallengeStatus,
)
from app.kernel import events
from app.kernel.models import Ctf2Challenge

logger = logging.getLogger("suvex.ctf2")

# 认领锁的 key。与 scans 的 `0x5A17_0001` 在同一个空间但**不同值** ——
# 它们串行化的是两件不同的事，共用一个 key 会让两边互相等（无害但没必要）。
_CLAIM_LOCK_KEY = 0x5A17_0002

# 一个 tick 最多认领几道题。防止"上限调大之后一次抢 50 个"把宿主打爆 ——
# 与 scans 同一条理由（它是安全阀，不是性能参数）。
_MAX_CLAIMS_PER_TICK = 4


def max_concurrent(db: Session | None = None) -> int:
    """当前生效的**解题并发配额**。

    ## ★★ v0.22：不再借用扫描那个上限（那是一处真实的超卖）

    原来这里读的是 `execution_settings.max_concurrent_scans`，理由是
    "两者抢同一批资源，各给一份配额等于超卖一倍"。

    **那个担心是对的，但做法是反的。** 两个调度器是**各自**数自己的
    名额、各自拿这同一个上限：

        scans/scheduler.py   COUNT(Scan WHERE status='running')  <= 3
        ctf2/scheduler.py     len(self._running)（内存线程数）      <= 3

    于是 `=3` 时真的能同时跑 **3 个扫描 + 3 道题 = 6 个** ——
    正好是那段注释说要避免的超卖，只是它发生在两个调度器**之间**，
    没有任何一处看得见。

    决赛的两个模式（融合靶场安全评估 + AI Agent 解题）在**同一场**
    同时进行，这条超卖会实打实地把单机打爆。

    正确的形状是**分层配额**：各领域守自己的子配额，
    **总并发 = 各子配额之和**。所以现在读的是 CTF2 自己的
    `ctf2_config.max_concurrent_solves`。

    ⚠️ `db is None` 时返回代码默认值（不查库）。
    调度器在没有会话的场合（比如启动日志）也要能问一句上限是多少。
    """
    if db is None:
        return DEFAULT_CTF2_MAX_CONCURRENT_SOLVES
    from app.kernel import service as ctf2_service

    settings = ctf2_service.effective_ctf2_settings(db)
    return max(1, int(settings.max_concurrent_solves))


def solve_poll_seconds(db: Session | None = None) -> float:
    """当前生效的**认领轮询间隔**（秒）。

    ## ★ v0.24：不再借用扫描那个旋钮

    原来 `_loop` 写的是 `max(1.0, get_settings().scan_poll_seconds)` ——
    那是**扫描**的设置。后果不是"少了个设置项"那么轻：用户在设置页把
    扫描轮询从 2 秒调到 10 秒（一个完全合理的省机器操作），CTF2 的认领
    延迟会**一起**变成 10 秒，而界面上没有任何地方提示这两件事连着。

    这与 v0.22 的 `max_concurrent_solves` 是同一类病：**两个独立的
    东西共用一个旋钮**（那次表现为超卖，这次表现为"改了 A 动了 B"）。

    ⚠️ `db is None` 时返回代码默认值（不查库），与 `max_concurrent`
       同一条理由：启动日志那种场合也要能问一句。
    """
    if db is None:
        return DEFAULT_CTF2_SOLVE_POLL_SECONDS
    from app.kernel import service as ctf2_service

    settings = ctf2_service.effective_ctf2_settings(db)
    return min(
        max(float(settings.solve_poll_seconds), MIN_CTF2_SOLVE_POLL_SECONDS),
        MAX_CTF2_SOLVE_POLL_SECONDS,
    )


def _lock_claiming(session: Session) -> None:
    """在多 worker 场景下串行化「数名额 + 认领」。

    与 `scans/scheduler.py` 的同名函数是同一套做法，理由也一样：
    单进程里 `threading.Lock` 就够了，这一条是为**将来加 worker**
    准备的 —— 两个进程各自数出"在跑 0 个"然后各启动一个，
    同一个上限就失效了。

    `pg_advisory_xact_lock` 在事务结束时自动释放，不需要手工解锁 ——
    手工解锁的版本会在异常路径上漏掉一次，然后整个调度器永久卡死。
    """
    if session.get_bind().dialect.name != "postgresql":
        return
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _CLAIM_LOCK_KEY})


@dataclass
class _Running:
    """一个在跑的解题 worker。**只在进程内有意义**，所以是内存态。"""

    challenge_id: str
    thread: threading.Thread
    started_at: float


@dataclass
class SchedulerStats:
    running: int
    limit: int
    queued: int
    challenges: list[str] = field(default_factory=list)


class Ctf2Scheduler:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._running: dict[str, _Running] = {}
        # ★ 续解标志（v0.26）：**内存态**。
        #
        # 它的生命周期只有"受理 → 启动"这几秒，落库反而错 ——
        # 服务重启后的孤儿回收不该让一道题"接着上次跑"：那时上下文早凉了，
        # 而且用户看到的是一次全新的运行，不是一个被恢复的旧现场。
        #
        # ⚠️ v0.28 起它只是**兜底**：主路径是 `_attempts` 里的快照 id
        #    （那个也带 `resume` 字段）。留着它是因为"没有快照的调用"
        #    仍然存在（比如测试直接调 `enqueue(challenge_id)`）。
        self._resume: set[str] = set()
        # ★ 策略快照 id（v0.28）：受理时定下的"这次按什么策略跑"。
        #
        # 为什么在这里中转一个 id 而不是把策略值一个个传下去：
        # 调度器只是"排队与启动"的中转，不该理解策略语义。传 id 让
        # worker 自己读快照，**将来加字段不用改这条链路** ——
        # 而这条链路跨了 service → scheduler → runner 三层，
        # 每加一个字段动三处是它最容易出错的地方。
        #
        # ⚠️ 与 `_resume` 不同，它**必须**在启动时取走并清掉：
        #    留着会让下一次重排（用户点"再解一次"）误用上一次的快照。
        self._attempts: dict[str, str] = {}
        self._lock = threading.Lock()
        # 让整个 tick 串行 —— 少了它，"数名额 + 认领"会与另一个
        # 触发点（比如接口里的 enqueue）交叉，于是并发上限失效。
        self._tick_lock = threading.Lock()

    # ══════════════════════════════════════════════════════
    # 生命周期
    # ══════════════════════════════════════════════════════

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="suvex-ctf2-scheduler", daemon=True
        )
        self._thread.start()
        logger.info("CTF2 调度器已启动（上限 %d）", max_concurrent(None))

    def stop(self, *, timeout: float = 15.0) -> None:
        """停止调度，并**等正在跑的 worker 自己结束**（最多 `timeout` 秒）。

        ⚠️ 这里**不杀线程**（杀不掉，见模块 docstring）。超时之后
        那些线程仍是 daemon，会随进程消失 —— 但**它们启动的沙箱
        容器不会**。所以进程退出前必须给 worker 足够时间跑完
        `finally` 里的 `sandbox.destroy()`。

        这条是"停服务时留下孤儿容器"这个真实故障的唯一防线，
        所以 `timeout` 默认给到 15 秒而不是 0。

        ## ★ 要等的是**两类**线程，只等调度线程是不够的

        调度线程只负责"认领并启动"，它在 worker 开始跑之后就没关系了。
        worker 是**另一个线程**（`_start` 里建的）。

        我第一版只 `join` 了调度线程 —— 实测撞到：一个正在跑的 worker
        被完全忽略，`stop()` 立刻返回，而那个容器还在。
        这个小错在测试里是一条红（`test_stop_waits_for_running_workers`），
        在生产上就是"每次重启都留几个孤儿容器"。

        `timeout` 是**总预算**：先给调度线程，剩下的给 worker，
        而不是每个线程各给一份（那会让总等待时间随在跑数量增长）。
        """
        self._stop.set()
        deadline = time.monotonic() + timeout

        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))

        with self._lock:
            workers = [run.thread for run in self._running.values()]

        for worker in workers:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            worker.join(timeout=remaining)

        with self._lock:
            alive = {cid: run for cid, run in self._running.items() if run.thread.is_alive()}
        if alive:
            # 出声。静默留下容器是本模块最坏的失败模式。
            logger.warning(
                "CTF2 调度器停止时仍有 %d 个解题任务在跑（%s）——"
                "它们会在后台跑完并自行销毁沙箱；若进程即将退出，"
                "请检查 `docker ps` 是否有残留的 ctf2-* 容器。",
                len(alive),
                ", ".join(alive),
            )

    def _interval(self) -> float:
        """读一次认领间隔，读不到就退回代码默认值。

        ★ 为什么**每轮都读**，而不是启动时读一次：用户在设置页把间隔从
        1 秒改成 10 秒之后，期望的是"下一个 tick 就变慢"。启动时读一次
        会让改动的生效时刻取决于**上一次读到的旧值** —— 旧值是 1 秒时
        改动立刻生效，旧值是 3600 秒时就得等一小时。那种"改了没反应"
        会让人以为设置坏了。

        代价是每轮一次主键读，相对 tick 本身可以忽略。

        ⚠️ 读库失败**绝不能**让循环退出：`_loop` 里的 try 只包住
        `tick()`，所以异常必须在这里兜住 —— 数据库抖动一次就永久停止
        认领，是那种"系统看起来还活着，但一道题都不动"的故障。
        """
        try:
            factory = get_session_factory()
            with factory() as session:
                return solve_poll_seconds(session)
        except Exception:  # noqa: BLE001 - 见上
            logger.warning("CTF2 读取认领间隔失败，本轮用默认值", exc_info=True)
            return DEFAULT_CTF2_SOLVE_POLL_SECONDS

    def _loop(self) -> None:
        while not self._stop.wait(self._interval()):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - 调度线程绝不能死
                logger.exception("CTF2 调度 tick 失败（下一轮会重试）")

    # ══════════════════════════════════════════════════════
    # 一个 tick
    # ══════════════════════════════════════════════════════

    def tick(self) -> int:
        """回收 → 算名额 → 认领。返回这一轮启动了几个。

        ★ 顺序不能反：先回收再算名额。反过来的话，"刚跑完一个"
        这一轮不会被利用上，队列会以一半的速度前进 —— 与
        `scans/scheduler.py` 的说明完全一致。
        """
        with self._tick_lock:
            self._reap_finished()
            return self._claim()

    def _reap_finished(self) -> None:
        """把已经结束的 worker 从在跑集合里摘掉。

        ⚠️ 只摘内存记录，**不动数据库** —— 状态由 worker 自己在
        第三段（同步段）里写好。这里再写一次会有两个写入方，
        而它们对"该是什么状态"的判断可能不一致。
        """
        with self._lock:
            done = [cid for cid, run in self._running.items() if not run.thread.is_alive()]
            for challenge_id in done:
                self._running.pop(challenge_id, None)
        if done:
            logger.debug("CTF2 回收了 %d 个已结束的解题任务", len(done))

    def _claim(self) -> int:
        """按**优先级**认领 `solving` 状态的题目，直到名额用完。

        ## 排序：`priority DESC, updated_at ASC`

        * `priority DESC` —— 用户显式调过优先级的题先做（v0.23）。
          比赛规则里"前三名额外加分"让**先做哪道题**直接换分，
          而"调整解题优先级"是赛事方明确允许的操作。
        * `updated_at ASC` —— **同级之内仍然是 FIFO**，也就是升级前的
          行为。全 0（没人调过）时排序与原来逐字一致。

        ## ⚠️ 一个真实的副作用：改优先级会把它排到同级的末尾

        `updated_at` 带 `onupdate`，所以"给一道题设优先级"这个动作
        本身会刷新它的 `updated_at`。对于**同级之内**的比较，那意味着
        刚调过优先级的那道题会排到同级最后。

        这不是 bug 而是取舍：要避免它就得再加一列"入队时刻"，
        而那会让"一道题的排队位置由哪个字段决定"变成两个字段的复合
        问题 —— 在"一次点几十道题、然后手工挑三五道插队"这个真实
        用法下，**优先级是主键、同级顺序无所谓**。写在这里是为了让
        下一个读这段代码的人不必自己发现它。

        ## 用 `priority` 排会让"取消再重排"不再是唯一手段

        原来用户想插队只能取消（状态打回 `unsolved`）再重新点解题 ——
        那会把这道题的中间状态搅乱，而且重排后它的 `updated_at` 又变了。
        现在直接改优先级即可，不改状态。
        """
        factory = get_session_factory()
        started = 0
        with factory() as session:
            _lock_claiming(session)
            limit = max_concurrent(session)
            with self._lock:
                running_ids = set(self._running)
            slots = limit - len(running_ids)
            if slots <= 0:
                return 0

            # ★★ 在 SQL 里排除"已经在跑"的题（2026-10-10 修）。
            #
            # ## 不排除会怎样：并发**静默退化**
            #
            # 原来的写法是 `.limit(min(slots, _MAX_CLAIMS_PER_TICK))` 之后
            # 在内存里 `if challenge.id in self._running: continue` —— 跳过的
            # 名额**不补取**。而"在跑"的题状态仍是 `solving`（环境启动期间
            # 还没走到 `environment_ready`），且排序是 `updated_at ASC`，
            # 于是它们**稳定地占据查询结果的前几行**。
            #
            # 实测推演：设上限 2，先跑起 A（正在起环境），再点 B、C ——
            # 每次 `slots=1`，取 1 行永远是 A，A 被跳过，B、C 在 A 的环境
            # 起来之前**一道都不会被认领**。实际并发退化成 1，而界面上
            # 显示上限 3。用户会把它归因为"平台慢"。
            #
            # ## 为什么把 limit 放宽到 `slots + len(running_ids)`
            #
            # 光排除还不够：`_MAX_CLAIMS_PER_TICK`（4）是"一次最多认领几道"
            # 的安全阀，语义是**认领数**而不是**取行数**。排除之后取到的
            # 每一行都会被真的启动，所以上限该按 `slots` 算。
            # 再取 `min(..., _MAX_CLAIMS_PER_TICK)` 才是对的。
            #
            # ⚠️ `notin_` 在 `running_ids` 为空时会生成 `NOT IN ()` —— 那在
            #    SQL 里是语法错误。所以空集时要跳过这个条件。
            stmt = select(Ctf2Challenge).where(
                Ctf2Challenge.status == Ctf2ChallengeStatus.SOLVING.value
            )
            if running_ids:
                stmt = stmt.where(Ctf2Challenge.id.notin_(running_ids))

            rows = list(
                session.execute(
                    stmt
                    # 优先级优先，同级内 FIFO。
                    # ⚠️ `updated_at` 是"被置成 solving 的时刻"，不是
                    #    "被同步进来的时刻" —— 后者（`created_at`）会让
                    #    一道三天前同步、刚才才点解题的题排到队首。
                    #    两条降序/升序都要显式写，否则会跟着主键序走。
                    .order_by(
                        Ctf2Challenge.priority.desc(),
                        Ctf2Challenge.updated_at.asc(),
                    )
                    .limit(min(slots, _MAX_CLAIMS_PER_TICK))
                    .with_for_update(skip_locked=True)
                )
                .scalars()
                .all()
            )
            for challenge in rows:
                # 排除已在 SQL 里做过，这里只防"取行与启动之间"的竞态
                # （另一个 worker 抢先启动）。正常路径不会命中。
                with self._lock:
                    if challenge.id in self._running:
                        continue
                self._start(challenge.id)
                started += 1
            session.commit()
        return started

    # ══════════════════════════════════════════════════════
    # 认领与启动
    # ══════════════════════════════════════════════════════

    def enqueue(
        self, challenge_id: str, *, resume: bool = False, attempt_id: str | None = None
    ) -> None:
        """受理一道题：置为 `solving` 并捅一下调度器。

        ★ 状态先落库、再启动：进程在两者之间崩溃时，这道题会停在
        `solving` 上，而**孤儿回收**会在下次启动时把它捡回来
        （见 `recover_orphans`）。反过来（先启动再落库）会留下一个
        跑着但数据库里显示 `unsolved` 的任务 —— 那是最糟的状态：
        它占着配额，而界面上看不出任何东西在跑。

        ## 两个参数的分工（v0.28 起）

        * `attempt_id` —— **主路径**：受理时落下的策略快照
          （`Ctf2SolveAttempt`）。worker 读它拿到 mode / brief /
          回合预算 / 模型，于是**排队期间改全局设置不会影响这次解题**。
        * `resume` —— **兜底**：没有快照的调用（测试、旧脚本）仍然
          靠这个内存标志表达"接着上次跑"。有快照时以快照里的
          `resume` 为准（它在受理时就被记下了，且不怕重启）。
        """
        with self._lock:
            if attempt_id is not None:
                self._attempts[challenge_id] = attempt_id
            if resume:
                self._resume.add(challenge_id)
        with session_scope() as db:
            challenge = db.get(Ctf2Challenge, challenge_id)
            if challenge is None:
                return
            challenge.status = Ctf2ChallengeStatus.SOLVING.value
            challenge.last_error = None
            db.commit()
        self.tick()

    def _start(self, challenge_id: str) -> None:
        from app.kernel import runner

        # ★ 取一次就清掉：这两个标志都只对**这一次**启动有效。
        #   不清的话，这道题以后每一次重排都会被当成续解（或误用上一次
        #   的策略快照）—— 而那时上次的过程可能已经是几十轮之前的事了。
        with self._lock:
            attempt_id = self._attempts.pop(challenge_id, None)
            resume = challenge_id in self._resume
            self._resume.discard(challenge_id)

        def target() -> None:
            try:
                runner.run_worker(challenge_id, resume=resume, attempt_id=attempt_id)
            except Exception:  # noqa: BLE001 - runner 自己已经兜住了，这是双保险
                logger.exception("CTF2 worker 线程异常退出：%s", challenge_id)

        thread = threading.Thread(
            target=target, name=f"suvex-ctf2-solve-{challenge_id[-8:]}", daemon=True
        )
        with self._lock:
            self._running[challenge_id] = _Running(
                challenge_id=challenge_id,
                thread=thread,
                started_at=now_utc().timestamp(),
            )
        thread.start()
        logger.info("CTF2 开始解题：%s（在跑 %d 个）", challenge_id, len(self._running))

    # ══════════════════════════════════════════════════════
    # 孤儿回收
    # ══════════════════════════════════════════════════════

    def recover_orphans(self) -> int:
        """服务启动时清理上一次运行遗留的题目。返回处理了几道。

        ## ★ `environment_ready` 也必须清（v0.26 补，实测踩到）

        worker 的流程是：起环境 → 把状态设成 `environment_ready` → **跑解题
        循环（几分钟到几十分钟）** → 回写最终状态。

        也就是说 `environment_ready` **是整个解题过程的主状态**，不是一个
        瞬间的中间态。这里原来只认 `solving`，于是（2026-10-02 实测）：

        部署时重启了服务，一道正在解题的题停在 `environment_ready` 上 ——
        它**永远卡住了**：

        * 后端认为这个状态可解（`RESOLVABLE_CTF2_CHALLENGE_STATUSES` 里有它）；
        * 前端 `canSolve` 的列表里没有它，所以**不给解题按钮**；
        * 结果是一道没有错误、没有在跑、也点不动的题。

        用户看到的就是这个：环境「已停止 · 轮询 2 次」、卡片上只剩「取消」。

        ## 为什么必须有

        `solving` 的含义是"已经受理，正在队列里或正在跑"。而
        **本进程的内存里没有任何对应的 worker** —— 上一轮的生命周期
        已经结束了。如果不清理：

        * 界面上它们永远显示"解题中"，而实际上没有任何东西在跑；
        * 它们会占着"已受理"这个状态，用户点了解题也不会重新触发
          （因为状态已经是 `solving`）；
        * 沙箱容器可能还在（如果进程是硬崩的），而那笔资源没人认领。

        ## 为什么重置成 `unsolved` 而不是 `failed`

        这两个状态对用户的意义完全不同：`failed` 是"试过了，没成"，
        而这里的情况是"**根本没跑完**"—— 它不该消耗用户对这道题的
        信任（也不该让"错误提交次数"之类的判断受影响）。
        重置成 `unsolved` 让用户可以干净地再点一次。

        ⚠️ 这是"宁可重跑"的取舍：如果上一轮其实已经解出来了、
        只是还没来得及落库，我们会重跑一次 —— 但**提交是幂等的**
        （去重 + 额度检查），所以重跑不会重复提交。
        """
        recovered = 0
        with session_scope() as db:
            rows = list(
                db.execute(
                    select(Ctf2Challenge).where(
                        Ctf2Challenge.status.in_(
                            (
                                Ctf2ChallengeStatus.SOLVING.value,
                                # ★ 见 docstring：它是解题过程的主状态，
                                #   不是瞬间中间态 —— 漏了它就会留下
                                #   "点不动也跑不了"的僵尸题。
                                Ctf2ChallengeStatus.ENVIRONMENT_READY.value,
                                # ★ 加上 submitting（2026-10-10 修）。
                                #
                                # `submit_candidate` 在**发出提交请求之前**
                                # 就把状态写成 `submitting`（刻意的：界面要能
                                # 显示"正在提交"）。进程若恰好死在
                                # "状态已写、响应还没回写"之间，这道题重启后：
                                #
                                #   · 这里不回收它；
                                #   · `_assert_resolvable` 对 `submitting` 抛
                                #     409「这道题正在处理中」—— 而**没有任何
                                #     东西在处理**；
                                #   · 用户唯一的出路是手动点「取消」。
                                #
                                # 这正是本函数 docstring 里描述过的那种僵尸
                                # （"没有错误、没有在跑、也点不动的题"），
                                # 只是换了一个状态值。
                                #
                                # ⚠️ 那条停在 `pending` 的提交记录**保留**：
                                #    它的语义是"请求已发出、结果未知"，是
                                #    一张需要人工对账的欠条（见
                                #    `vocab.Ctf2SubmissionStatus.PENDING`）。
                                #    这里只重置题目状态，不动提交记录。
                                Ctf2ChallengeStatus.SUBMITTING.value,
                            )
                        )
                    )
                )
                .scalars()
                .all()
            )
            for challenge in rows:
                challenge.status = Ctf2ChallengeStatus.UNSOLVED.value
                challenge.last_error = "上一次运行未完成，已重置为未解出。"
                events.emit_abandoned(db, challenge, reason="服务重启，解题中断")
                # ★ v0.27：顺手把解题 scan 与 solver agent 收尾。
                #
                # 走的是与正常结束**同一个** `finalize_solve_scan`（不另写一套）：
                # 它按题目状态映射 scan 终态 + 收 agent + 刷计数。
                #
                # 不收的后果是实测到的：题目被重置成 `unsolved` 了，而它的
                # scan 永远显示"运行中"、agent 永远转圈 —— 三个页面三种说法。
                # `finalize_solve_scan` 会把它记成 `failed`（题没解出），
                # 那是对的：这一轮**确实**没有产出一个结果。
                try:
                    events.finalize_solve_scan(db, challenge)
                except Exception:  # noqa: BLE001 - 收尾失败不该让回收失败
                    logger.warning(
                        "回收题目 %s 时收尾解题记录失败（已忽略）",
                        challenge.id,
                        exc_info=True,
                    )
                recovered += 1
            db.commit()
        if recovered:
            logger.warning("CTF2 回收了 %d 道未完成的题目（已重置为未解出）", recovered)
        return recovered

    # ══════════════════════════════════════════════════════
    # 查询
    # ══════════════════════════════════════════════════════

    def stats(self, db: Session | None = None) -> SchedulerStats:
        """给接口用的快照。与 `GET /scans/queue` 同一条思路：
        它回答的是"为什么我点了解题却一直排在队列里"。"""
        with self._lock:
            running = list(self._running)
        queued = 0
        if db is not None:
            queued = len(
                db.execute(
                    select(Ctf2Challenge.id).where(
                        Ctf2Challenge.status == Ctf2ChallengeStatus.SOLVING.value
                    )
                ).all()
            )
        return SchedulerStats(
            running=len(running),
            limit=max_concurrent(db),
            queued=max(0, queued - len(running)),
            challenges=running,
        )


# ══════════════════════════════════════════════════════════
# 模块级单例
# ══════════════════════════════════════════════════════════

_scheduler: Ctf2Scheduler | None = None
_scheduler_lock = threading.Lock()


def get_scheduler() -> Ctf2Scheduler:
    """进程内单例。

    ⚠️ 延迟构造 + 加锁：`scans` 那边用的是一个裸的模块级全局，
    而它在"两个请求线程同时第一次调用"时会创建两个调度器
    （于是并发上限被悄悄翻倍）。这里顺手把这个坑堵上 ——
    调度器的并发上限是**安全阀**，翻倍等于它没起作用。
    """
    global _scheduler
    if _scheduler is None:
        with _scheduler_lock:
            if _scheduler is None:
                _scheduler = Ctf2Scheduler()
    return _scheduler


def reset_scheduler() -> None:
    """停掉并丢掉单例。测试用（与各领域里 `reset_limiters` 的角色相同）。"""
    global _scheduler
    with _scheduler_lock:
        if _scheduler is not None:
            _scheduler.stop(timeout=5.0)
        _scheduler = None


__all__ = [
    "Ctf2Scheduler",
    "SchedulerStats",
    "get_scheduler",
    "max_concurrent",
    "reset_scheduler",
]
