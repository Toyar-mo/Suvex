"""Layer2 `SolveOneAdapter` 的翻译逻辑单测（不起真容器）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

from app.ctf_race import adapter as ad
from app.ctf_race.solver_base import ERROR, FLAG_FOUND, GAVE_UP, QUOTA_ERROR


# ── 假对象 ────────────────────────────────────────────────────────────
@dataclass
class FakeCandidate:
    value: str
    flag: str | None = None


@dataclass
class FakeUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class FakeOutcome:
    candidates: list = field(default_factory=list)
    turns: int = 0
    submitted: list = field(default_factory=list)
    rejected: list = field(default_factory=list)
    error: str | None = None
    transcript: str = ""
    usages: list = field(default_factory=list)
    llm_calls: int = 0
    saw_tool_output: bool = False


class FakeSpec:
    def __init__(self, model: str):
        self.llm_model = model
        self.llm_api_key = "k"
        self.llm_api_base = "http://x"
        self.llm_label = ""
        self.llm_kind = "cloud"
        self.brief = ""

class FakeSandbox:
    def __init__(self, spec):
        self.spec = spec
        self.stopped = False

    def stop(self):
        self.stopped = True


def _adapter(outcome, *, submit=None, sandboxes=None, tracker=None):
    def spec_factory(model):
        return FakeSpec(model)

    def sandbox_factory(spec):
        sb = FakeSandbox(spec)
        if sandboxes is not None:
            sandboxes.append(sb)
        return sb

    async def runner(spec, *, sandbox, gateway, system_prompt):
        return outcome

    return ad.SolveOneAdapter(
        spec_factory=spec_factory,
        sandbox_factory=sandbox_factory,
        submit_fn=submit,
        runner=runner,
        cost_tracker=tracker,
        system_prompt_builder=lambda spec: "sys",
    )


# ── 测试 ──────────────────────────────────────────────────────────────
async def test_translate_confirmed_candidate_is_flag_found():
    outcome = FakeOutcome(
        candidates=[FakeCandidate(value="flag{ok}")],
        turns=5,
        usages=[(1, "m", "l", FakeUsage(100, 50)), (2, "m", "l", FakeUsage(30, 20))],
    )

    async def submit(flag, model):
        return ("CORRECT", True)

    sandboxes: list = []
    result = await _adapter(outcome, submit=submit, sandboxes=sandboxes)("m", hint=None)
    assert result.status == FLAG_FOUND
    assert result.flag == "flag{ok}"
    assert result.candidate_flag == "flag{ok}"
    assert result.step_count == 5
    # 沙箱被停
    assert sandboxes and sandboxes[0].stopped


async def test_translate_rejected_candidate_is_gave_up():
    outcome = FakeOutcome(candidates=[FakeCandidate(value="flag{bad}")], turns=3, rejected=["flag{bad}"])

    async def submit(flag, model):
        return ("INCORRECT", False)

    result = await _adapter(outcome, submit=submit)("m", hint=None)
    assert result.status == GAVE_UP
    assert result.flag is None
    assert result.candidate_flag == "flag{bad}"


async def test_translate_quota_error():
    outcome = FakeOutcome(error="HTTP 429 rate limit exceeded", turns=1)
    result = await _adapter(outcome)("m", hint=None)
    assert result.status == QUOTA_ERROR


async def test_translate_other_error():
    outcome = FakeOutcome(error="docker exploded", turns=1)
    result = await _adapter(outcome)("m", hint=None)
    assert result.status == ERROR


async def test_translate_plain_gave_up_and_usage_accumulated():
    tracker_log = {}

    class FakeTracker:
        def record(self, model, *, input_tokens, output_tokens, cache_read_tokens=0):
            tracker_log.update(model=model, i=input_tokens, o=output_tokens)
            return 0.25

    outcome = FakeOutcome(
        turns=4,
        usages=[(1, "m", "l", FakeUsage(80, 40)), (2, "m", "l", FakeUsage(20, 10))],
    )
    result = await _adapter(outcome, tracker=FakeTracker())("m", hint=None)
    assert result.status == GAVE_UP
    assert result.cost_usd == 0.25
    assert tracker_log == {"model": "m", "i": 100, "o": 50}


async def test_hint_appended_to_brief():
    captured = {}

    def spec_factory(model):
        spec = FakeSpec(model)
        spec.brief = "原始任务"
        return spec

    def sandbox_factory(spec):
        captured["brief"] = spec.brief
        return FakeSandbox(spec)

    async def runner(spec, *, sandbox, gateway, system_prompt):
        return FakeOutcome(turns=1)

    a = ad.SolveOneAdapter(
        spec_factory=spec_factory,
        sandbox_factory=sandbox_factory,
        runner=runner,
    )
    await a("m", hint="别的模型试过 base64 无果")
    assert "原始任务" in captured["brief"]
    assert "base64" in captured["brief"]


async def test_exception_in_solve_returns_error_and_stops_sandbox():
    def spec_factory(model):
        return FakeSpec(model)

    stopped = {}

    def sandbox_factory(spec):
        sb = FakeSandbox(spec)
        stopped["sb"] = sb
        return sb

    async def runner(spec, *, sandbox, gateway, system_prompt):
        raise RuntimeError("boom")

    a = ad.SolveOneAdapter(spec_factory=spec_factory, sandbox_factory=sandbox_factory, runner=runner)
    result = await a("m", hint=None)
    assert result.status == ERROR
    assert "boom" in result.findings_summary
    assert stopped["sb"].stopped


async def test_extract_candidate_falls_back_to_submitted():
    outcome = FakeOutcome(candidates=[], submitted=["old", "flag{last}"], turns=2)
    result = await _adapter(outcome)("m", hint=None)
    assert result.candidate_flag == "flag{last}"
