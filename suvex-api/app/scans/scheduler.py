"""扫描调度器：队列、并发上限、进程监督、孤儿回收。

## 它解决的是什么问题

任务原文：「同时运行的扫描数量要有上限（可配置），超过上限的新扫描先排队，
**不要无限制地并发起真实攻击性质的进程**。」

所以这一层不是"性能优化"，是**安全阀**。一次扫描会拉起一个沙箱容器
加大量 LLM 调用，而且它**真的在向目标发包**。失控的并发不只是把机器拖慢 ——
它意味着同时在打多个目标、费用不可控、而且没有任何界面能说清"现在到底在跑几个"。

## 一个 tick 做三件事

```
tick()
 ├─ 1. 回收已退出的进程（poll 句柄 → 定终态）
 ├─ 2. 算出还剩几个名额 = 上限 − 在跑的
 └─ 3. 按 FIFO 认领 queued 的扫描并启动
```

顺序不能反：先回收再算名额，否则"刚跑完一个"这一轮不会被利用上，
队列会以一半的速度前进。

## 为什么是一个线程 + 轮询，而不是每个扫描一个监控线程

* 线程数不随扫描数增长（上限本来就是 1~几个，但监控线程会各自持有会话）；
* 所有状态变更都在**一个地方**发生，状态机的正确性只需要在一处保证；
* 轮询间隔（2 秒）远小于人对状态变化的感知阈值，够用。

代价是多最多 2 秒的"状态回写延迟"，如实记在这里。

## 认领用 `FOR UPDATE SKIP LOCKED`

现在只有一个 API 进程，加锁看起来多余。但它防的是一件具体的事：
**将来跑多个 worker 时，两个进程同时认领同一条 queued 扫描**。
`SKIP LOCKED` 让第二个进程直接跳过被锁住的行，而不是等它 ——
没有它的话，两个进程会各自启动一个 strix 去打同一个目标，
而"一次扫描跑了两遍"在界面上完全看不出来。
"""

from __future__ import annotations

import logging
import subprocess
import threading
from pathlib import Path

from sqlalchemy import func, select, text

from app.core.config import get_settings
from app.core.db import get_session_factory
from app.core.timeutil import now_utc, seconds_between
from app.core.vocab import SCAN_ORIGIN_CTF2, ScanGoal, ScanStatus
from app.scans import service
from app.scans.flag import extract_flag
from app.scans.models import Scan
from app.scans.runner import (
    RunnerUnavailable,
    did_strix_run,
    get_runner,
    is_alive,
    is_terminable,
    terminate_tree,
)
from app.settings import service as settings_service

logger = logging.getLogger("suvex.scheduler")

# 认领扫描时用的 PostgreSQL 咨询锁 key。取一个本应用专用的常数即可。
_CLAIM_LOCK_KEY = 0x5A17_0001


def _flag_of(scan_id: str) -> str | None:
    """这次扫描的 flag —— **只碰文件系统**。

    ★ 为什么单独跑一趟、而且用一个**独立的短会话**读配置：

    调用方紧接着就要开一个写事务（`session.get()` 起手）。把磁盘读取夹在
    "开事务"与"commit"之间，会让持锁窗口跨越 I/O —— 实测撞过一次
    `DeadlockDetected`（详细说明见 `_reap_finished` 里那段）。
    这里读完 `goal` / `run_dir` 就 `close()`，锁在读文件**之前**已经放掉。

    非夺旗扫描直接返回 `None`：真实渗透报告里 `{...}` 形状的东西太多，
    对它们跑抽取会稳定地产出假 flag（见 `scans/flag.py`）。
    """
    session = get_session_factory()()
    try:
        scan = session.get(Scan, scan_id)
        if scan is None or scan.goal != ScanGoal.CAPTURE_FLAG.value:
            return None
        run_dir = scan.run_dir
    finally:
        # 用 finally：`get()` 抛了也要把会话关掉，否则连接会一直挂着
        session.close()
    return extract_flag(run_dir) if run_dir else None


def _run_dir_of(scan_id: str) -> str | None:
    """这次扫描的工作目录 —— **只碰数据库，且用一个独立的短会话**。

    与 `_flag_of` 完全同构（同一个理由，见那里的长说明）：调用方紧接着
    要开写事务，所以这里读完就 `close()`，锁在读文件**之前**已经放掉。

    ★ 为什么不让调用方用自己那个 `session` 顺手读一下：
    那段代码里 `session.get()` 就是**开事务**的动作，读 `run_dir`
    必须发生在它之前 —— 而"之前"只能靠一个已经关掉的会话。
    """
    session = get_session_factory()()
    try:
        scan = session.get(Scan, scan_id)
        return scan.run_dir if scan is not None else None
    finally:
        session.close()


