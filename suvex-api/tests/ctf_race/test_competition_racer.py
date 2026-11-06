"""`CompetitionRacer` 的接线单测（不连 DB、不起容器）。"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from app.ctf_race import competition_racer as cr
from app.ctf_race.swarm import SwarmResult


@contextmanager
def _fake_session():
    yield None


def _make_racer(**kw):
    return cr.CompetitionRacer(_fake_session, **kw)


async def test_submit_maps_accepted_solved(monkeypatch):
    racer = _make_racer()

    class FakeOutcome:
        accepted = True
        is_solved = True
        message = "ok"

    async def fake_submit(db, remote_id, answer):
        assert answer == "abc"  # strip_flag_shell 后
        return FakeOutcome()

    import app.chat.competition as comp
    monkeypatch.setattr(comp, "submit", fake_submit)
    monkeypatch.setattr(comp, "strip_flag_shell", lambda v: "abc")

    disp, ok = await racer._submit("r1", "flag{abc}", "m")
    assert ok is True
    assert "CORRECT" in disp


async def test_submit_not_accepted_does_not_count(monkeypatch):
    racer = _make_racer()

    class FakeOutcome:
        accepted = False
        is_solved = False
        message = "rate limited"

    async def fake_submit(db, remote_id, answer):
        return FakeOutcome()

    import app.chat.competition as comp
    monkeypatch.setattr(comp, "submit", fake_submit)
    monkeypatch.setattr(comp, "strip_flag_shell", lambda v: v)

    disp, ok = await racer._submit("r1", "flag{x}", "m")
    assert ok is False
    assert "未受理" in disp


async def test_run_solves_via_fake_swarm(monkeypatch):
    racer = _make_racer(models=("m1",), max_concurrent=1, poll_interval=0)

    async def fake_list():
        return [{"remote_id": "a", "title": "A", "solved": False, "is_open": True}]

    class FakeSwarm:
        async def run(self_inner):
            return SwarmResult(challenge="a", winner_model="m1", flag="flag{a}")

    def fake_make_swarm(state):
        return FakeSwarm()

    monkeypatch.setattr(racer, "_list_challenges", fake_list)
    monkeypatch.setattr(racer, "_make_swarm", fake_make_swarm)

    report = await racer.run()
    assert report.solved == 1
    assert report.by_challenge["a"].flag == "flag{a}"
    # 跑完后 snapshot 里 running 应回 False
    snap = racer.snapshot()
    assert snap["running"] is False


def test_snapshot_before_run():
    racer = _make_racer(models=("m1", "m2"))
    snap = racer.snapshot()
    assert snap["models"] == ["m1", "m2"]
    assert snap["running"] is False


def test_stop_without_run_is_safe():
    racer = _make_racer()
    racer.stop("提前停止")  # 不该抛
