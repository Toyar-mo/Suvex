"""`CompetitionRacer` 的接线单测（不连 DB、不起容器）。"""

from __future__ import annotations

from contextlib import contextmanager

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


# ══════════════════════════════════════════════════════════
# v1.73：靶机信息与提交闸的接线
# ══════════════════════════════════════════════════════════


async def test_build_spec_for_injects_platform_environment(monkeypatch):
    """★ 赛跑的题来自比赛平台，靶机信息在**平台侧** —— 必须注入 spec。

    不注入的后果：容器起来了、模型开跑了，但它不知道打哪个 IP/账号，
    于是报"连不上"，看起来像模型无能。
    """
    import app.kernel.runner as runner_mod
    from app.kernel import service as kernel_service
    from app.kernel.platform import RemoteEndpoint, RemoteEnvironment

    # 这个单测的 session 工厂故意 yield None（不连 DB）；让远端题库查询
    # 直接返回 None，走 `_ensure_local_challenge` 的替身。
    monkeypatch.setattr(kernel_service, "get_challenge_by_remote_id", lambda db, rid: None)

    racer = _make_racer()
    spec = runner_mod.SolveSpec(
        challenge_id="c1",
        title="T",
        category="web",
        description="d",
        profile="base",
        access_urls=(),
        attachments=(),
        llm_model="m",
        llm_api_key="k",
    )
    monkeypatch.setattr(runner_mod, "build_spec", lambda db, ch: spec)
    monkeypatch.setattr(racer, "_ensure_local_challenge", lambda db, rid: object())

    async def fake_env(remote_id):
        assert remote_id == "12891"
        return (
            RemoteEnvironment(
                endpoints=[RemoteEndpoint(expose_ips=["10.0.0.10"], ports=["80"])],
                ready=True,
            ),
            "靶机：IP 10.0.0.10",
        )

    monkeypatch.setattr(racer, "_provision_environment", fake_env)

    out = await racer._build_spec_for("12891", "m2")

    assert out.env_prompt.startswith("靶机")
    assert out.access_urls == ("http://10.0.0.10:80",)
    # 有靶机就必须放开容器网络（gobuster / nc 都靠它）
    assert out.network_isolated is False
    assert out.llm_model == "m2"


async def test_build_spec_for_without_environment_keeps_spec_untouched(monkeypatch):
    """纯附件题（平台没给靶机）：不许伪造 URL，也不许放开网络。"""
    import app.kernel.runner as runner_mod
    from app.kernel import service as kernel_service

    monkeypatch.setattr(kernel_service, "get_challenge_by_remote_id", lambda db, rid: None)

    racer = _make_racer()
    spec = runner_mod.SolveSpec(
        challenge_id="c1",
        title="T",
        category="crypto",
        description="d",
        profile="base",
        access_urls=(),
        attachments=(),
        llm_model="m",
        llm_api_key="k",
        network_isolated=True,
    )
    monkeypatch.setattr(runner_mod, "build_spec", lambda db, ch: spec)
    monkeypatch.setattr(racer, "_ensure_local_challenge", lambda db, rid: object())

    async def no_env(remote_id):
        return None, ""

    monkeypatch.setattr(racer, "_provision_environment", no_env)

    out = await racer._build_spec_for("1", "m")
    assert out.env_prompt == ""
    assert out.access_urls == ()
    assert out.network_isolated is True


def test_make_swarm_routes_submits_through_the_swarm_gate():
    """★★ adapter 的提交**必须**走 `swarm.try_submit_flag`。

    绕过它的代价：演练模式（noSubmit）照样真提交、错答没有冷却、多个模型
    重复交同一个 flag —— 每一次都在烧平台给的错误提交次数（不可再生）。
    """
    racer = _make_racer(models=("m1",))
    swarm = racer._make_swarm({"remote_id": "1"})
    adapter = swarm._solve_one
    assert adapter is not None
    assert getattr(adapter._submit_fn, "__self__", None) is swarm
    assert getattr(adapter._submit_fn, "__func__", None) is swarm.try_submit_flag.__func__


async def test_solve_one_translates_confirmed_flag_and_persists(monkeypatch):
    """`solve_one` 用一条已证明可用的链解一道题：spec→adapter→落库。

    这里把 adapter 的 `__call__` 换成直接返回一个 flag_found 结果，
    验证 ① 走的是 `_build_spec_for` ② 结果经 `_persist_single_result`
    以 `confirmed=True` 落库（即 `_advance_challenge(..., "correct", ...)`）。
    """
    from app.ctf_race.solver_base import FLAG_FOUND, SolverResult

    racer = _make_racer(models=("m1",))

    calls = {}

    async def fake_build_spec_for(remote_id, model):
        calls["spec_remote"] = remote_id
        return object()

    def fake_build_sandbox(spec):
        return None

    async def fake_translate(outcome, model):
        return outcome

    # 让 SolveOneAdapter.__call__ 返回既定结果
    import app.ctf_race.adapter as adapter_mod

    async def fake_call(self, model, *, hint):
        return SolverResult(model=model, status=FLAG_FOUND, flag="flag{win}",
                            candidate_flag="flag{win}", findings_summary="ok")

    monkeypatch.setattr(adapter_mod.SolveOneAdapter, "__call__", fake_call)
    monkeypatch.setattr(racer, "_build_spec_for", fake_build_spec_for)
    monkeypatch.setattr(racer, "_build_sandbox", fake_build_sandbox)

    async def fake_aclose():
        calls["closed"] = True
    monkeypatch.setattr(racer, "_aclose_driver", fake_aclose)

    persisted = {}

    async def fake_persist(challenge_id, *, flag, confirmed, message):
        persisted.update(challenge_id=challenge_id, flag=flag, confirmed=confirmed)

    monkeypatch.setattr(racer, "_persist_single_result", fake_persist)

    out = await racer.solve_one("12891", challenge_id="knchal_x")
    assert calls["closed"] is True
    assert out["status"] == FLAG_FOUND
    assert out["flag"] == "flag{win}"
    assert persisted["confirmed"] is True
    assert persisted["flag"] == "flag{win}"