def _not_ctf2_shell():
    """SQL 条件：**排除 CTF2 借来挂事件流的壳**。

    ## ★ 为什么扫描侧的每一处"遍历 running 扫描"都要带上它

    CTF2 每解一道题都借一条 scan 来挂活动流（`ctf2/events.ensure_solve_scan`），
    而那条 scan 的状态由 **CTF2 自己**维护 —— 它在解题期间就是 `running`，
    短则几十秒、长则几十分钟。

    对扫描侧来说，那条行**不是它的工作单元**：`self._procs` 里没有它的进程，
    也不该有。而扫描侧有三处按 `status == 'running'` 遍历，每一处漏掉它
    都会产生一个具体故障：

    | 位置 | 漏掉的后果（全部实测或可直接推演） |
    | ---- | --------------------------------- |
    | `_claim_and_launch` 数名额 | 每解一道题，扫描并发就少一个名额 —— **静默的超卖/欠卖**。设成 3 时，一道题在解就只剩 2 个扫描位，而界面上没有任何地方解释这件事。 |
    | `_detect_stalls` 卡死检测 | CTF2 的壳靠 worker 写事件，事件间隔取决于 LLM 响应速度。慢题一安静就是十几分钟，于是**它会被标记「疑似卡死」**；若 `scan_stall_action=abort`，直接把它置成 `aborted` —— 而题**还在解**。 |
    | `recover_orphans` 孤儿回收 | 服务重启时它会认为"这条 running 没有对应进程"，于是判成 `failed` 并写一句"扫描进程已不在，结果不可知"。实测已发生 5 次（见 `failure_reason` 里那批"服务重启"记录），而其中有的题**其实解出来了** —— 于是扫描列表说失败、题目说已解出。 |

    ⚠️ 判据用 `origin` 列而**不是**"查 ctf2_challenges 关联表"：
    重跑会建出一条**没有 challenge 指向它**的新壳（这正是 2026-10-08
    那次事故的产生方式）。详见 `vocab.SCAN_ORIGIN_CTF2`。
    """
    return Scan.origin.is_distinct_from(SCAN_ORIGIN_CTF2)


def _lock_claiming(session) -> None:  # noqa: ANN001
    """在多 worker 场景下串行化「数名额 + 认领」。

    单进程里 `threading.Lock` 就够了（见 `tick()`）。这一条是为**将来加 worker**
    准备的：两个进程各自数出"在跑 0 个"，然后各启动一个 —— 同一个上限就失效了，
    而症状是"偶发地同时跑了好几个扫描"，几乎不可能复现。

    `pg_advisory_xact_lock` 在**事务结束时自动释放**，不需要手工解锁 ——
    手工解锁的版本会在异常路径上漏掉一次，然后整个调度器永久卡死。

    非 PostgreSQL 直接跳过：本项目只支持 PG（SQLite 只出现在少数纯逻辑测试里）。
    """
    if session.get_bind().dialect.name != "postgresql":
        return
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _CLAIM_LOCK_KEY})


