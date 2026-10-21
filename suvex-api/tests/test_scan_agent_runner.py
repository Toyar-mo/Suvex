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


def test_pentest_spec_turns_clamped_to_eighty() -> None:
    # 扫描侧可能给 500 轮，但单次评估 80 轮足够；不夹的话墙钟会失控。
    assert agent_runner.build_solve_spec_from_scan(_spec(scan_max_turns=500)).max_turns == 80
    assert agent_runner.build_solve_spec_from_scan(_spec(scan_max_turns=40)).max_turns == 40


def test_pentest_spec_network_isolation_follows_scan() -> None:
    # 渗透评估默认要联网（False），配置显式隔离时跟随配置。
    assert (
        agent_runner.build_solve_spec_from_scan(_spec(sandbox_network_isolated=False))).network_isolated is False
    assert (
        agent_runner.build_solve_spec_from_scan(_spec(sandbox_network_isolated=True))).network_isolated is True


def test_pentest_prompt_block_actually_injected() -> None:
    """端到端：翻译出的 spec 过 `build_system_prompt_by_spec` 要真的带上渗透块。

    这是"接缝"上最容易悄悄坏掉的一环 —— 分类改了名字、前缀对不上，
    提示词就静默退回解题纪律，而 spec 本身看起来完全正常。
    """
    from app.ctf2 import runner as ctf2_runner

    solve = agent_runner.build_solve_spec_from_scan(_spec(enable_offensive_prompt=True))
    prompt = ctf2_runner.build_system_prompt_by_spec(solve)
    # 渗透块里出现"发现 / 证据 / 严重度"这类词；更重要的是它不该是纯解题块。
    assert len(prompt) > len(ctf2_runner.load_system_prompt())
    assert "发现" in prompt or "证据" in prompt


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
