"""自研引擎后端（`app/scans/agent_runner.py`）的离线单测。

## 这些测试**不碰 docker、不碰 LLM**

它们锁的是三件**纯函数**层面的事 —— 也就是"把引擎的产出翻译成既有
发现入库链路"这一步。这一步是"只加场景包、不动核心"的接缝：写错了
不会崩，只会让一次真扫描的结论**看起来正常但内容是错的**，而那是最难
在后验里发现的失败形态。

分三组：

1. `build_solve_spec_from_scan` —— `LaunchSpec` → `SolveSpec` 的翻译。
   重点锁住"渗透评估一定带上 `pentest-evaluation` 分类 + `pentest`/
   `offensive` 开关"，否则模型会按 CTF 解题纪律去追一个不存在的 flag。
2. `_write_vulnerabilities` —— 引擎 `findings` → `vulnerabilities.json`。
   重点锁住"空白发现被丢弃"与"严重度／证据分级被归一"。
3. `_normalize_*` 与 `request_stop` 的空句柄行为。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.scans import agent_runner


def _spec(**overrides):  # noqa: ANN001 - 测试内部的简单夹具
    from app.scans.runner import LaunchSpec

    base = dict(
        scan_id="scan_demo",
        target="http://demo.test:8080",
        target_kind="url",
        skills=(),
        run_dir=Path("/tmp/unused"),
        log_path=Path("/tmp/unused/scan.log"),
        llm_model="deepseek-v4.1-flash",
        llm_api_key="sk-x",
        llm_api_base="https://api.example/v1",
        brief="授权评估",
    )
    base.update(overrides)
    return LaunchSpec(**base)


# ══════════════════════════════════════════════════════════
# 1. LaunchSpec → SolveSpec
# ══════════════════════════════════════════════════════════


def test_pentest_spec_sets_evaluation_category_and_flags() -> None:
    spec = _spec(enable_offensive_prompt=True, sandbox_network_isolated=False)
    solve = agent_runner.build_solve_spec_from_scan(spec)

    # 分类必须落在 `pentest-` 前缀上 —— 它同时是提示词注入判据。
    assert solve.category == "pentest-evaluation"
    assert solve.pentest is True
    # offensive 是行为开关（越权/注入手法块），与 pentest 场景包正交。
    assert solve.offensive is True


def test_pentest_spec_offensive_follows_scan_toggle() -> None:
    # 开关关着时 offensive 必须是 False —— 否则会在关掉手法块的部署里
    # 仍然注入那一块，与配置相反。
    solve = agent_runner.build_solve_spec_from_scan(_spec(enable_offensive_prompt=False))
    assert solve.offensive is False


def test_pentest_spec_url_becomes_access_url() -> None:
    solve = agent_runner.build_solve_spec_from_scan(_spec(target="http://t:80", target_kind="url"))
    assert solve.access_urls == ("http://t:80",)


def test_pentest_spec_service_becomes_access_url() -> None:
    solve = agent_runner.build_solve_spec_from_scan(
        _spec(target="1.2.3.4:443", target_kind="service")
    )
    assert solve.access_urls == ("1.2.3.4:443",)


@pytest.mark.parametrize("kind", ["repo", "path", "artifact"])
def test_pentest_spec_non_url_kinds_have_no_access_url(kind: str) -> None:
    # 非 URL 形态靠 run_shell 在容器里处理，不进 http_request 白名单。
    solve = agent_runner.build_solve_spec_from_scan(_spec(target="/some/path", target_kind=kind))
    assert solve.access_urls == ()


def test_pentest_spec_turns_follow_scan_mode_ceiling() -> None:
    """回合上限由「扫描深度」定档、「回合上限」定细，二者取较小值。

    回归锁：旧实现写死 `min(scan_max_turns, 80)`，导致
      ① 用户把「回合上限」调到 200/500 永远只有 80（界面填了不生效）；
      ② 「扫描深度」对自研引擎完全没被读，是死配置。
    现 quick/standard/deep 各自给天花板 40/80/150，用户填入的值取较小值。
    """
    # deep 才能跑到 150（旧实现永远 80）
    assert (
        agent_runner.build_solve_spec_from_scan(
            _spec(scan_max_turns=500, scan_mode="deep")
        ).max_turns
        == 150
    )
    # standard 天花板 80（与旧默认一致）
    assert (
        agent_runner.build_solve_spec_from_scan(
            _spec(scan_max_turns=500, scan_mode="standard")
        ).max_turns
        == 80
    )
    # quick 天花板 40
    assert (
        agent_runner.build_solve_spec_from_scan(
            _spec(scan_max_turns=500, scan_mode="quick")
        ).max_turns
        == 40
    )
    # 用户填得比天花板小 → 尊重用户值（细旋钮有效）
    assert (
        agent_runner.build_solve_spec_from_scan(
            _spec(scan_max_turns=40, scan_mode="deep")
        ).max_turns
        == 40
    )
    # 下限 10：填 1 也不会真的只跑 1 轮
    assert (
        agent_runner.build_solve_spec_from_scan(
            _spec(scan_max_turns=1, scan_mode="standard")
        ).max_turns
        == 10
    )


def test_pentest_spec_unknown_scan_mode_falls_back_to_standard_ceiling() -> None:
    # 认不出的深度退回 standard 天花板，不静默当成 deep 烧钱。
    assert (
        agent_runner.build_solve_spec_from_scan(
            _spec(scan_max_turns=500, scan_mode="banana")
        ).max_turns
        == 80
    )


def test_pentest_spec_network_isolation_for_network_targets_is_forced_open() -> None:
    """网络目标（url/service）的渗透必须开网，即便 settings 说要隔离。

    这是一条**修过的 bug 的回归锁**。原实现是
    `network_isolated = bool(spec.sandbox_network_isolated)`，纯跟随 settings。
    真跑实测：Kali 上 egress 白名单写死了 CTF 靶站网段，目标 192.168.1.121
    被 DROP，而 setup 又是一次 web 渗透 —— 容器被关进 `--network none`，
    镜像里 98 个工具（nmap/ffuf/gobuster/sqlmap…）全部连不上目标，
    96% 的动作退化成宿主侧 http_request 逐个猜 URL。

    判据：url/service 是"要去连别人"的形态，联网是前提不是选项。
    """
    for kind in ("url", "service"):
        solve = agent_runner.build_solve_spec_from_scan(
            _spec(target="http://t:80", target_kind=kind, sandbox_network_isolated=True)
        )
        assert solve.network_isolated is False, f"{kind} 目标被隔离了 —— 工具会全废"


def test_pentest_spec_network_isolation_follows_settings_for_local_targets() -> None:
    """本地形态（repo/path/artifact）不需要联网，此时跟随 settings。

    这些形态的证据在磁盘上，放不放开出站网络不影响能不能干成活，
    所以尊重运维的隔离意愿（最小权限）。
    """
    for kind in ("repo", "path", "artifact"):
        solve = agent_runner.build_solve_spec_from_scan(
            _spec(target="/some/path", target_kind=kind, sandbox_network_isolated=True)
        )
        assert solve.network_isolated is True, f"{kind} 是本地目标，应尊重隔离设置"


def test_pentest_prompt_block_actually_injected() -> None:
    """端到端：翻译出的 spec 过 `build_system_prompt_by_spec` 要真的带上渗透块。

    这是"接缝"上最容易悄悄坏掉的一环 —— 分类改了名字、前缀对不上，
    提示词就静默退回解题纪律，而 spec 本身看起来完全正常。
    """
    from app.kernel import runner as ctf2_runner

    solve = agent_runner.build_solve_spec_from_scan(_spec(enable_offensive_prompt=True))
    prompt = ctf2_runner.build_system_prompt_by_spec(solve)
    # 渗透块里出现"发现 / 证据 / 严重度"这类词；更重要的是它不该是纯解题块。
    assert len(prompt) > len(ctf2_runner.load_system_prompt())
    assert "发现" in prompt or "证据" in prompt


def test_pentest_prompt_keeps_methodology_sections() -> None:
    """渗透提示词的「方法论」段不能在未来重构里被悄悄删掉。

    这几段是照着成熟项目的做法补的（分阶段推进 / 一次只打一类 / 认证绕过
    清单 / 读→写提权 / 工具选择纪律）。删掉它们，模型会退化成「随机猜 URL」
    —— 这正是那次 80 轮任务 96% 动作是 http_request 的成因。用关键短语
    钉死，坏掉时测试直接红，而不是等到真跑才发现。
    """
    from app.kernel import runner as ctf2_runner

    p = ctf2_runner.load_pentest_prompt()
    # 五个方法论小节必须都在
    for heading in ("6.2.1", "6.2.2", "6.2.3", "6.3.1"):
        assert heading in p, f"方法论小节 {heading} 丢了"
    # 关键纪律短语
    for phrase in ("一次只专注", "读→写", "http_request", "dirb/wordlists"):
        assert phrase in p, f"关键短语「{phrase}」丢了"
    # 反面：不该推荐镜像里不存在的工具当常规手段（nuclei 只能出现在「没有」的告诫里）
    assert "镜像里没有 `nuclei`" in p or "镜像里**没有** `nuclei`" in p


# ══════════════════════════════════════════════════════════
# 2. findings → vulnerabilities.json
# ══════════════════════════════════════════════════════════


class _FakeOutcome:
    def __init__(self, findings):  # noqa: ANN001
        self.findings = findings
        self.error = None
        self.turns = 0
        self.tool_calls = 0
        self.llm_calls = 0


def _write(tmp_path: Path, findings) -> list[dict]:  # noqa: ANN001
    spec = _spec(run_dir=tmp_path, log_path=tmp_path / "scan.log")
    agent_runner._write_vulnerabilities(spec, _FakeOutcome(findings))
    path = tmp_path / "vulnerabilities.json"
    assert path.is_file()
    return json.loads(path.read_text(encoding="utf-8"))


def test_write_vulnerabilities_roundtrips_through_importer(tmp_path: Path) -> None:
    """写出来的文件必须能被既有的 `findings.importer` 一行不改地读进去。

    这是整个后端的存在理由：产出侧对齐 strix 形状，入库链路（含 SLA /
    时间线 / CWE 映射）就全部复用现成代码。形状对不上，整条链路静默空转。
    """
    from app.findings import strix_vulns

    records = _write(
        tmp_path,
        [
            {
                "title": "SQL 注入",
                "severity": "高",
                "target": "/q?q=",
                "verification": "confirmed",
                "evidence": "q=1' -> syntax error",
                "reasoning": "可注入",
                "remediation": "参数化查询",
                "cwe": "CWE-89",
            }
        ],
    )
    parsed = strix_vulns.parse(records, default_target="http://demo.test")
    assert len(parsed) == 1
    finding = parsed[0]
    assert finding.title == "SQL 注入"
    # 中文"高"归一成 high；confirmed → verified（证据强度轴）。
    assert finding.severity == "high"
    assert finding.verification == "verified"
    assert finding.cwe == "CWE-89"


def test_write_vulnerabilities_drops_blank_entries(tmp_path: Path) -> None:
    # 既没标题又没位置的"发现"不是发现 —— 丢弃而不是造空壳。
    records = _write(
        tmp_path,
        [
            {"title": "有标题", "severity": "low"},
            {"title": "", "target": ""},
            {"title": "  ", "target": "  "},
        ],
    )
    assert len(records) == 1
    assert records[0]["title"] == "有标题"


def test_write_vulnerabilities_confidence_maps_from_verification(tmp_path: Path) -> None:
    # confidence 只允许 high/medium/low（importer 的 _CONFIDENCE_TO_VERIFICATION）。
    # confirmed → high，其余 → medium。写错会静默落到 partial，不可接受。
    records = _write(
        tmp_path,
        [
            {"title": "a", "verification": "confirmed"},
            {"title": "b", "verification": "ruled_out"},
            {"title": "c", "verification": "needs_follow_up"},
        ],
    )
    by_title = {r["title"]: r for r in records}
    assert by_title["a"]["confidence"] == "high"
    assert by_title["b"]["confidence"] == "medium"
    assert by_title["c"]["confidence"] == "medium"


def test_write_vulnerabilities_ignores_non_list_findings(tmp_path: Path) -> None:
    # 模型可能给出非列表（字符串 / 对象）—— 不崩，写空列表。
    records = _write(tmp_path, "not a list")
    assert records == []
    records = _write(tmp_path, None)
    assert records == []


# ══════════════════════════════════════════════════════════
# 3. 归一函数与空句柄
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("高", "high"),
        ("高危", "high"),
        ("信息", "info"),
        ("CRITICAL", "critical"),
        ("不认识", "info"),  # 认不出 → info（宁低不高）
        (None, "info"),
    ],
)
def test_normalize_severity(raw: object, expected: str) -> None:
    assert agent_runner._normalize_severity(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("confirmed", "confirmed"),
        ("排除", "ruled_out"),
        ("成立", "confirmed"),
        ("garbage", "needs_follow_up"),  # 拿不准 → 待跟进
        (None, "needs_follow_up"),
    ],
)
def test_normalize_verification(raw: object, expected: str) -> None:
    assert agent_runner._normalize_verification(raw) == expected


def test_scheduler_request_stop_no_handle_returns_false() -> None:
    """请求停止一个不存在的句柄：返回 False、不抛错。

    这不是失败（记录已被 abort_scan 置成 aborted），只是"我们无从下手"。
    """
    from app.scans.scheduler import ScanScheduler

    sched = ScanScheduler()
    assert sched.request_stop("scan_does_not_exist") is False


def test_scheduler_request_stop_calls_handle_stopper() -> None:
    called = {"n": 0}

    class _H:
        def request_stop(self) -> None:
            called["n"] += 1

    from app.scans.scheduler import ScanScheduler

    sched = ScanScheduler()
    sched.register("scan_x", _H())  # type: ignore[arg-type]
    assert sched.request_stop("scan_x") is True
    assert called["n"] == 1


# ══════════════════════════════════════════════════════════
# v0.64：设置页两项「单次超时」「允许写入挂载卷」从死配置变成真功能
# ══════════════════════════════════════════════════════════


def test_launch_spec_defaults_keep_history_behavior() -> None:
    """不传 timeout/volume_write 时：不设超时、允许写挂载（=历史行为）。

    这条锁的是"没配过就行为突变"这个失败形态 —— 老扫描不该因为加了
    开关就突然变成只读挂载或不带超时。
    """
    spec = _spec()
    assert spec.timeout_seconds is None
    assert spec.allow_volume_write is True


def test_launch_spec_accepts_timeout_and_volume_write() -> None:
    spec = _spec(timeout_seconds=1800, allow_volume_write=False)
    assert spec.timeout_seconds == 1800
    assert spec.allow_volume_write is False


def test_volume_write_true_mounts_workspace_rw(tmp_path: Path) -> None:
    """`allow_volume_write=True` → 持久工作区以 `:rw` 挂进 /work/persist。"""
    from app.kernel import runner as kernel_runner

    ws = tmp_path / "workspace"
    ws.mkdir()
    box = kernel_runner.SandboxContainer(
        name="suvex-scan-test",
        image="ctf2-sandbox-ir",
        network_isolated=True,
        workspace_dir=ws,
        volume_write=True,
    )
    argv = box._argv()
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    persist = [m for m in mounts if m.endswith(f":{kernel_runner.PERSIST_DIR}:rw")]
    assert persist, f"没找到 /work/persist 的读写挂载：{mounts}"


def test_volume_write_false_mounts_workspace_ro(tmp_path: Path) -> None:
    """`allow_volume_write=False` → 同一工作区改以 `:ro` 挂。

    这条是真功能的**反面对照**：只测 rw 会漏掉"开关其实没接线"。
    """
    from app.kernel import runner as kernel_runner

    ws = tmp_path / "workspace"
    ws.mkdir()
    box = kernel_runner.SandboxContainer(
        name="suvex-scan-test",
        image="ctf2-sandbox-ir",
        network_isolated=True,
        workspace_dir=ws,
        volume_write=False,
    )
    argv = box._argv()
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    ro = [m for m in mounts if m.endswith(f":{kernel_runner.PERSIST_DIR}:ro")]
    rw = [m for m in mounts if m.endswith(f":{kernel_runner.PERSIST_DIR}:rw")]
    assert ro, f"关掉写入后应挂 :ro，实际挂载：{mounts}"
    assert not rw, "关掉写入后不应再有 :rw 的 /work/persist"


def test_timeout_outcome_carries_human_reason() -> None:
    """超时收尾的 outcome 带一句人话原因，且没有假造发现。"""

    class _Spec:
        timeout_reason = "扫描超过设置页指定的单次超时（30 分钟），已被引擎停止。"

    outcome = agent_runner._timeout_outcome(_Spec())
    assert outcome.error and "超时" in outcome.error
    assert not outcome.findings


def test_solve_with_stop_cancels_on_wall_clock_timeout(monkeypatch) -> None:
    """墙钟超时真的会取消引擎循环，并交回一个"超时收尾"的 outcome。

    ## 为什么这条要用 monkeypatch

    `_solve_with_stop` 会去拉真正的 `solve_in_sandbox`（要 docker、要 LLM）。
    这里把内核那个协程换成一个"永不结束"的假协程，验证的是**超时轮询逻辑**
    本身：到点取消、不把 CancelledError 抛给上层、返回带人话原因的 outcome。

    ## 反面对照（变异会红）

    把 deadline 判断去掉 / 不设 `timed_out`，这条会因为拿不到 outcome
    （或 CancelledError 上抛）而失败。
    """
    import asyncio
    import threading

    from app.kernel import runner as kernel_runner

    async def _forever(*args, **kwargs):  # noqa: ANN002, ANN003
        # 真实引擎一旦开跑就不会自己返回；用 sleep 模拟。
        await asyncio.sleep(3600)
        raise AssertionError("不应跑到这里")  # pragma: no cover

    monkeypatch.setattr(kernel_runner, "solve_in_sandbox", _forever)

    class _Spec:
        timeout_reason = ""  # 允许被 `_solve_with_stop` 覆盖

    stop_event = threading.Event()
    outcome = asyncio.run(
        agent_runner._solve_with_stop(
            _Spec(),
            sandbox=object(),
            gateway=object(),
            system_prompt="x",
            stop_event=stop_event,
            on_progress=lambda *a, **k: None,
            timeout_seconds=1,  # 1 秒就超时（轮询间隔 0.5s，约 1~1.5s 收尾）
        )
    )
    assert outcome.error and "超时" in outcome.error


def test_solve_with_stop_does_not_time_out_when_none(monkeypatch) -> None:
    """`timeout_seconds=None` 时不设 deadline，快返回的引擎正常收尾。"""
    import asyncio
    import threading

    from app.kernel import runner as kernel_runner

    async def _quick(*args, **kwargs):  # noqa: ANN002, ANN003
        return kernel_runner.SolveOutcome(error=None)

    monkeypatch.setattr(kernel_runner, "solve_in_sandbox", _quick)

    outcome = asyncio.run(
        agent_runner._solve_with_stop(
            object(),
            sandbox=object(),
            gateway=object(),
            system_prompt="x",
            stop_event=threading.Event(),
            on_progress=lambda *a, **k: None,
            timeout_seconds=None,
        )
    )
    assert outcome.error is None