class ScanScheduler:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # scan_id → Popen。只在**本进程**内有意义，所以它是内存态而不是数据库列。
        self._procs: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()
        # ★ 让整个 tick 串行。见 `tick()` 的说明 —— 少了它并发上限会失效。
        self._tick_lock = threading.Lock()

    # ══════════════════════════════════════════════════════
    # 生命周期
    # ══════════════════════════════════════════════════════

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="suvex-scan-scheduler", daemon=True)
        self._thread.start()
        logger.info("扫描调度器已启动（上限 %d）", get_settings().max_concurrent_scans)

    def stop(self, *, timeout: float = 15.0) -> None:
        """停下来。

        ★ **停之前要把在跑的扫描终止掉。** 不终止的话，它们会变成
        **无人跟踪的攻击进程**：服务重启之后没有任何界面能终止它们
        （新进程里的 `_procs` 是空的，`pid` 虽然还在库里，但那是最后手段）。
        一个用户以为已经停了的服务，其实还在替他打目标 —— 这是最糟的一种状态。

        代价如实记：开发时的一次热重载也会终止在跑的扫描。
        那是对的 —— 宁可重跑，也不要留一个看不见的进程。
        """
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        self._terminate_tracked(reason="服务停止，扫描已终止")
        logger.info("扫描调度器已停止")

    def _loop(self) -> None:
        interval = get_settings().scan_poll_seconds
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                # 调度线程**绝不能因为一次异常就死掉** —— 死了之后
                # 所有扫描会永远停在 queued，而界面上只显示"排队中"，
                # 没有任何地方说明"调度器已经不转了"。
                logger.exception("调度循环出错（已忽略，下一轮重试）")
            self._stop.wait(interval)

    # ══════════════════════════════════════════════════════
    # 供端点查询
    # ══════════════════════════════════════════════════════

    @property
    def running_count(self) -> int:
        with self._lock:
            return sum(1 for proc in self._procs.values() if proc.poll() is None)

    def register(self, scan_id: str, process: subprocess.Popen) -> None:
        with self._lock:
            self._procs[scan_id] = process

    def forget(self, scan_id: str) -> None:
        with self._lock:
            self._procs.pop(scan_id, None)

    def request_stop(self, scan_id: str) -> bool:
        """请一个在跑的任务自己退出。返回"有没有找到可请求的句柄"。

        ## 什么时候需要它

        自研引擎后端的句柄是**线程**（`terminable=False`，`Scan.pid` 恒为 0）。
        用户点「终止扫描」时 `service.abort_scan` 那边拿不到 pid，也就
        `terminate_tree` 不了 —— 唯一能做的就是到这里问一句"你手上那个
        线程还在吗，让它停"。线程本身杀不掉（见 `agent_runner` 的说明），
        所以这是"请求"不是"命令"：它会在下一个检查点退出，期间可能还跑
        几秒。

        ## 为什么找不到句柄不报错

        服务重启过、或这个扫描根本不是本进程启动的 —— 这两种情况下我们
        确实无从下手，而 `abort_scan` 已经把记录置成 aborted 了。返回值是
        `False` 让调用方能决定要不要打日志，但**不是**失败（记录状态是对的）。
        """
        with self._lock:
            process = self._procs.get(scan_id)
        if process is None:
            return False
        stopper = getattr(process, "request_stop", None)
        if not callable(stopper):
            return False
        stopper()
        return True

    def runner_status(self) -> tuple[bool, str | None]:
        """执行后端自检。给 `GET /scans/queue` 用。"""
        try:
            get_runner().ensure_ready()
        except RunnerUnavailable as exc:
            return False, str(exc)
        return True, None

    # ══════════════════════════════════════════════════════
    # 一轮
    # ══════════════════════════════════════════════════════

    def tick(self) -> None:
        """一轮：回收 → 算名额 → 认领并启动。

        ★ **整个 tick 必须串行**，这不是"顺手加的锁"。

        没有它会出现一个真实的竞态，而且它已经被实测抓到过：

        > `POST /scans` 之后会在后台线程里捅一下调度器（为了让状态立刻变，
        > 而不是等下一次轮询）。三个新建请求 → 三个线程**同时**算出
        > "当前在跑 0 个" → 各自认领一个 → **上限形同虚设**。
        > 日志里三个 pid 都在，而配置写的是 1。

        `FOR UPDATE SKIP LOCKED` 挡不住这个：三个线程认领的是**三条不同的**
        扫描行，行锁彼此不冲突。真正需要互斥的是"数名额 + 认领"这一整段。
        """
        with self._tick_lock:
            self._reap_finished()
            # 卡死检测（任务 11）—— 必须在导入之后：它读的正是刚导进来的事件
            self._detect_stalls()
            self._claim_and_launch()

    def _reap_finished(self) -> None:
        """把已经退出的进程收尾。"""
        with self._lock:
            handles = list(self._procs.items())

        for scan_id, process in handles:
            code = process.poll()
            if code is None:
                # 还在跑：把外部工具**已经写出来**的事件导进来。
                #
                # 这就是任务 4 的实时性来源 —— 事件先落库，
                # SSE 端（本来就在轮询 activity_events）自然就推给前端了。
                # 不在这里导的话，活动流要等扫描**结束**才一次性出现，
                # 而那正是任务 4 要消灭的现象。
                self._import_for(scan_id)
                continue
            self.forget(scan_id)

            # ★ 把 flag 抽出来 —— 必须在**开启写事务之前**（任务 18）。
            #
            # 它只碰文件系统，而下面那个会话一 `session.get()` 就开了事务。
            # 把一次磁盘读取夹在"开事务"与"commit"之间，等于让持锁窗口
            # 跨越 I/O —— 实测撞过一次 `DeadlockDetected`：它与 conftest
            # 的全表 TRUNCATE 撞在一起，而**关掉抽取就再也复现不出来**。
            #
            # 这不是"只在测试里有"的问题：生产里任何批量维护动作
            # （归档、清理、迁移）都可能在那个窗口里进来。
            # 事务里不做 I/O，本来就该是默认做法。
            flag = _flag_of(scan_id)

            # ★ v0.27：消歧退出码 2 —— 同样必须在**开事务之前**。
            #
            # `2` 有两个语义：strix 的"发现了漏洞"（`EXIT_FINDINGS`），
            # 以及 argparse 的"命令行用法错误"。实测撞到的形态是用户对
            # CTF2 的壳点重跑，strix 拒收 `ctf2:…` 这个 target 后退 2，
            # 而那条什么都没干的记录被 `finalize` 记成「已完成」。
            #
            # 判据是 `strix_runs/` 目录在不在（见 `runner.did_strix_run`
            # 的完整说明与实测分离数据）。与 `_flag_of` 同一个理由放在这里：
            # 它碰文件系统，而 `session.get()` 一开事务就会让持锁窗口跨 I/O。
            ran = did_strix_run(_run_dir_of(scan_id))

            session = get_session_factory()()
            try:
                scan = session.get(Scan, scan_id)
                if scan is None:
                    continue

                # 抓一次外部工具自己的 run 标识。失败时留空 ——
                # 它是诊断信息，不该影响状态判定。
                if not scan.run_id and scan.run_dir:
                    try:
                        scan.run_id = get_runner().detect_run_id(Path(scan.run_dir))
                        session.commit()
                    except Exception:  # noqa: BLE001
                        session.rollback()
                        logger.debug("读取 run 标识失败（忽略）", exc_info=True)

                # ★ 收尾前把外部事件**读干净**。
                #
                # 工具在退出前可能刚写完最后几行，而那一轮导入还没跑到。
                # 不做这一步，历史回放里会永远缺最后几条 ——
                # 而"历史扫描能完整回放"是任务 4 的验收标准之一。
                self._import_for(scan_id, backfill=True, session=session)

                if scan.status != ScanStatus.RUNNING.value:
                    # 已经被 abort 了（它自己会写终态）。退出码在这里没有意义：
                    # 我们杀掉它时得到的退出码和"它自己失败"的退出码长得一样，
                    # 覆盖过去会把"用户终止的"变成"失败的"。
                    continue

                scan.pid = None
                # ★ `ran` 一并传下去：退出码 2 与"真的跑过"必须同时成立
                #   才算"发现了漏洞"。见 `service.finalize` 的判定。
                service.finalize(session, scan, exit_code=code, flag=flag, strix_ran=ran)
            except Exception:  # noqa: BLE001
                session.rollback()
                logger.exception("收尾扫描 %s 时出错", scan_id)
            finally:
                session.close()

    def _import_for(self, scan_id: str, *, backfill: bool = False, session=None) -> None:  # noqa: ANN001
        """给一个扫描导入外部事件与漏洞记录。**异常一律吞掉。**

        吞掉的理由：导入是**可观测性**，不是扫描本身。
        一次读库失败（WAL 正在被写、文件刚建出来还是空的）不该让调度循环出错 ——
        下一轮会重试，而外部产物是只增的，晚一轮读不会丢数据。

        `session` 传进来时复用调用方的会话（收尾路径上已经有了），
        否则自己开一个（运行中的轮询路径）。
        """
        from app.findings import service as findings_service
        from app.scans import importer

        own_session = session is None
        conn = session or get_session_factory()()
        try:
            scan = conn.get(Scan, scan_id)
            if scan is None:
                return
            # ★ 事件导入失败**不能**连累漏洞导入。
            #
            # 原来这三段共用一个 try：事件那段一抛异常就直接跳到最外层
            # except，后面的漏洞导入与汇总刷新**根本没被执行**。实测
            # （2026-09-27）一条含 NUL 字节的事件让某次扫描丢了全部 100 条
            # 事件、**以及**那唯一一条漏洞（flag 就写在它的报告里），而界面
            # 上只表现为"这次没扫到东西" —— 因为失败记在 debug 级。
            #
            # 现在每段各自兜住自己的异常，日志一律 warning：静默失败正是
            # 这次排查时"什么都看不见"的根源。
            try:
                if backfill:
                    importer.backfill_after_finish(conn, scan)
                else:
                    importer.import_events(conn, scan)
            except Exception:  # noqa: BLE001
                conn.rollback()
                logger.warning(
                    "导入扫描 %s 的外部事件失败（将在下一轮重试）", scan_id, exc_info=True
                )

            # ★ 漏洞记录也在同一条路径上导入（任务 5 要求 1）。
            #
            # 与事件导入共用一次会话与一个时点，是刻意的：
            # 分开成两条独立的轮询会让"事件说发现了 X"与"漏洞列表里有 X"
            # 在时间上错开，而活动流与漏洞列表对不上的表现看起来像数据丢失。
            try:
                findings_service.import_from_run(conn, scan)
            except Exception:  # noqa: BLE001
                conn.rollback()
                logger.warning("导入扫描 %s 的漏洞记录失败（将在下一轮重试）", scan_id, exc_info=True)

            # ★ 汇总列（`findings` / `agentCount`）在这里刷。
            #
            # 这两列是**冗余列**，存在的理由是扫描列表一屏几十条 ——
            # 逐条去 count 一次等于几十个查询，而契约 §3.2 把这两个字段
            # 放进了列表响应里，所以必须有人负责让它们是真的。
            #
            # 必须刷在**导入之后**：`findings` 原来只在建行时写过一次 `{}`，
            # 之后再没人碰过，于是扫描列表永远显示"未发现漏洞"、
            # "0 个智能体"，而漏洞页里明明有两条 —— 两个页面互相打脸，
            # 且列表那一侧看起来像"这次扫描什么都没扫到"。
            #
            # 单独包一层 try：刷汇总失败不该被记成"导入失败"，
            # 下一轮还会再刷一次（导入是幂等的，汇总也是）。
            try:
                service.refresh_scan_rollup(conn, scan)
            except Exception:  # noqa: BLE001
                conn.rollback()
                logger.warning("刷新扫描 %s 的汇总列失败（将在下一轮重试）", scan_id, exc_info=True)
        except Exception:  # noqa: BLE001
            conn.rollback()
            logger.warning("导入扫描 %s 时出错（将在下一轮重试）", scan_id, exc_info=True)
        finally:
            if own_session:
                conn.close()

    def _detect_stalls(self) -> None:
        """把"很久没有新事件"的在跑扫描标记出来（任务 11）。

        ## 为什么需要它

        实测有过一次扫描跑了 32 分钟零产出：agent 把 `https` 当成了目标协议
        （目标其实只有 http），每个请求都撞在 TLS 握手上失败，而它反复用
        **空输入**轮询同一个 shell 会话（45 次 `write_stdin {"chars":""}`，
        等待从 15 秒一路涨到 60 秒）。

        那是"一个命令永远不返回"的形态 —— 而 `--max-turns` **拦不住它**：
        回合数根本没在涨，时间却在走。用户只能看着一个转圈的界面，
        既不知道还该不该等，也不知道哪里出了问题。

        ## 判据是事件时间戳，不是 CPU 或输出量

        `_import_for` 每轮都把外部工具新写出的事件导进来（那正是实时性的
        来源）。所以"事件停了" == "agent 确实没在产出"，与它在忙什么无关。
        用 CPU 占用判断会误伤"在跑长命令但没输出"的正常情况，
        用输出量则要读文件大小、还依赖外部工具的落盘时机。

        ## 默认只标记、不终止

        见 `config.scan_stall_action`：这是个**启发式**判断，而终止一个
        正在跑真扫描的进程不可逆。宁可让用户看到提示后自己决定。

        ## 标记是**可自愈**的

        一旦又有新事件进来就清掉。否则一次误判（比如某个工具确实安静了
        11 分钟才输出）会让界面永远挂着"疑似卡死"，而它其实早已继续。
        """
        cfg = get_settings()
        limit = cfg.scan_stall_seconds
        if limit <= 0:
            return

        from app.events.models import ActivityEvent

        session = get_session_factory()()
        try:
            running = list(
                session.execute(
                    select(Scan)
                    .where(Scan.status == ScanStatus.RUNNING.value)
                    # ★ CTF2 的壳不归这里管：它的"静默"取决于 LLM 响应速度，
                    #   而它的生命周期由 CTF2 的 worker 维护。标记它卡死是误判，
                    #   abort 模式下更是会把它置成 aborted 而题还在解。
                    #   见 `_not_ctf2_shell`。
                    .where(_not_ctf2_shell())
                ).scalars()
            )
            if not running:
                return

            moment = now_utc()
            changed = 0
            for scan in running:
                # 最近一条事件的时刻。用 max(at) 走索引（`ix_activity_events_scan_seq`）
                latest = session.execute(
                    select(func.max(ActivityEvent.at)).where(
                        ActivityEvent.scan_id == scan.id
                    )
                ).scalar_one_or_none()

                if latest is not None and latest != scan.last_event_at:
                    scan.last_event_at = latest

                # 判据用"最近活动"；一次事件都还没产生时退回 started_at ——
                # 否则一个启动后就卡住的扫描永远不会被判为卡死
                # （`last_event_at` 是空的，比较无从谈起）。
                reference = latest or scan.started_at
                silent_seconds = (moment - reference).total_seconds()

                if silent_seconds >= limit:
                    if scan.stalled_at is None:
                        scan.stalled_at = moment
                        changed += 1
                        logger.warning(
                            "扫描 %s 已静默 %.0f 秒（阈值 %.0f），标记为疑似卡死",
                            scan.id,
                            silent_seconds,
                            limit,
                        )
                        # ★ `abort` 模式下**当场终止**（见 config.scan_stall_action）。
                        #
                        # 标记与终止是两件事，所以这里先标再杀：即便终止失败
                        # （进程已经不在、kill 报错），标记也已经落库 ——
                        # 用户至少能看到"它为什么不见了"。
                        if cfg.scan_stall_action == "abort":
                            self._abort_stalled(scan, silent_seconds)
                elif scan.stalled_at is not None:
                    # ★ 自愈：又有事件了，把标记清掉
                    scan.stalled_at = None
                    changed += 1

            if changed:
                session.commit()
        except Exception:  # noqa: BLE001
            session.rollback()
            logger.debug("卡死检测失败（下一轮重试）", exc_info=True)
        finally:
            session.close()

    def _abort_stalled(self, scan: Scan, silent_seconds: float) -> None:
        """终止一个判定为卡死的扫描（仅 `scan_stall_action="abort"` 时）。

        ## 为什么要处理"进程根本不在我们手上"

        `_detect_stalls` 读的是**数据库里的 running 扫描**，而进程句柄
        （`self._procs`）只在**本进程**启动过它时才存在。服务重启之后，
        一条 `running` 记录可能对应一个我们不知道 pid 的进程 —— 那正是
        `recover_orphans` 要处理的情况。

        所以这里有两条路：
          · 句柄在 → `terminate_tree` 杀掉整棵树（含孙进程），这条最干净；
          · 句柄不在 → 只把**记录**置为 aborted 并说明原因。⚠️ 没有办法
            杀那个进程 —— 谎称"已终止"会让用户以为目标上已经干净了，
            所以措辞必须写明"进程可能仍在运行，请手工检查"。

        终止方始终是 `system`：用户没按任何按钮，是服务按规则决定的。
        """
        with self._lock:
            process = self._procs.get(scan.id)

        killed = False
        if process is not None and process.poll() is None:
            if is_terminable(process):
                terminate_tree(
                    process.pid,
                    grace_seconds=get_settings().scan_abort_grace_seconds,
                    log=logger.warning,
                )
            else:
                # 自研引擎：线程杀不掉，只能请求它在下一个检查点自己退出。
                stopper = getattr(process, "request_stop", None)
                if callable(stopper):
                    stopper()
            self.forget(scan.id)
            killed = True

        reason = (
            f"疑似卡死：已静默 {silent_seconds:.0f} 秒，无任何新活动事件，服务已自动终止。"
            if killed
            else (
                f"疑似卡死：已静默 {silent_seconds:.0f} 秒，无任何新活动事件。"
                " ⚠️ 进程句柄不在本服务手中，可能仍在运行，请手工检查。"
            )
        )
        logger.warning("按 scan_stall_action=abort 终止扫描 %s（%s）", scan.id, reason)

        scan.status = ScanStatus.ABORTED.value
        scan.finished_at = now_utc()
        scan.duration_sec = seconds_between(scan.started_at, scan.finished_at)
        scan.failure_reason = reason
        scan.terminated_by = "system"
        scan.pid = None

    def _claim_and_launch(self) -> None:
        """算名额并认领。

        ⚠️ 名额按**数据库里 `running` 的行数**算，而不是按 `self._procs`。
        内存里那份是"已经注册好句柄的进程"，而在"认领了但还没启动完"的那段窗口里
        它是偏小的 —— 正是那段窗口让并发上限失效。数据库是双方唯一的共识。
        """
        session = get_session_factory()()
        claimed: list[str] = []
        try:
            _lock_claiming(session)

            running = int(
                session.execute(
                    select(func.count())
                    .select_from(Scan)
                    .where(Scan.status == ScanStatus.RUNNING.value)
                    # ★ 只数**自己的**在跑扫描：CTF2 借的壳占的是 CTF2 的
                    #   配额（`ctf2_config.max_concurrent_solves`），
                    #   把它算进来等于两个配额互相蚕食。见 `_not_ctf2_shell`。
                    .where(_not_ctf2_shell())
                ).scalar_one()
            )
            # ★ 每 tick 重读，而且读的是 **DB 覆盖后的生效值**（任务 13）。
            #
            # 这是"在设置页改并发不用重启服务"的落点。原来读的是
            # `get_settings().max_concurrent_scans` —— 那是 env 常量，
            # 只有重启进程才会变；而重启会经由 `recover_orphans()`
            # **终止所有正在跑的扫描**。所以这条读法不只是方便，
            # 它是"改并发不杀正在解的题"的唯一实现方式。
            capacity = (
                settings_service.effective_execution_settings(session).max_concurrent_scans
                - running
            )
            if capacity <= 0:
                return

            queued = list(
                session.execute(
                    select(Scan)
                    .where(Scan.status == ScanStatus.QUEUED.value)
                    .order_by(Scan.created_at, Scan.id)
                    .limit(capacity)
                    .with_for_update(skip_locked=True)
                ).scalars()
            )
            if not queued:
                return

            moment = now_utc()
            for scan in queued:
                scan.status = ScanStatus.RUNNING.value
                # ★ `startedAt` 在这里被**改写为真实开跑时刻**。
                #
                # 契约 §2.1 说它在 queued 时等于受理时刻（因为它非空）。
                # 而一旦真的开跑，语义上它就该是"开跑时刻" —— 否则
                # `durationSec = finishedAt - startedAt` 会把**排队等待的时间**
                # 也算进"扫描耗时"，那是两个不同的东西。
                scan.started_at = moment
                claimed.append(scan.id)
            # 先提交再启动：进程起来了但状态没落库的话，abort 找不到它。
            session.commit()
        except Exception:  # noqa: BLE001
            session.rollback()
            logger.exception("认领排队扫描失败")
            return
        finally:
            session.close()

        for scan_id in claimed:
            self._launch_one(scan_id)

    def _launch_one(self, scan_id: str) -> None:
        if self._stop.is_set():
            # 正在停止：不要再启动新的了，把它退回队列会让它一直排队，
            # 所以直接什么都不做 —— 库里它是 running，孤儿回收会处理。
            return
        session = get_session_factory()()
        try:
            scan = session.get(Scan, scan_id)
            if scan is None or scan.status != ScanStatus.RUNNING.value:
                return
            result = service.launch_scan(session, scan)
            if result is not None:
                self.register(scan_id, result.process)
        except Exception:  # noqa: BLE001
            session.rollback()
            logger.exception("启动扫描 %s 时出错", scan_id)
        finally:
            session.close()

    def _terminate_tracked(self, *, reason: str) -> None:
        with self._lock:
            items = list(self._procs.items())
        for scan_id, process in items:
            if process.poll() is not None:
                self.forget(scan_id)
                continue
            # ⚠️ 自研引擎的句柄是线程（`terminable=False`）—— 拿它的 pid
            # （线程标识）去杀会打到不相干的进程。对这类句柄只能请求停止。
            if not is_terminable(process):
                stopper = getattr(process, "request_stop", None)
                if callable(stopper):
                    stopper()
                self.forget(scan_id)
                continue
            terminate_tree(
                process.pid,
                grace_seconds=get_settings().scan_abort_grace_seconds,
                log=logger.warning,
            )
            self.forget(scan_id)
            # 状态也要落库，否则重启后孤儿回收会把它判成"结果不可知"。
            session = get_session_factory()()
            try:
                scan = session.get(Scan, scan_id)
                if scan is not None and scan.status == ScanStatus.RUNNING.value:
                    moment = now_utc()
                    scan.status = ScanStatus.ABORTED.value
                    scan.finished_at = moment
                    scan.duration_sec = seconds_between(scan.started_at, moment)
                    scan.failure_reason = reason
                    # ★ 终止方是**服务**，不是用户（任务 11）。
                    #
                    # 这条路径只由 `stop()` 触发（进程退出、热重载、systemd
                    # 重启），而 strix 那边会把它记成
                    # `Scan ... interrupted by the user` —— 它只能看到
                    # "父进程叫我停"，看不到是谁叫的。
                    #
                    # 两边的措辞不一致会让排查时以为是两种原因。写清 `system`
                    # 之后，界面上的「终止方」才能回答那个真问题：
                    # **是我停的，还是服务重启顺手带走的**。
                    scan.terminated_by = "system"
                    scan.pid = None
                    session.commit()
            except Exception:  # noqa: BLE001
                session.rollback()
                logger.exception("停止时收尾扫描 %s 失败", scan_id)
            finally:
                session.close()

    # ══════════════════════════════════════════════════════
    # 孤儿回收
    # ══════════════════════════════════════════════════════

    def recover_orphans(self) -> int:
        """启动时清理上一次进程留下的 `running` 扫描。

        ★ 这是**必须做**的一步，而且两种情况的处理**不同**：

        | 情况 | 处理 | 为什么 |
        | ---- | ---- | ------ |
        | pid 还活着 | **终止它**，置 `aborted` | 我们丢了 Popen 句柄，拿不到退出码、也不知道它跑到哪了。一个**没人能终止的攻击进程**比一次白跑的扫描危险得多。 |
        | pid 已经不在 | 置 `failed`，原因写"结果不可知" | 我们不知道它成功还是失败。报 `completed` 是编造，报 `aborted` 也不对 —— 用户没终止过它。 |

        两种都**不猜**。用户看到一条明确的、能解释的记录，
        比看到一条看起来正常但结论可能是错的记录要好。
        """
        recovered = 0
        session = get_session_factory()()
        try:
            # ★ 先把自己**正在跟踪**的进程排除掉。
            #
            # 没有这一步就会有一个很糟的 bug：`recover_orphans()` 把所有
            # `running` 的扫描都当成"上一次运行的遗留"，于是它对**自己刚启动的**
            # 扫描下手 —— 明明跑得好好的，被自己杀掉了。
            # 这个 bug 是写测试时抓到的（"没有孤儿时应该什么都不做"那条报出了 1）。
            with self._lock:
                tracked = {
                    scan_id
                    for scan_id, process in self._procs.items()
                    if process.poll() is None
                }

            stale = [
                scan
                for scan in session.execute(
                    select(Scan)
                    .where(Scan.status == ScanStatus.RUNNING.value)
                    # ★ CTF2 的壳**必须排除**：它的"没有进程句柄"是正常的
                    #   （它压根不是子进程，而是 CTF2 的一个线程）。
                    #   不排除就会在每次重启时把它判成 failed + "进程已不在，
                    #   结果不可知" —— 实测已发生 5 次，其中有的题其实解出来了，
                    #   于是扫描列表说失败而题目说已解出。
                    #   它的状态由 CTF2 自己的 `recover_orphans` 负责。
                    #   见 `_not_ctf2_shell`。
                    .where(_not_ctf2_shell())
                ).scalars()
                if scan.id not in tracked
            ]
            if not stale:
                return 0

            moment = now_utc()
            for scan in stale:
                # ★ 趁现在把 strix 的 run 标识记下来。
                #
                # 平时这件事由 `_reap_finished()` 做，但它遍历的是**内存里的
                # 进程句柄表** `self._procs` —— 服务重启后那张表是空的，
                # 所以走的正是这条路（"被重启打断的扫描"）时，
                # 那边**永远不会为它执行**。
                #
                # 不补的后果很具体，而且是实测撞到的：
                # 磁盘上 `strix_runs/<run_id>/` 里 run.json 与 agents.json
                # 一应俱全、本该能续跑，而 `can_resume` 因为 `run_id` 是 NULL
                # 直接判否 —— 用户点「续跑」稳定得到 409，
                # 而那句错误信息说"会话文件已不在"，与事实**正好相反**。
                #
                # 这恰恰是最想续跑的场景：跑十几分钟被重启带走。
                #
                # 放在循环最前面（而不是只在 `failed` 分支里）：`aborted`
                # 那条分支同样可续，两边都需要这个标识。
                if not scan.run_id and scan.run_dir:
                    try:
                        scan.run_id = get_runner().detect_run_id(Path(scan.run_dir))
                    except Exception:  # noqa: BLE001
                        # 拿不到就留空 —— 它不该影响"这次扫描已中断"这个判定，
                        # 那才是这条记录最重要的事实。
                        logger.warning(
                            "读取扫描 %s 的 run 标识失败（忽略）", scan.id, exc_info=True
                        )

                alive = is_alive(scan.pid)
                if alive:
                    gone = terminate_tree(
                        scan.pid or 0,
                        grace_seconds=get_settings().scan_abort_grace_seconds,
                        log=logger.warning,
                    )
                    scan.status = ScanStatus.ABORTED.value
                    scan.failure_reason = (
                        "服务重启，无法继续跟踪这次扫描的进程，已将其终止。"
                        + ("" if gone else " ⚠️ 进程可能未被终止，请手工检查。")
                    )
                else:
                    scan.status = ScanStatus.FAILED.value
                    scan.failure_reason = "服务重启，扫描进程已不在，本次结果不可知。"
                scan.finished_at = moment
                scan.duration_sec = seconds_between(scan.started_at, moment)
                scan.pid = None
                # ★ 同样是**服务**导致的（任务 11）——这条路径正是
                # "上一次运行没退干净，重启后回收"。没有它，界面上
                # 「终止方」对这类记录又是空的，而它恰恰是最需要
                # 说清的一种："不是我停的，是重启带走的"。
                scan.terminated_by = "system"
                session.commit()
                service.emit_lifecycle_event(
                    session,
                    scan,
                    event_type="blocked" if alive else "failed",
                    summary=f"扫描中断：{scan.failure_reason}",
                    detail=(
                        {"kind": "blocked", "attempted": f"继续扫描 {scan.target}", "policy": "服务重启", "reason": scan.failure_reason}
                        if alive
                        else {"kind": "failed", "attempted": f"扫描 {scan.target}", "error": scan.failure_reason}
                    ),
                    phase="report",
                )
                recovered += 1
            logger.warning("回收了 %d 条上一次运行遗留的 running 扫描", recovered)
        except Exception:  # noqa: BLE001
            session.rollback()
            logger.exception("孤儿回收失败")
        finally:
            session.close()
        return recovered


# 进程内单例。调度器是**进程级**的东西（它持有 Popen 句柄），
# 所以不能按请求构造 —— 那样每次请求都会有一份空的 _procs。
_scheduler: ScanScheduler | None = None
_scheduler_lock = threading.Lock()


def get_scheduler() -> ScanScheduler:
    global _scheduler
    with _scheduler_lock:
        if _scheduler is None:
            _scheduler = ScanScheduler()
        return _scheduler


def reset_scheduler() -> None:
    """给测试用：停掉并丢掉单例（连带它持有的进程句柄）。"""
    global _scheduler
    with _scheduler_lock:
        if _scheduler is not None:
            _scheduler.stop(timeout=5.0)
        _scheduler = None