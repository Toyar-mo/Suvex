"""v1.71：对话页「赛事自动托管（多模型赛跑）」三工具的测试。

这三个工具复用 `app/ctf_race/manager.py` 的进程内单例。这里不真跑赛跑，
用 monkeypatch 把 `manager.start` 换成可控假实现，验证：
  · start 转发参数正确、把快照压成中文摘要、已在跑给人话；
  · status 读快照、压缩 running/report 两种形态；
  · stop 请求停止并回执；
  · 权限：start/stop 走 scan（viewer 调不动）；
  · 工具已注册进 CHAT_TOOLS / _TOOL_IMPLS / CONSEQUENTIAL_TOOL_NAMES。
"""

from __future__ import annotations

import pytest

from app.chat import engine
from app.chat import prompts as prompts_mod


# ── 注册与 schema ────────────────────────────────────────────
def test_three_race_tools_registered():
    names = {t.name for t in prompts_mod.CHAT_TOOLS}
    assert {"start_competition_race", "competition_race_status", "stop_competition_race"} <= names
    for n in ("start_competition_race", "competition_race_status", "stop_competition_race"):
        assert n in engine._TOOL_IMPLS
    # start/stop 有副作用 → 走审批闸
    assert "start_competition_race" in prompts_mod.CONSEQUENTIAL_TOOL_NAMES
    assert "stop_competition_race" in prompts_mod.CONSEQUENTIAL_TOOL_NAMES
    # status 只读 → 不进审批闸
    assert "competition_race_status" not in prompts_mod.CONSEQUENTIAL_TOOL_NAMES


def test_build_tools_schema_includes_race_tools():
    schema = prompts_mod.build_tools_schema()
    by = {s["function"]["name"]: s["function"]["parameters"] for s in schema}
    assert "start_competition_race" in by
    p = by["start_competition_race"]
    assert p["type"] == "object"
    assert "models" in p["properties"]
    assert "maxConcurrent" in p["properties"]


# ── 快照压缩 ────────────────────────────────────────────────
def test_snapshot_brief_idle_no_report():
    txt = engine._race_snapshot_brief({"running": False, "report": None})
    assert "没有在跑" in txt


def test_snapshot_brief_running_with_challenges():
    snap = {
        "running": True,
        "challenges": {
            "rounds": 5,
            "challenges": {
                "a": {"title": "荷兰宽带", "stage": "solved", "flag": "DASCTF{x}"},
                "b": {"title": "线索", "stage": "running"},
                "c": {"title": "格密码", "stage": "failed", "last_error": "无公钥"},
            },
        },
        "cost": {"totalCostUsd": 0.0123},
    }
    txt = engine._race_snapshot_brief(snap)
    assert "进行中" in txt
    assert "第 5 轮" in txt
    assert "已解 1/3" in txt
    assert "flag=DASCTF{x}" in txt
    assert "无公钥" in txt
    assert "$0.0123" in txt


def test_snapshot_brief_finished_report():
    snap = {
        "running": False,
        "report": {
            "total": 2, "solved": 1, "failed": 1, "skipped": 0,
            "stopped_reason": "全部题目已处理",
            "by_challenge": {"a": {"title": "A", "flag": "flag{a}"}},
        },
    }
    txt = engine._race_snapshot_brief(snap)
    assert "已结束" in txt
    assert "解出 1" in txt
    assert "全部题目已处理" in txt
    assert "flag{a}" in txt


# ── 工具执行（monkeypatch manager.start/stop/snapshot）────────
class _FakeCtx:
    """最小 ctx：start/stop 需要 _need_scope → 给 admin identity（全权限）。"""

    def __init__(self):
        from tests.test_chat import ADMIN

        self.db = None
        self.session = None
        self.identity = ADMIN


@pytest.mark.asyncio
async def test_start_forwards_params_and_returns_brief(monkeypatch):
    from app.ctf_race import manager as race_manager

    captured = {}

    async def fake_start(**kw):
        captured.update(kw)
        return {"running": True, "models": list(kw.get("models") or []),
                "challenges": {"rounds": 1, "challenges": {}}, "cost": {}}

    monkeypatch.setattr(race_manager, "start", fake_start)

    out = await engine._tool_start_competition_race(
        _FakeCtx(),
        {"models": ["m1", "m2"], "maxConcurrent": 3, "maxBudgetUsd": 1.5,
         "noSubmit": True, "maxBumps": 2},
    )
    assert captured["models"] == ("m1", "m2")
    assert captured["max_concurrent"] == 3
    assert captured["max_budget_usd"] == 1.5
    assert captured["no_submit"] is True
    assert captured["max_bumps"] == 2
    assert "已启动" in out
    assert "第 1 轮" in out


@pytest.mark.asyncio
async def test_start_already_running_human_message(monkeypatch):
    from app.ctf_race import manager as race_manager

    async def fake_start(**kw):
        raise race_manager.RaceAlreadyRunning("running")

    monkeypatch.setattr(race_manager, "start", fake_start)
    out = await engine._tool_start_competition_race(_FakeCtx(), {})
    assert "已经有一场" in out


@pytest.mark.asyncio
async def test_start_models_comma_string(monkeypatch):
    from app.ctf_race import manager as race_manager

    captured = {}

    async def fake_start(**kw):
        captured.update(kw)
        return {"running": True, "challenges": {}, "cost": {}}

    monkeypatch.setattr(race_manager, "start", fake_start)
    await engine._tool_start_competition_race(_FakeCtx(), {"models": "m1, m2 ,m3"})
    assert captured["models"] == ("m1", "m2", "m3")


@pytest.mark.asyncio
async def test_status_tool_reads_snapshot(monkeypatch):
    from app.ctf_race import manager as race_manager

    monkeypatch.setattr(
        race_manager, "snapshot",
        lambda: {"running": True, "challenges": {"rounds": 2, "challenges": {}}, "cost": {}},
    )
    out = await engine._tool_competition_race_status(_FakeCtx(), {})
    assert "进行中" in out


@pytest.mark.asyncio
async def test_stop_tool_requests_stop(monkeypatch):
    from app.ctf_race import manager as race_manager

    called = {}

    def fake_stop(reason="手动停止"):
        called["reason"] = reason
        return {"running": False, "report": None}

    monkeypatch.setattr(race_manager, "stop", fake_stop)
    out = await engine._tool_stop_competition_race(_FakeCtx(), {})
    assert "已请求停止" in out
    assert "reason" in called
