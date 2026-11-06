"""`app.ctf_race` 核心编排层单测（脱离 Docker/DB）。

覆盖：solver_base 判据、message_bus、loop_detect、cost_tracker、swarm
（多模型首胜 / bump 循环 / 提交冷却去重 / 连续 error 弃 / broken 不 bump）。
"""

from __future__ import annotations

import asyncio

import pytest

from app.ctf_race import cost_tracker as ct
from app.ctf_race import loop_detect as ld
from app.ctf_race import message_bus as mb
from app.ctf_race import solver_base as sb
from app.ctf_race import swarm as sw

# 项目配置 asyncio_mode="auto"，async 测试自动识别，无需 pytestmark。


# ── solver_base ────────────────────────────────────────────────────────
def test_solver_result_win_and_broken():
    r = sb.SolverResult(model="m", status=sb.FLAG_FOUND, flag="flag{x}", step_count=3, cost_usd=0.1)
    assert r.is_win and not r.is_broken
    g = sb.SolverResult(model="m", status=sb.GAVE_UP, step_count=0, cost_usd=0.0)
    assert not g.is_win and g.is_broken
    # flag 非空但状态不是 flag_found → 不算赢
    fake = sb.SolverResult(model="m", status=sb.GAVE_UP, flag="flag{y}", step_count=2, cost_usd=0.2)
    assert not fake.is_win


# ── message_bus ────────────────────────────────────────────────────────
async def test_message_bus_post_check_cursor():
    bus = mb.ChallengeMessageBus()
    await bus.post("a", "发现 AES 密钥在 .data")
    await bus.post("b", "试过 rot13 无效")
    # a 只看别人的
    a_unread = await bus.check("a")
    assert [f.model for f in a_unread] == ["b"]
    # 再取一次为空（cursor 已推进）
    assert await bus.check("a") == []
    # b 看 a 的那条
    await bus.post("a", "新的线索")
    b_unread = await bus.check("b")
    assert {f.model for f in b_unread} == {"a"}
    txt = bus.format_unread(b_unread)
    assert "其它模型" in txt


async def test_message_bus_empty_post_ignored_and_trim():
    bus = mb.ChallengeMessageBus()
    await bus.post("a", "   ")
    assert bus.findings == []
    # 超过 MAX_FINDINGS 会削旧并修 cursor
    for i in range(mb.MAX_FINDINGS + 10):
        await bus.post("x" if i % 2 else "y", f"f{i}")
    assert len(bus.findings) == mb.MAX_FINDINGS


# ── loop_detect ────────────────────────────────────────────────────────
def test_loop_detector_warn_then_break():
    d = ld.LoopDetector(warn_threshold=3, break_threshold=5, window=12)
    args = {"command": "cat flag"}
    assert d.check("run_shell", args) is None
    assert d.check("run_shell", args) is None
    assert d.check("run_shell", args) == "warn"      # 3 次
    assert d.check("run_shell", args) == "warn"      # 4 次仍 warn
    assert d.check("run_shell", args) == "break"     # 5 次 break
    # 换个命令就重置计数
    assert d.check("run_shell", {"command": "ls"}) is None


def test_loop_detector_bad_thresholds():
    with pytest.raises(ValueError):
        ld.LoopDetector(warn_threshold=5, break_threshold=3)


# ── cost_tracker ───────────────────────────────────────────────────────
def test_cost_tracker_pricing_match_and_unknown():
    tracker = ct.CostTracker()
    # 精确/子串匹配
    c1 = tracker.record("deepseek-v4.1-flash", input_tokens=1_000_000, output_tokens=0)
    assert c1 == pytest.approx(0.10, abs=1e-9)
    # 带前缀也能匹配
    c2 = tracker.record("openai/deepseek-v4.1-flash", input_tokens=0, output_tokens=1_000_000)
    assert c2 == pytest.approx(0.40, abs=1e-9)
    # 未知模型记 0 并登记
    c3 = tracker.record("mystery-model", input_tokens=1000, output_tokens=1000)
    assert c3 == 0.0
    assert "mystery-model" in tracker.unknown_models
    summ = tracker.summary()
    assert "deepseek-v4.1-flash" in summ["models"]
    assert summ["unknownPricedModels"] == ["mystery-model"]
    # 缓存命中率：cached 计入 cheaper 档
    tracker2 = ct.CostTracker()
    tracker2.record("deepseek-v4.1-flash", input_tokens=1_000_000, cache_read_tokens=1_000_000, output_tokens=0)
    # 全缓存 → 0.02
    assert tracker2.by_model["deepseek-v4.1-flash"].cost_usd == pytest.approx(0.02, abs=1e-9)
    assert "100% hit" in tracker2.format_usage("deepseek-v4.1-flash")


