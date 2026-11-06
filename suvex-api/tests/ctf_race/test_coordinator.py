"""Layer3 `MatchCoordinator` 的调度语义单测（假 swarm，不起容器）。"""

from __future__ import annotations

import asyncio

import pytest

from app.ctf_race.coordinator import (
    ChallengeStage,
    MatchCoordinator,
)
from app.ctf_race.swarm import SwarmResult


# ── 假 swarm / 假题 ───────────────────────────────────────────────────
class FakeSwarm:
    """按预设剧本返回一个 SwarmResult；可选 sleep 模拟耗时。"""

    def __init__(self, *, flag=None, delay=0.0, raise_exc=None):
        self._flag = flag
        self._delay = delay
        self._raise = raise_exc
        self.cancelled = False
        self.ran = False

    async def run(self) -> SwarmResult:
        self.ran = True
        try:
            if self._delay:
                await asyncio.sleep(self._delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self._raise:
            raise self._raise
        return SwarmResult(
            challenge="c",
            winner_model="m" if self._flag else None,
            flag=self._flag,
        )


def _ch(cid, *, solved=False, open_=True):
    return {"remote_id": cid, "title": f"题{cid}", "solved": solved, "is_open": open_}


# ── 测试 ──────────────────────────────────────────────────────────────
async def test_solves_all_open_challenges():
    made: dict[str, FakeSwarm] = {}

    async def list_fn():
        return [_ch("a"), _ch("b")]

    def make_fn(state):
        sw = FakeSwarm(flag=f"flag{{{state.remote_id}}}")
        made[state.remote_id] = sw
        return sw

    coord = MatchCoordinator(
        list_challenges=list_fn, make_swarm=make_fn, max_concurrent=2, poll_interval=0
    )
    report = await coord.run_until_idle()
    assert report.solved == 2
    assert report.by_challenge["a"].stage == ChallengeStage.SOLVED
    assert report.by_challenge["a"].flag == "flag{a}"
    assert made["a"].ran and made["b"].ran


async def test_skips_already_solved_and_unopened():
    async def list_fn():
        return [_ch("s", solved=True), _ch("u", open_=False), _ch("n")]

    def make_fn(state):
        return FakeSwarm(flag="flag{n}")

    coord = MatchCoordinator(
        list_challenges=list_fn, make_swarm=make_fn, max_concurrent=2, poll_interval=0
    )
    report = await coord.run_until_idle()
    # s 已解、u 未开放 → 只有 n 真跑
    assert report.by_challenge["s"].stage == ChallengeStage.SOLVED
    assert report.by_challenge["u"].stage == ChallengeStage.SKIPPED
    assert report.by_challenge["n"].stage == ChallengeStage.SOLVED
    assert report.solved == 2  # s(平台已解) + n


async def test_max_concurrent_respected():
    active = {"now": 0, "peak": 0}
    made = {}

    async def list_fn():
        return [_ch(x) for x in "abcd"]

    def make_fn(state):
        class SlowSwarm:
            async def run(self_inner):
                active["now"] += 1
                active["peak"] = max(active["peak"], active["now"])
                try:
                    await asyncio.sleep(0.03)
                except asyncio.CancelledError:
                    active["now"] -= 1
                    raise
                active["now"] -= 1
                return SwarmResult(challenge="c", winner_model="m", flag=f"flag{{{state.remote_id}}}")

        sw = SlowSwarm()
        made[state.remote_id] = sw
        return sw

    coord = MatchCoordinator(
        list_challenges=list_fn, make_swarm=make_fn, max_concurrent=2, poll_interval=0
    )
    report = await coord.run_until_idle()
    assert report.solved == 4
    assert active["peak"] <= 2, f"并发峰值 {active['peak']} 超上限 2"


async def test_failed_challenge_retried_then_gives_progress():
    """一次失败 → 下一轮 poll 重排 → 第二次成功。"""
    attempts = {"a": 0}

    async def list_fn():
        return [_ch("a")]

    def make_fn(state):
        attempts["a"] += 1
        # 第一次失败，第二次成功
        if attempts["a"] == 1:
            return FakeSwarm(flag=None)
        return FakeSwarm(flag="flag{a}")

    calls = {"n": 0}

    # 用一个会「阻塞一次」的 wrapper 保证第一次失败后能进下一轮 poll
    coord = MatchCoordinator(
        list_challenges=list_fn, make_swarm=make_fn, max_concurrent=1, poll_interval=0
    )
    report = await coord.run_until_idle()
    # 由于 poll_interval=0 且循环重排失败题，最终应解出
    assert report.by_challenge["a"].stage == ChallengeStage.SOLVED
    assert attempts["a"] >= 2


async def test_swarm_exception_marks_failed_not_crash():
    async def list_fn():
        return [_ch("boom")]

    def make_fn(state):
        return FakeSwarm(raise_exc=RuntimeError("容器炸了"))

    coord = MatchCoordinator(
        list_challenges=list_fn, make_swarm=make_fn, max_concurrent=1, poll_interval=0,
        max_rounds=3,
    )
    report = await coord.run_until_idle()
    st = report.by_challenge["boom"]
    assert st.stage == ChallengeStage.FAILED
    assert "容器炸了" in (st.last_error or "")
    assert report.stopped_reason  # 有终止原因


async def test_budget_gate_stops_launching():
    class FakeTracker:
        def __init__(self):
            self.spent = 0.0

        def summary(self):
            return {"totalCostUsd": self.spent}

    tracker = FakeTracker()
    launched = []

    async def list_fn():
        return [_ch(x) for x in "abc"]

    def make_fn(state):
        launched.append(state.remote_id)
        tracker.spent += 10.0  # 每开一题就烧 10 块
        return FakeSwarm(flag=None)

    coord = MatchCoordinator(
        list_challenges=list_fn, make_swarm=make_fn, max_concurrent=1,
        poll_interval=0, max_budget_usd=15.0, cost_tracker=tracker,
        max_rounds=5,
    )
    report = await coord.run_until_idle()
    # 预算 15，每次 10 → 最多开 2 次（第一次后 10<15 可再开，第二次后 20>=15 停）
    assert len(launched) <= 2
    assert "预算" in report.stopped_reason


async def test_stop_cancels_running():
    cancelled = {"v": False}

    async def list_fn():
        return [_ch("a")]

    def make_fn(state):
        class HangSwarm:
            async def run(self_inner):
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    cancelled["v"] = True
                    raise
                return SwarmResult(challenge="c")

        return HangSwarm()

    coord = MatchCoordinator(
        list_challenges=list_fn, make_swarm=make_fn, max_concurrent=1, poll_interval=0.01
    )

    async def driver():
        task = asyncio.create_task(coord.run_until_idle())
        # 等它真的开跑
        for _ in range(50):
            await asyncio.sleep(0.01)
            if coord._running:
                break
        coord.stop("测试停止")
        return await task

    report = await driver()
    assert cancelled["v"] is True
    assert "测试停止" in report.stopped_reason


async def test_snapshot_shape():
    async def list_fn():
        return [_ch("a", solved=True), _ch("b")]

    def make_fn(state):
        return FakeSwarm(flag=None)

    coord = MatchCoordinator(
        list_challenges=list_fn, make_swarm=make_fn, max_concurrent=1, poll_interval=0,
        max_rounds=1,
    )
    async def quick():
        await coord._poll_once()
    await quick()
    snap = coord.snapshot()
    assert "a" in snap and snap["a"]["stage"] == "solved"
    assert snap["a"]["running"] is False
