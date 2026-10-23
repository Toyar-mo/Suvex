"""结束条件外置（v0.75）的回归测试。

分两层：
1. `termination.decide` 的纯函数穷举 —— 判据要能被复述给人听；
2. 走真实 `_run_agent_loop` 的端到端 —— 门必须真的能拦住 root 的 `FINAL`，
   而不只是纯函数返回 False。第 2 层是重点：v0.72 那个 `mcp_servers`
   的事故就是"纯函数测试全绿、接线没接上"。
"""

from __future__ import annotations

import json

import pytest

from app.kernel import runner, termination
from tests.kernel.test_runner import _run, _spec

# ══════════════════════════════════════════════════════════
# 1. 纯函数：判据穷举
# ══════════════════════════════════════════════════════════


class TestDecide:
    def test_allows_when_coverage_and_finding_are_enough(self) -> None:
        d = termination.decide(
            termination.Observation(
                coverage_count=7, finding_count=2, turns_used=20, total_budget=80, model_claims_done=True
            )
        )
        assert d.allow is True
        assert d.nudge == ""

    def test_blocks_on_thin_coverage(self) -> None:
        """只看了 2 个面就说扫完了 —— 这是"过早收敛"，必须拦。

        反向验证：把 `MIN_COVERAGE` 改成 2，本条立刻 FAILED。
        """
        d = termination.decide(
            termination.Observation(
                coverage_count=2, finding_count=3, turns_used=10, total_budget=80, model_claims_done=True
            )
        )
        assert d.allow is False
        assert "覆盖" in d.reason
        assert d.nudge, "拒绝收口时必须给出可执行的提示，不能只说不行"

    def test_blocks_when_no_conclusion_at_all(self) -> None:
        d = termination.decide(
            termination.Observation(
                coverage_count=9, finding_count=0, turns_used=30, total_budget=80, model_claims_done=True
            )
        )
        assert d.allow is False
        assert "结论" in d.reason

    def test_budget_exhausted_always_allows(self) -> None:
        """预算用尽 → 一律放行。

        这不是"扫完了"，是"没预算了"。硬拦只会让循环空转、结论一个字交不出来
        —— v0.73 修的就是这个死法（跑满墙钟被取消，已确认发现全丢）。
        """
        d = termination.decide(
            termination.Observation(
                coverage_count=0, finding_count=0, turns_used=80, total_budget=80, model_claims_done=True
            )
        )
        assert d.allow is True
        assert "预算" in d.reason

    def test_does_not_interfere_before_model_asks(self) -> None:
        # 模型还没写 FINAL —— 门不该提前改变循环行为。
        d = termination.decide(
            termination.Observation(
                coverage_count=0, finding_count=0, turns_used=3, total_budget=80
            )
        )
        assert d.allow is True

    def test_nudge_mentions_concrete_actions_not_internal_counters(self) -> None:
        """提示里要给动作，不给"把 coverage 刷到 5 条就能收工"这种作弊路径。"""
        d = termination.decide(
            termination.Observation(
                coverage_count=0, finding_count=0, turns_used=5, total_budget=80, model_claims_done=True
            )
        )
        assert "record_coverage" in d.nudge
        # 不能把内部阈值写进给模型看的话里
        assert "MIN_COVERAGE" not in d.nudge
        # 不能出现"覆盖 0/5 个面"这种内部计数口径（模型会去刷这个数）。
        assert "/5 个面" not in d.nudge
        assert "5 个" not in d.nudge

    @pytest.mark.parametrize("count", [0, 1, 4])
    def test_min_coverage_boundary(self, count: int) -> None:
        d = termination.decide(
            termination.Observation(
                coverage_count=count, finding_count=1, turns_used=10, total_budget=80, model_claims_done=True
            )
        )
        assert d.allow is (count >= termination.MIN_COVERAGE)

    def test_custom_min_coverage(self) -> None:
        obs = termination.Observation(
            coverage_count=2, finding_count=1, turns_used=10, total_budget=80,
            model_claims_done=True,
        )
        assert termination.decide(obs, min_coverage=2).allow is True
        assert termination.decide(obs, min_coverage=3).allow is False


