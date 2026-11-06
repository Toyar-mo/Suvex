"""`/ctf-race` 端点的鉴权 + 生命周期测试（不真跑赛跑）。"""

from __future__ import annotations

import asyncio

import pytest

from app.ctf_race import competition_racer as cr_mod
from app.ctf_race import router as router_mod


@pytest.fixture(autouse=True)
def _reset_race_state():
    """每个测试前后都清进程内单例，避免串味。"""
    def _clear():
        router_mod._state["racer"] = None
        router_mod._state["task"] = None
        router_mod._state["report"] = None
        router_mod._state["error"] = None

    _clear()
    yield
    _clear()


def test_status_requires_auth(client):
    r = client.get("/api/v1/ctf-race/status")
    assert r.status_code in (401, 403), r.text


def test_status_empty_when_idle(ready_client):
    r = ready_client.get("/api/v1/ctf-race/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["running"] is False
    assert body["models"] == []


def test_start_requires_scan(ready_client, viewer_client):
    # viewer 是只读，不能起赛跑
    r = viewer_client.post("/api/v1/ctf-race/start", json={"models": ["m1"]})
    assert r.status_code in (401, 403), r.text


def test_start_and_status_with_fake_run(ready_client, monkeypatch):
    """起一场假赛跑：run() 立刻返回一个 MatchReport，不连平台/容器。"""
    from app.ctf_race.coordinator import ChallengeStage, ChallengeState, MatchReport

    async def fake_run(self):
        return MatchReport(
            total=1,
            solved=1,
            failed=0,
            skipped=0,
            by_challenge={
                "a": ChallengeState(
                    remote_id="a", title="A", stage=ChallengeStage.SOLVED,
                    winner_model="m1", flag="flag{a}",
                )
            },
            rounds=1,
            stopped_reason=None,
            elapsed_seconds=0.1,
        )

    monkeypatch.setattr(cr_mod.CompetitionRacer, "run", fake_run)

    r = ready_client.post("/api/v1/ctf-race/start", json={"models": ["m1"], "maxConcurrent": 1})
    assert r.status_code == 200, r.text

    # TestClient 会同步驱动事件循环，instant 的假 run 可能在返回前就跑完了；
    # 只需确认最终状态是「已收尾 + 有 report」。
    st = ready_client.get("/api/v1/ctf-race/status").json()
    assert st["running"] is False
    assert st["report"] is not None
    assert st["report"]["solved"] == 1
    assert st["report"]["by_challenge"]["a"]["flag"] == "flag{a}"


def test_start_twice_conflicts(ready_client, monkeypatch):
    """一场在跑时再 start 应 409。"""
    async def slow_run(self):
        await asyncio.sleep(5)
        from app.ctf_race.coordinator import MatchReport
        return MatchReport(total=0, solved=0, failed=0, skipped=0)

    monkeypatch.setattr(cr_mod.CompetitionRacer, "run", slow_run)

    r1 = ready_client.post("/api/v1/ctf-race/start", json={"models": ["m1"]})
    assert r1.status_code == 200, r1.text
    r2 = ready_client.post("/api/v1/ctf-race/start", json={"models": ["m1"]})
    assert r2.status_code == 409, r2.text
    body = r2.json()
    # 错误体里应带 RACE_ALREADY_RUNNING 语义
    assert "RACE_ALREADY_RUNNING" in str(body) or "赛事" in str(body) or "跑" in str(body)
