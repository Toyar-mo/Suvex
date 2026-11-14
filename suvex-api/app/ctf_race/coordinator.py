"""Layer3：赛事自动托管 —— poller + coordinator 守护循环。

## 它做什么

一场比赛 = 一堆题（`RemoteChallenge`，分批开放）。本模块持续轮询题库，
把**新开放且未解**的题丢进「题目池」，对每道题跑一个 `ChallengeSwarm`
（多模型横向赛跑，见 `swarm.py`），并发上限可配。任一模型解出并由平台
确认 → 该题标记 done、取消它的其它模型、把 flag 记进战绩。

## 与 ctf-agent 的对应

* `poller.py`（ctf-agent `CTFdPoller`）→ 本模块 `PollResult` + `poll_once()`；
* `coordinator_loop.py`（共享事件循环 / 自动开关 swarm）→ 本模块 `MatchCoordinator`。

## 刻意不抄 ctf-agent 的地方

* **不抄「Cost is not a concern / NEVER kill a swarm」**：本模块有
  `max_concurrent`（并发闸）、`max_budget_usd`（总预算闸，超了不再开新题）、
  `poll_interval`（不无脑高频轮询）。确定性失败必须能收场。
* **不自动 kill 正在跑的 swarm**：只在整场停（`stop()`）或题被平台标 solved
  时取消对应 swarm —— 这是有理由的干预，不是 prompt 强断言。

## 可测性

所有 I/O（拉题库、读环境、提交、跑一道题）都通过**注入的回调**完成：
`list_challenges()`、`make_swarm(challenge) -> ChallengeSwarm`。
真实生产由 `competition_racer.py` 提供这些回调；单测注入假的即可。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.ctf_race.swarm import ChallengeSwarm, SwarmResult

logger = logging.getLogger(__name__)

#: 拉题库回调：返回当前所有题的快照（每项至少要有 remote_id / title / solved / is_open）。
ListChallengesFn = Callable[[], Awaitable[list[Any]]]
#: 造 swarm 回调：给一道题，返回一个待 `run()` 的 `ChallengeSwarm`。
MakeSwarmFn = Callable[[Any], ChallengeSwarm]


class ChallengeStage(StrEnum):
    """一道题在整场里的生命周期阶段。"""

    PENDING = "pending"      # 在池里，等并发名额
    RUNNING = "running"      # 正在跑 swarm
    SOLVED = "solved"        # 平台确认解出
    FAILED = "failed"        # swarm 跑完没解出（不代表放弃，下轮 poll 还会重试）
    SKIPPED = "skipped"      # 平台已解 / 未开放


@dataclass
class ChallengeState:
    remote_id: str
    title: str = ""
    #: 平台给的分类名（WEB / PWN / MISC / RE / CR …）。
    #:
    #: ★ v1.73：它决定**并发出发的顺序**，不只是显示用。见 `_next_pending`。
    category: str = ""
    #: 附件总字节数；`None` = 平台没给且本地还没下载，不能当 0。
    #: 小的先解，未知的排最后，避免 353MB 的包占住并发名额。
    attachment_bytes: int | None = None
    stage: ChallengeStage = ChallengeStage.PENDING
    winner_model: str | None = None
    flag: str | None = None
    attempts: int = 0
    last_error: str | None = None


@dataclass
class MatchReport:
    """整场的最终战报。"""

    total: int = 0
    solved: int = 0
    failed: int = 0
    skipped: int = 0
    by_challenge: dict[str, ChallengeState] = field(default_factory=dict)
    rounds: int = 0
    stopped_reason: str = ""
    elapsed_seconds: float = 0.0


def _field(obj: Any, *names: str, default: Any = None) -> Any:
    """从 dict 或对象里按多个候选名取值（兼容 dataclass 与 dict）。"""
    for n in names:
        if isinstance(obj, dict):
            if n in obj:
                return obj[n]
        elif hasattr(obj, n):
            return getattr(obj, n)
    return default


def _is_open(challenge: Any) -> bool:
    """判断一道题是否已开放。默认 True（平台没给就当作开放）。"""
    val = _field(challenge, "is_open", "isOpen", "isOpened", default=True)
    return bool(val)


def _is_solved(challenge: Any) -> bool:
    return bool(_field(challenge, "solved", "hasSolved", "isSolved", default=False))


class MatchCoordinator:
    """一场比赛的自动托管器（poller + 调度）。

    典型用法：

        coord = MatchCoordinator(
            list_challenges=fetch_all,
            make_swarm=build_swarm_for,
            max_concurrent=2,
            max_budget_usd=20.0,
        )
        report = await coord.run_until_idle()   # 到没有待跑的题为止
    """

    def __init__(
        self,
        *,
        list_challenges: ListChallengesFn,
        make_swarm: MakeSwarmFn,
        max_concurrent: int = 2,
        poll_interval: float = 5.0,
        max_budget_usd: float | None = None,
        max_rounds: int = 1000,
        cost_tracker: Any = None,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent 必须 >= 1")
        self._list = list_challenges
        self._make_swarm = make_swarm
        self._max_concurrent = max_concurrent
        self._poll_interval = poll_interval
        self._max_budget_usd = max_budget_usd
        self._max_rounds = max_rounds
        self._cost_tracker = cost_tracker

        self._states: dict[str, ChallengeState] = {}
        self._running: dict[str, asyncio.Task] = {}
        self._stop = asyncio.Event()
        self._started_at = 0.0
        self._rounds = 0
        self._stopped_reason = ""

    # ── 公共只读视图 ──────────────────────────────────────────────────
    def snapshot(self) -> dict[str, dict]:
        """给前端/日志看当前整场状态（JSON 友好）。"""
        return {
            cid: {
                "remoteId": cid,
                "title": st.title,
                "category": st.category,
                "attachmentBytes": st.attachment_bytes,
                "stage": st.stage.value,
                "winnerModel": st.winner_model,
                "flag": st.flag,
                "attempts": st.attempts,
                "lastError": st.last_error,
                "running": cid in self._running,
            }
            for cid, st in self._states.items()
        }

    def budget_exhausted(self) -> bool:
        if self._max_budget_usd is None or self._cost_tracker is None:
            return False
        try:
            total = self._cost_tracker.summary().get("totalCostUsd", 0.0)
        except Exception:  # noqa: BLE001
            return False
        return total >= self._max_budget_usd

    # ── 停止 ──────────────────────────────────────────────────────────
    def stop(self, reason: str = "手动停止") -> None:
        """请求整场停止：不再开新题，取消在跑的 swarm。"""
        self._stopped_reason = reason
        self._stop.set()
        for task in self._running.values():
            task.cancel()

    # ── 主循环 ────────────────────────────────────────────────────────
    async def run_until_idle(self) -> MatchReport:
        """跑到「没有待跑的题，且没有在跑的题」为止。"""
        self._started_at = time.monotonic()
        try:
            while not self._stop.is_set():
                self._rounds += 1
                if self._rounds > self._max_rounds:
                    self._stopped_reason = f"达到最大轮次 {self._max_rounds}"
                    break
                await self._poll_once()

                # 预算用尽 + 没有在跑的 → 收场（不再开新题）
                if self.budget_exhausted() and not self._running:
                    self._stopped_reason = "预算已用尽"
                    break

                # 有并发名额 + 有 PENDING 题 + 预算没爆 → 开新题
                while (
                    len(self._running) < self._max_concurrent
                    and not self.budget_exhausted()
                    and not self._stop.is_set()
                ):
                    nxt = self._next_pending()
                    if nxt is None:
                        break
                    self._launch(nxt)

                # 收掉已结束的 swarm
                await self._reap()

                # 终止条件：没有待跑也没有在跑
                has_pending = any(
                    st.stage == ChallengeStage.PENDING for st in self._states.values()
                )
                if not has_pending and not self._running:
                    if self.budget_exhausted():
                        self._stopped_reason = "预算已用尽"
                    else:
                        self._stopped_reason = self._stopped_reason or "全部题目已处理"
                    break

                await self._sleep(self._poll_interval)
        finally:
            # 收尾：取消一切残余
            for task in list(self._running.values()):
                task.cancel()
            if self._running:
                await asyncio.gather(*self._running.values(), return_exceptions=True)
                self._running.clear()
        return self._report()

    async def _sleep(self, seconds: float) -> None:
        """可被 stop 打断的 sleep（测试里常把 poll_interval 设 0）。"""
        if seconds <= 0:
            # 有在跑的题时必须真的让出一点真实时间，否则纯 sleep(0) 忙等到
            # 任务堆满轮次上限都还没跑完（0.03s 的解题在 30ms 内能空转上千轮）。
            await asyncio.sleep(0.001 if self._running else 0)
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    def _next_pending(self) -> str | None:
        """下一个该开的题：**先开"当前在跑最少的分类"**。

        ## 为什么不是"谁先来开谁"（v1.73）

        平台是按**分类分组**返回题库的（WEB 一整个组、PWN 一组 …）。按插入
        顺序开题的话，两个并发名额会连续开两道 WEB —— 一道题卡住时另一个
        名额也跟着卡在同一类上（同一类题往往用同一套工具、同一类假设）。
        这正是"分批多并行解不同类"要避免的：并行的价值在于**覆盖不同的
        攻击面**，而不是把同一类题做两遍。

        这里用稳定排序：同分类内的相对顺序不变，所以行为是可预测的。
        """
        pending = [
            (cid, st) for cid, st in self._states.items()
            if st.stage == ChallengeStage.PENDING
        ]
        if not pending:
            return None

        running_by_category: dict[str, int] = {}
        for cid in self._running:
            st = self._states.get(cid)
            if st is not None:
                running_by_category[st.category] = (
                    running_by_category.get(st.category, 0) + 1
                )

        # 小附件优先；大小未知的排最后。同大小再按“当前分类在跑的数量”
        # 分散，保留 v1.73 的跨分类覆盖。
        pending.sort(
            key=lambda item: (
                0 if item[1].attachment_bytes is not None else 1,
                item[1].attachment_bytes or 0,
                running_by_category.get(item[1].category, 0),
            )
        )
        return pending[0][0]

    def _launch(self, cid: str) -> None:
        st = self._states[cid]
        st.stage = ChallengeStage.RUNNING
        st.attempts += 1
        swarm = self._make_swarm_by_id(cid)
        task = asyncio.create_task(self._run_one(cid, swarm), name=f"swarm-{cid}")
        self._running[cid] = task

    def _make_swarm_by_id(self, cid: str) -> ChallengeSwarm:
        return self._make_swarm(self._states[cid])

    async def _run_one(self, cid: str, swarm: ChallengeSwarm) -> None:
        st = self._states[cid]
        try:
            result: SwarmResult = await swarm.run()
            if result.flag:
                st.stage = ChallengeStage.SOLVED
                st.flag = result.flag
                st.winner_model = result.winner_model
            else:
                st.stage = ChallengeStage.FAILED
                st.last_error = "swarm 未解出"
        except asyncio.CancelledError:
            st.stage = ChallengeStage.FAILED
            st.last_error = "已取消"
            raise
        except Exception as exc:  # noqa: BLE001
            st.stage = ChallengeStage.FAILED
            st.last_error = str(exc)[:300]
            logger.warning("[race] 题 %s swarm 崩溃: %s", cid, exc, exc_info=True)

    async def _reap(self) -> None:
        done = [(cid, t) for cid, t in self._running.items() if t.done()]
        for cid, task in done:
            self._running.pop(cid, None)
            exc = task.exception() if not task.cancelled() else None
            if exc is not None:
                logger.debug("[race] 题 %s 结束但带异常: %s", cid, exc)

    async def _poll_once(self) -> None:
        try:
            challenges = await self._list()
        except Exception as exc:  # noqa: BLE001 - 拉题库失败不该终结整场
            logger.warning("[race] 拉题库失败（本轮跳过）: %s", exc)
            return
        seen: set[str] = set()
        for ch in challenges:
            cid = str(_field(ch, "remote_id", "remoteId", "id", default=""))
            if not cid:
                continue
            seen.add(cid)
            st = self._states.setdefault(cid, ChallengeState(remote_id=cid))
            st.title = _field(ch, "title", default=st.title) or st.title
            st.category = _field(ch, "category", default=st.category) or st.category
            raw_bytes = _field(ch, "attachment_bytes", "attachmentBytes", default=None)
            if isinstance(raw_bytes, int) and raw_bytes >= 0:
                st.attachment_bytes = raw_bytes
            elif st.attachment_bytes is None:
                st.attachment_bytes = None
            if _is_solved(ch):
                # 平台已解（可能网页手交/别的实例解的）→ 跳过，别浪费额度
                if st.stage not in (ChallengeStage.SOLVED,):
                    st.stage = ChallengeStage.SOLVED
                continue
            if not _is_open(ch):
                if st.stage == ChallengeStage.PENDING:
                    st.stage = ChallengeStage.SKIPPED
                continue
            # 未解且开放：若之前在 SKIPPED 现在开放了 → 回到 PENDING
            if st.stage in (ChallengeStage.SKIPPED, ChallengeStage.FAILED):
                # FAILED 也重排（下一轮再试一次）；但不覆盖正在跑的/已解的
                st.stage = ChallengeStage.PENDING
            # PENDING 保持；RUNNING/SOLVED 不动

    def _report(self) -> MatchReport:
        rep = MatchReport(
            total=len(self._states),
            by_challenge=dict(self._states),
            rounds=self._rounds,
            stopped_reason=self._stopped_reason,
            elapsed_seconds=time.monotonic() - self._started_at,
        )
        for st in self._states.values():
            if st.stage == ChallengeStage.SOLVED:
                rep.solved += 1
            elif st.stage == ChallengeStage.FAILED:
                rep.failed += 1
            elif st.stage in (ChallengeStage.SKIPPED, ChallengeStage.PENDING):
                rep.skipped += 1
        return rep


__all__ = [
    "ChallengeStage",
    "ChallengeState",
    "ListChallengesFn",
    "MakeSwarmFn",
    "MatchCoordinator",
    "MatchReport",
]