# ══════════════════════════════════════════════════════════
# 2. 端到端：门真的接在编排循环里
# ══════════════════════════════════════════════════════════

_FINAL = 'FINAL: {"summary": "扫完了", "findings": [{"title": "某发现", "severity": "low"}]}'
def _coverage_action(surface: str) -> str:
    """造一条 `record_coverage` 动作。

    ⚠️ 每个 surface 必须不同：`SharedState.coverage` 是 dict，同一个 surface
    重复记只会覆盖成 1 条（`record_coverage` 的 docstring 写明"后写的覆盖前写的"）。
    用同一个 surface 记 3 次，覆盖率还是 1 —— 这个坑让本条测试第一版假绿过。
    """
    return (
        "ACTION: "
        + json.dumps(
            {
                "tool": "record_coverage",
                "args": {"surface": surface, "risk": "high", "outcome": "clean"},
            },
            ensure_ascii=False,
        )
    )


_ACTION_COVERAGE = _coverage_action("/x")


def _gateway(texts: list[str]):
    """复用 test_runner 的脚本 gateway 形状（每次调用吐下一条）。"""
    from tests.kernel.test_multiagent import _ScriptedGateway

    return _ScriptedGateway(texts)


def test_root_final_is_rejected_when_coverage_is_thin() -> None:
    """★★ root 写 FINAL 但一个面都没记 → 门必须把这一轮退回去。

    反向验证：把 `multiagent.py` 里那段 `if not gate.allow: ... continue`
    删掉，本条立刻 FAILED（root 会直接收口，FINAL 生效）。
    """
    # root 连续两次想收口，中间没有 coverage 记录 → 两次都被拒 →
    # 脚本 gateway 用完，循环走完，finished=False。
    gateway = _gateway([_FINAL] * 4)
    spec = _spec(multi_agent=True, max_turns=4, min_coverage=3)

    outcome = _run(
        runner.solve_in_sandbox(
            spec, sandbox=_FakeSandboxProxy(), gateway=gateway, system_prompt="sys"
        )
    )

    # 门拦住了两次 FINAL，所以 summary 没有被写进 outcome
    assert outcome.summary == ""
    assert gateway.calls >= 2


def test_root_final_passes_after_recording_enough_coverage() -> None:
    """反面：先记够覆盖，再收口 → 放行。

    这条保证门不会把"认真扫完了"的扫描也拦死 —— 否则整条链路会一直
    烧预算到硬超时，那比误报更糟。
    """
    # 3 条覆盖记录 + 4 条 FINAL 余量。只看 coverage 不够 —— 门还要求至少
    # 1 条有证据的结论（`MIN_FINDINGS`）。`_FINAL` 里带了一条 finding，
    # 但它要等门放行才登记进 `finding_sink`，所以第四次调用必须再给一次。
    texts = [_coverage_action(f"/s{i}") for i in range(3)] + [_FINAL] * 5
    gateway = _gateway(texts)
    spec = _spec(multi_agent=True, max_turns=8, min_coverage=3)

    outcome = _run(
        runner.solve_in_sandbox(
            spec, sandbox=_FakeSandboxProxy(), gateway=gateway, system_prompt="sys"
        )
    )

    assert outcome.summary == "扫完了"


def test_gate_disabled_by_min_coverage_zero() -> None:
    """`min_coverage=0` 退回 v0.74 行为（测试专用开关）。"""
    gateway = _gateway([_FINAL])
    spec = _spec(multi_agent=True, max_turns=4, min_coverage=0)

    outcome = _run(
        runner.solve_in_sandbox(
            spec, sandbox=_FakeSandboxProxy(), gateway=gateway, system_prompt="sys"
        )
    )
    assert outcome.summary == "扫完了"


def test_rejection_emits_reasoning_event_not_final_event() -> None:
    """被退回时界面要能看见，但**不能**发 finished 类事件（它没结束）。"""
    events: list[tuple[str, str, dict[str, str]]] = []
    gateway = _gateway([_FINAL] * 2)
    spec = _spec(multi_agent=True, max_turns=2, min_coverage=3)

    _run(
        runner.solve_in_sandbox(
            spec,
            sandbox=_FakeSandboxProxy(),
            gateway=gateway,
            system_prompt="sys",
            on_progress=lambda k, s, p: events.append((k, s, p)),
        )
    )

    kinds = [k for k, _s, _p in events]
    assert "reasoning" in kinds
    assert any("还不能收口" in s for _k, s, _p in events)