# ── swarm ──────────────────────────────────────────────────────────────
async def test_swarm_first_win_cancels_others():
    """一个模型很快返回 flag_found，其它慢模型被取消。"""
    cancel_seen = {"slow_cancelled": False}

    async def solve_one(model, *, hint):
        if model == "fast":
            await asyncio.sleep(0.01)
            return sb.SolverResult(model=model, status=sb.FLAG_FOUND, flag="flag{win}", step_count=5, cost_usd=0.1)
        # slow：长睡眠，应被取消
        try:
            await asyncio.sleep(5)
            return sb.SolverResult(model=model, status=sb.GAVE_UP, step_count=3, cost_usd=0.1)
        except asyncio.CancelledError:
            cancel_seen["slow_cancelled"] = True
            raise

    swarm = sw.ChallengeSwarm(
        challenge="chal", models=["fast", "slow"], solve_one=solve_one,
        config=sw.SwarmConfig(models=["fast", "slow"]),
    )
    res = await swarm.run()
    assert res.winner_model == "fast"
    assert res.flag == "flag{win}"
    assert cancel_seen["slow_cancelled"] is True


async def test_swarm_bump_with_sibling_insights():
    """某模型先 gave_up 且产出洞察，bump 时 hint 里能看到兄弟洞察，第二次成功。"""
    calls = {"m1": 0, "m2": 0}
    hints = {}

    async def solve_one(model, *, hint):
        hints.setdefault(model, []).append(hint)
        calls[model] += 1
        if model == "m1":
            # 第一次给出一份洞察然后 gave_up；先睡一小会让 m2 先 post 洞察，
            # 保证 m1 bump 时能读到兄弟洞察（避免时序竞争）。
            if calls["m1"] == 1:
                await asyncio.sleep(0.02)
                return sb.SolverResult(model=model, status=sb.GAVE_UP, findings_summary="m1: 密钥可能是 16 位", step_count=2, cost_usd=0.05)
            return sb.SolverResult(model="m1", status=sb.FLAG_FOUND, flag="flag{bumped}", step_count=4, cost_usd=0.1)
        # m2 一直 gave_up（有洞察）
        return sb.SolverResult(model=model, status=sb.GAVE_UP, findings_summary="m2: 试过 base64 无果", step_count=2, cost_usd=0.05)

    # 加速冷却：让 bump_cooldown_base 为 0
    cfg = sw.SwarmConfig(models=["m1", "m2"], max_bumps=3, bump_cooldown_base=0, bump_cooldown_max=0)
    swarm = sw.ChallengeSwarm(challenge="chal", models=["m1", "m2"], solve_one=solve_one, config=cfg)
    res = await swarm.run()
    assert res.winner_model == "m1"
    # m1 第二次调用时 hint 应包含 m2 的洞察
    second_hint = hints["m1"][1]
    assert second_hint is not None and "m2" in second_hint


async def test_swarm_max_bumps_caps_infinite_retry():
    """一直 gave_up 的模型会被 max_bumps 截停，不会无限跑。"""
    counter = {"n": 0}

    async def solve_one(model, *, hint):
        counter["n"] += 1
        return sb.SolverResult(model=model, status=sb.GAVE_UP, findings_summary="还在试", step_count=1, cost_usd=0.01)

    cfg = sw.SwarmConfig(models=["solo"], max_bumps=2, bump_cooldown_base=0, bump_cooldown_max=0)
    swarm = sw.ChallengeSwarm(challenge="chal", models=["solo"], solve_one=solve_one, config=cfg)
    res = await swarm.run()
    # 1 次初始 + 2 次 bump = 3 次
    assert counter["n"] == 3
    assert res.flag is None
    assert res.per_model["solo"].status == sb.GAVE_UP