def _FakeSandboxProxy():
    """`_FakeSandbox` 从 test_runner 借来；单独包一层方便将来替换。"""
    from tests.kernel.test_runner import _FakeSandbox

    return _FakeSandbox()


# ══════════════════════════════════════════════════════════
# 3. v0.76：原地打转（acting_ratio）——P2 真跑催生的一层
# ══════════════════════════════════════════════════════════


class TestStalledDetection:
    """`Observation.stalled` 的判据。

    真跑依据（2026-10-23 P2）：root 用 63 轮只探了 4 个端点，
    42% 的轮次在 grep 前端 bundle / 写解析脚本，一次请求都没发；
    它收到"覆盖 0/5"之后继续打同一个点直到 80 轮耗尽。
    """

    def test_not_stalled_when_ratio_missing(self) -> None:
        """没统计过（`None`）就不能判打转 —— 宁可不知道也不要误伤。"""
        obs = termination.Observation(acting_ratio=None)
        assert obs.stalled is False

    def test_stalled_when_mostly_analyzing(self) -> None:
        """四成以下在打目标 = 打转。0.42 正是 P2 的实测值。"""
        assert termination.Observation(acting_ratio=0.42).stalled is True

    def test_not_stalled_at_normal_mix(self) -> None:
        """正常扫描允许一半以下用于读文档/写脚本，0.65 不该被判打转。"""
        assert termination.Observation(acting_ratio=0.65).stalled is False

    def test_boundary_is_sixty_percent(self) -> None:
        """阈值就在 0.6：正好 0.6 不算打转（留余量）。"""
        assert termination.Observation(acting_ratio=0.6).stalled is False
        assert termination.Observation(acting_ratio=0.599).stalled is True


class TestRedirectNudge:
    """打转时给的提醒必须**换向**，而不是"再多测几个"。"""

    def test_stalled_nudge_suggests_changing_approach(self) -> None:
        d = termination.decide(
            termination.Observation(
                coverage_count=0,
                finding_count=0,
                turns_used=60,
                total_budget=240,
                model_claims_done=True,
                acting_ratio=0.42,
            )
        )
        assert d.allow is False
        # 换向是第一位的要求，不是"再补覆盖"。
        assert "打得太窄" in d.nudge
        assert d.nudge.index("打得太窄") < d.nudge.index("record_coverage")

    def test_normal_nudge_has_no_redirect_language(self) -> None:
        """没打转时不许冒出"太窄"这类话 —— 否则模型会以为方向错了。"""
        d = termination.decide(
            termination.Observation(
                coverage_count=1,
                finding_count=0,
                turns_used=20,
                total_budget=240,
                model_claims_done=True,
                acting_ratio=0.75,
            )
        )
        assert d.allow is False
        assert "打得太窄" not in d.nudge

    def test_stalled_never_leaks_internal_thresholds(self) -> None:
        """换向提示里同样不能出现内部阈值（否则模型会去刷指标）。"""
        d = termination.decide(
            termination.Observation(
                coverage_count=0,
                finding_count=0,
                turns_used=60,
                total_budget=240,
                model_claims_done=True,
                acting_ratio=0.1,
            )
        )
        assert "0.6" not in d.nudge
        assert "60%" not in d.nudge