async def test_swarm_broken_not_bumped():
    """0 步 0 花费 = broken，不 bump（只跑 1 次）。"""
    counter = {"n": 0}

    async def solve_one(model, *, hint):
        counter["n"] += 1
        return sb.SolverResult(model=model, status=sb.ERROR, step_count=0, cost_usd=0.0)

    cfg = sw.SwarmConfig(models=["solo"], max_bumps=5, bump_cooldown_base=0, bump_cooldown_max=0)
    swarm = sw.ChallengeSwarm(challenge="chal", models=["solo"], solve_one=solve_one, config=cfg)
    await swarm.run()
    assert counter["n"] == 1


async def test_swarm_consecutive_errors_give_up():
    """连续 3 次 error（非 broken）→ 放弃，即使还有 bump 预算。"""
    counter = {"n": 0}

    async def solve_one(model, *, hint):
        counter["n"] += 1
        # 有步数花费，但不是 broken
        return sb.SolverResult(model=model, status=sb.ERROR, step_count=1, cost_usd=0.01)

    cfg = sw.SwarmConfig(models=["solo"], max_bumps=10, max_consecutive_errors=3, bump_cooldown_base=0, bump_cooldown_max=0)
    swarm = sw.ChallengeSwarm(challenge="chal", models=["solo"], solve_one=solve_one, config=cfg)
    await swarm.run()
    # 1 初始 + 2 bump（第 3 次 error 达阈值时 break）→ 3 次
    assert counter["n"] == 3


# ── 提交安全 ───────────────────────────────────────────────────────────
async def test_try_submit_dedup_and_confirm(monkeypatch):
    submitted = []
    now = {"t": 1000.0}
    monkeypatch.setattr(sw.ChallengeSwarm, "_now", lambda self: now["t"])

    async def submit_fn(flag, model):
        submitted.append((flag, model))
        return ("CORRECT" if flag == "flag{right}" else "INCORRECT", flag == "flag{right}")

    swarm = sw.ChallengeSwarm(
        challenge="chal", models=["m"], solve_one=None,
        config=sw.SwarmConfig(models=["m"]), submit_fn=submit_fn,
    )
    # 第一次错误
    disp, ok = await swarm.try_submit_flag("flag{wrong}", "m")
    assert ok is False and "INCORRECT" in disp
    # 重复同一个错误 flag → 去重，不真提交
    before = len(submitted)
    disp2, ok2 = await swarm.try_submit_flag("flag{wrong}", "m")
    assert ok2 is False and "精确地尝试过" in disp2
    assert len(submitted) == before  # 没再打平台
    # 错误后进入冷却，等过冷却期再交正确的
    now["t"] += 31
    disp3, ok3 = await swarm.try_submit_flag("flag{right}", "m")
    assert ok3 is True and swarm.confirmed_flag == "flag{right}"
    # 确认后任何提交都短路
    disp4, ok4 = await swarm.try_submit_flag("flag{another}", "m")
    assert ok4 is True and "ALREADY SOLVED" in disp4


async def test_try_submit_cooldown_escalation(monkeypatch):
    """错得越多，冷却越久；冷却中提交被拒。"""
    now = {"t": 1000.0}
    monkeypatch.setattr(sw.ChallengeSwarm, "_now", lambda self: now["t"])

    async def submit_fn(flag, model):
        return ("INCORRECT", False)

    swarm = sw.ChallengeSwarm(
        challenge="chal", models=["m"], solve_one=None,
        config=sw.SwarmConfig(models=["m"]), submit_fn=submit_fn,
    )
    # 第 1 次提交（cooldown[0]=0，允许），判错 → wrong=1
    _, ok = await swarm.try_submit_flag("flag{a}", "m")
    assert ok is False
    assert swarm._submit_count["m"] == 1
    # 立刻再交不同 flag：cooldown[1]=30s，未过 → COOLDOWN
    disp, ok2 = await swarm.try_submit_flag("flag{b}", "m")
    assert ok2 is False and "COOLDOWN" in disp
    # 时间推进 31s → 可提交
    now["t"] += 31
    _, ok3 = await swarm.try_submit_flag("flag{c}", "m")
    assert ok3 is False  # 仍错，但这次真的提交了（wrong=2）


async def test_try_submit_no_submit_mode():
    """演练模式：永不确认、不真提交，但仍去重。"""
    swarm = sw.ChallengeSwarm(
        challenge="chal", models=["m"], solve_one=None,
        config=sw.SwarmConfig(models=["m"], no_submit=True),
    )
    disp, ok = await swarm.try_submit_flag("flag{any}", "m")
    assert ok is False and "演练" in disp