class TestCoordinatorActionWindow:
    """`AgentCoordinator` 的动作滑窗（`note_action` / `acting_ratio`）。"""

    def test_ratio_is_none_before_enough_samples(self) -> None:
        from app.kernel.multiagent import ACTION_WINDOW_MIN_SAMPLES, AgentCoordinator

        coord = AgentCoordinator()
        for _ in range(ACTION_WINDOW_MIN_SAMPLES - 1):
            coord.note_action(acted=True)
        assert coord.acting_ratio() is None

    def test_ratio_computed_after_enough_samples(self) -> None:
        from app.kernel.multiagent import ACTION_WINDOW_MIN_SAMPLES, AgentCoordinator

        coord = AgentCoordinator()
        for i in range(ACTION_WINDOW_MIN_SAMPLES):
            coord.note_action(acted=(i % 2 == 0))
        ratio = coord.acting_ratio()
        assert ratio is not None
        assert 0.0 < ratio < 1.0

    def test_window_only_keeps_recent_actions(self) -> None:
        """滑窗只留最近 `ACTION_WINDOW` 条 —— 早期有效侦察不该长期稀释信号。"""
        from app.kernel.multiagent import ACTION_WINDOW, AgentCoordinator

        coord = AgentCoordinator()
        # 先 100 次全在打目标，再 20 次全在分析。
        for _ in range(100):
            coord.note_action(acted=True)
        for _ in range(ACTION_WINDOW):
            coord.note_action(acted=False)
        assert len(coord.recent_actions) == ACTION_WINDOW
        assert coord.acting_ratio() == 0.0


class TestLooksLikeActing:
    """`_looks_like_acting` 的分工判定（P2 的动作类别分布就靠它复现）。"""

    @pytest.mark.parametrize(
        "command",
        [
            "curl -s http://t/api/v1/scans",
            "nmap -p- 10.0.0.5",
            "ffuf -u http://t/FUZZ -w /tmp/w.txt",
            "gobuster dir -u http://t -w /tmp/w.txt",
            "sqlmap -u 'http://t/x?id=1' --batch",
        ],
    )
    def test_live_requests_count_as_acting(self, command: str) -> None:
        from app.kernel.multiagent import _looks_like_acting

        assert _looks_like_acting(tool_name="run_shell", command=command) is True

    @pytest.mark.parametrize(
        "command",
        [
            "grep -oE 'http[A-Za-z]*' /tmp/index.js",
            "python3 -c 'import json;print(1)'",
            "ls -la /tmp",
            "cat > /tmp/probe.sh <<'EOF'",
            "jq '.paths | keys' /tmp/spec.json",
        ],
    )
    def test_local_analysis_does_not_count(self, command: str) -> None:
        from app.kernel.multiagent import _looks_like_acting

        assert _looks_like_acting(tool_name="run_shell", command=command) is False

    def test_curl_plus_grep_still_counts_as_acting(self) -> None:
        """一条命令里既有 curl 又有 grep —— 打出去的那发子弹要算数。"""
        from app.kernel.multiagent import _looks_like_acting

        assert (
            _looks_like_acting(
                tool_name="run_shell",
                command="curl -s http://t/ | grep -o '<title>[^<]*'",
            )
            is True
        )

    def test_http_request_tool_is_always_acting(self) -> None:
        from app.kernel.multiagent import _looks_like_acting

        assert _looks_like_acting(tool_name="http_request", command="{}") is True

    def test_bookkeeping_tools_are_not_acting(self) -> None:
        """`record_coverage` / `write_note` / `think` 是记账与思考，不碰目标。"""
        from app.kernel.multiagent import _looks_like_acting

        for name in ("record_coverage", "write_note", "think", "create_todo"):
            assert _looks_like_acting(tool_name=name, command="{}") is False



def test_note_action_is_wired_into_the_tool_loop() -> None:
    """★ 接线断言：`_run_agent_loop` 里必须真的调 `coord.note_action`。

    纯函数测试全绿但接线没接上，是 v0.72 `mcp_servers` 事故的形状。
    这条用 `inspect.getsource` 直接盯源码里的调用点。
    """
    import inspect

    from app.kernel import multiagent

    src = inspect.getsource(multiagent.run_agent_loop)
    assert "coord.note_action(" in src, "动作登记没接进工具循环，原地打转判定永远拿不到数据"
    assert "_looks_like_acting(" in src


def test_gate_receives_acting_ratio() -> None:
    """★ 接线断言：门的 `Observation` 必须带上 `acting_ratio`。

    否则 `stalled` 恒 False，换向提示永远不会触发。
    """
    import inspect

    from app.kernel import multiagent

    src = inspect.getsource(multiagent.run_agent_loop)
    assert "acting_ratio=coord.acting_ratio()" in src
