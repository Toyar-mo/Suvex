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
    现 quick/standard/deep 各给天花板（v0.76 按 P2 实测从 40/80/150
    上调到 60/120/200），用户填入的值取较小值。
    """
    # deep 才能跑到 200（旧实现永远 80）
    assert (
        agent_runner.build_solve_spec_from_scan(
            _spec(scan_max_turns=500, scan_mode="deep")
        ).max_turns
        == 200
    )
    # standard 天花板 120（v0.76 上调：P2 在 80 轮跑满未收敛）
    assert (
        agent_runner.build_solve_spec_from_scan(
            _spec(scan_max_turns=500, scan_mode="standard")
        ).max_turns
        == 120
    )
    # quick 天花板 60
    assert (
        agent_runner.build_solve_spec_from_scan(
            _spec(scan_max_turns=500, scan_mode="quick")
        ).max_turns
        == 60
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
        == 120
    )


def test_depth_ceiling_leaves_room_to_dig_after_covering() -> None:
    """★ v0.76：standard 档必须留出"探完面还能回来深挖"的余量。

    P2 失败的形态不是"没探到"，是 80 轮刚好够窄探一遍就耗尽，
    连一条带对照实验的结论都交不出来。这条盯的是档位之间的区分度
    与 standard 的绝对值，而不是某个具体数字——数字可以再调，
    但 standard 不能低到再次出现"探完就没预算"。
    """
    ceiling = {
        mode: agent_runner.build_solve_spec_from_scan(
            _spec(scan_max_turns=10_000, scan_mode=mode)
        ).max_turns
        for mode in ("quick", "standard", "deep")
    }
    assert ceiling["quick"] < ceiling["standard"] < ceiling["deep"]
    # P1 那次 80 轮"刚好够"（28 分钟、17 条）是顺的一次；常态需要更多余量。
    assert ceiling["standard"] > 80


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
    def __init__(self, findings, workorders=None):  # noqa: ANN001
        self.findings = findings
        self.workorders = workorders
        self.error = None
        self.turns = 0
        self.tool_calls = 0
        self.llm_calls = 0


def _write(tmp_path: Path, findings, workorders=None) -> list[dict]:  # noqa: ANN001
    spec = _spec(run_dir=tmp_path, log_path=tmp_path / "scan.log")
    agent_runner._write_vulnerabilities(spec, _FakeOutcome(findings, workorders))
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
                # v0.75 起的信任分级：两个身份对比 → L2，才能落 verified。
                # 用 single_response（L1）的话，模型自述的 confirmed 会被降级成
                # partial —— 那条规则本身由 test_..._downgrades_self_reported 钉住。
                "evidence_kind": "cross_identity",
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
            {"title": "有标题", "severity": "low", "evidence_kind": "single_response"},
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
            # 三条都给 L2（有对照实验），这样验证的是 verification 与
            # confidence 的映射本身，而不是被分级规则挡掉。
            {"title": "a", "verification": "confirmed", "evidence_kind": "cross_identity"},
            {"title": "b", "verification": "ruled_out", "evidence_kind": "cross_identity"},
            {"title": "c", "verification": "needs_follow_up", "evidence_kind": "cross_identity"},
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


def test_write_vulnerabilities_collects_workorders_for_ir_scans(tmp_path: Path) -> None:
    """IR 场景模型交的是 `workorders` 不是 `findings` —— 必须一起入库。

    回归 v0.74 之前的缺陷：`_write_vulnerabilities` 只遍历
    `outcome.findings`，于是 4 个 IR case 全部 `ok:true` 而
    `vulnerabilities.json` 是 `[]`、DB findings 恒 0。
    反向验证：把 workorders 那段删掉，本条立刻 FAILED。
    """
    records = _write(
        tmp_path,
        None,
        [
            {
                "title": "web-01 应用上传接口路径穿越导致 RCE",
                "severity": "高",
                "verification": "confirmed",
                "summary": "203.0.113.201 通过上传接口穿越实现 RCE",
                "evidence": "/var/log/nginx/error.log 中 /../../etc/passwd 请求",
                "action": "隔离 web-01，轮换 deploy 凭据",
                "cwe": "CWE-22",
            }
        ],
    )
    assert len(records) == 1
    got = records[0]
    assert got["title"] == "web-01 应用上传接口路径穿越导致 RCE"
    assert got["severity"] == "high"
    assert got["verification"] == "confirmed"
    assert got["confidence"] == "high"
    assert got["discovered_by"] == "agent-workorder"
    # 证据与处置动作都要落到 importer 真正读的字段上
    assert "/../../etc/passwd" in got["evidence"]
    assert "轮换 deploy 凭据" in got["remediation_steps"]
    assert "203.0.113.201" in got["description"]


def test_write_vulnerabilities_keeps_honest_grading_fields(tmp_path: Path) -> None:
    """模型按 `counterevidence` 技能写的四个诚实分级字段，必须到达报告。

    回归 2026-10-26 的缺陷：这四项被 `prompts/kernel_pentest.md` 与
    `skills/analysis/counterevidence.md:156-166` 明确教模型去写，而
    `_write_vulnerabilities` 的 dict 里没有这四个键 —— 模型认真写完、
    后端静默丢掉，不报错不警告，报告里一个字都没有。

    后果不是"少一段文字"：丢掉 `counterevidence` 之后，一条**有反证**的
    结论会和一条铁证在界面上长得一模一样，读的人无法判断该不该信。

    这里断言的是**行为**（解析后的 reasoning 里有这些内容），不是源码
    文本 —— 本项目栽过 `inspect.getsource` 按行号切片的假红。
    """
    from app.findings import strix_vulns

    records = _write(
        tmp_path,
        [
            {
                "title": "简介页存储型 XSS",
                "severity": "high",
                "target": "https://example.test/profile",
                "verification": "confirmed",
                "evidence_kind": "cross_identity",
                "evidence": "POST bio=<script>alert(1)</script>",
                "reasoning": "个人简介未转义直接拼进 HTML。",
                "remediation": "输出前做 HTML 实体编码。",
                "cwe": "CWE-79",
                "counterevidence": "检查了 CSP 响应头，未设置 script-src。",
                "assumptions": "该页面对所有已登录用户可见。",
                "confidence_rationale": "有可复现 PoC 且经浏览器确认执行。",
                "severity_change_conditions": "若仅管理员可见则降为 medium。",
            }
        ],
    )
    assert len(records) == 1
    record = records[0]
    # ① 四个键必须被写进中间文件（否则下游无从读取）
    assert record["counterevidence"] == "检查了 CSP 响应头，未设置 script-src。"
    assert record["assumptions"] == "该页面对所有已登录用户可见。"
    assert record["confidence_rationale"] == "有可复现 PoC 且经浏览器确认执行。"
    assert record["severity_change_conditions"] == "若仅管理员可见则降为 medium。"

    # ② 解析后它们必须真的出现在契约的 reasoning 里（带标签，读者能分辨）
    parsed = strix_vulns.parse(records, default_target="https://example.test")
    assert len(parsed) == 1
    reasoning = parsed[0].reasoning
    assert "【反证】" in reasoning
    assert "未设置 script-src" in reasoning
    assert "【成立假设】" in reasoning
    assert "【置信度依据" in reasoning
    assert "【严重度升降条件】" in reasoning


def test_write_vulnerabilities_reasoning_is_not_the_evidence(tmp_path: Path) -> None:
    """「为什么判断有问题」必须是推理，不能是复现命令。

    回归 2026-10-26 的缺陷：`technical_analysis` 原来接的是 `evidence`，
    而 `strix_vulns._reasoning()` 把 `technical_analysis` 当作契约的
    `reasoning`。于是报告的「为什么判断有问题」渲染出 `curl -s ...`，
    与「证据」栏**一模一样**，两栏都答非所问。
    """
    from app.findings import strix_vulns

    records = _write(
        tmp_path,
        [
            {
                "title": "未授权读取他人扫描记录",
                "severity": "high",
                "target": "https://example.test/api/v1/scans/2",
                "verification": "confirmed",
                "evidence_kind": "cross_identity",
                "evidence": "curl -H 'Authorization: Bearer userA' https://example.test/api/v1/scans/2",
                "reasoning": "userA 的凭据拿到了 userB 的记录，鉴权只校验登录不校验归属。",
                "remediation": "在查询层加 owner_id 过滤。",
                "cwe": "CWE-639",
            }
        ],
    )
    parsed = strix_vulns.parse(records, default_target="https://example.test")
    finding = parsed[0]
    assert "鉴权只校验登录不校验归属" in finding.reasoning
    # 证据仍然是证据（原文保留），但它不能是 reasoning 的全部
    assert "curl -H" in finding.evidence
    assert finding.reasoning.strip() != finding.evidence.strip()


def test_write_vulnerabilities_passes_code_locations(tmp_path: Path) -> None:
    """`code_locations` 必须进 `affected` —— 白盒扫描的交付物就是它。

    回归 2026-10-26 的缺陷：原代码把 `code_locations` 写死成 `""`，而
    `skills/custom/dependency_cve_scanning.md:231` 要求
    "You MUST report usage sites in `code_locations`"。
    `strix_vulns._affected_positions()` 读不到就退化成只有 target，
    修的人还得自己去翻代码找位置。
    """
    from app.findings import strix_vulns

    records = _write(
        tmp_path,
        [
            {
                "title": "pyyaml 反序列化",
                "severity": "high",
                "target": "requirements.txt",
                "verification": "confirmed",
                "evidence_kind": "static_artifact",
                "evidence": "requirements.txt: pyyaml==5.1",
                "reasoning": "5.1 的 yaml.load 默认走 FullLoader 之外的 UnsafeLoader。",
                "remediation": "升到 6.x 并改用 safe_load。",
                "cwe": "CWE-502",
                "code_locations": [
                    {"file": "app/config/loader.py", "start_line": 42},
                ],
            }
        ],
    )
    assert records[0]["code_locations"] == [
        {"file": "app/config/loader.py", "start_line": 42}
    ]
    parsed = strix_vulns.parse(records, default_target="requirements.txt")
    assert parsed[0].affected == ["app/config/loader.py:42"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # 模型实测会交这几种形状。不归一 = 静默丢弃。
        (None, []),
        ("", []),
        ("src/app.py:42", [{"file": "src/app.py", "start_line": 42}]),
        ("src/app.py", [{"file": "src/app.py", "start_line": None}]),
        (["src/app.py:42"], [{"file": "src/app.py", "start_line": 42}]),
        (["src/app.py"], [{"file": "src/app.py", "start_line": None}]),
        (
            [{"file": "src/app.py", "start_line": 42}],
            [{"file": "src/app.py", "start_line": 42}],
        ),
        # 字符串行号 / 区间写法 / 浮点
        (
            [{"file": "a.py", "start_line": "42"}],
            [{"file": "a.py", "start_line": 42}],
        ),
        (
            [{"file": "a.py", "start_line": "42-45"}],
            [{"file": "a.py", "start_line": 42}],
        ),
        # `path` / `line` 是别名
        ([{"path": "a.py", "line": 7}], [{"file": "a.py", "start_line": 7}]),
        # 没文件名 → 丢弃（没有位置的"位置"没有意义）
        ([{"start_line": 42}], []),
        ([{"file": ""}], []),
        ([123], [{"file": "123", "start_line": None}]),
    ],
)
def test_normalize_code_locations_shapes(raw, expected) -> None:  # noqa: ANN001
    """`code_locations` 的容错归一。模型不会按提示词的形状交数据。"""
    assert agent_runner._normalize_code_locations(raw) == expected


def test_normalize_code_locations_keeps_url_and_windows_path() -> None:
    """`file:line` 的拆分只能切**最后**一个冒号且右侧纯数字。

    否则 `https://h/p:80`、`C:\\x\\y.py:42` 会被切错 —— 而 Windows 路径
    在本项目里是真实存在的（开发机就是 Windows）。
    """
    got = agent_runner._normalize_code_locations(
        ["https://example.test:8443/x", r"C:\repo\app\main.py:42"]
    )
    assert got == [
        {"file": "https://example.test:8443/x", "start_line": None},
        {"file": r"C:\repo\app\main.py", "start_line": 42},
    ]


def test_write_vulnerabilities_workorders_keep_basis_risk_permission(
    tmp_path: Path,
) -> None:
    """IR 工单的 `basis` / `risk` / `permission` 不能丢。

    回归 2026-10-26 的缺陷：`prompts/kernel_ir.md:94-112` 把
    `action`/`basis`/`risk`/`permission` 定为必填，还写明了原因 ——
    "省了 `permission` 就等于让人在不知情的前提下批权限"。而扫描侧
    的 workorder 映射当时只读了 `action` 和 `evidence`（工单契约里
    根本没有 `evidence`），`basis`/`risk`/`permission` 全部丢弃，
    审批人只看到一个光秃秃的动作标题。
    """
    records = _write(
        tmp_path,
        None,
        [
            {
                "action": "阻断 203.0.113.45 对该主机的访问",
                "basis": "时间线第 3、7 条（confirmed）：该 IP 上传了 webshell",
                "risk": "该 IP 若属办公出口，封禁会影响正常业务",
                "permission": "边界防火墙策略写权限",
                "grade": "confirmed",
            }
        ],
    )
    assert len(records) == 1
    got = records[0]
    narrative = got["technical_analysis"]
    assert "【依据】" in narrative and "webshell" in narrative
    assert "【误做风险】" in narrative and "办公出口" in narrative
    assert "【所需权限】" in narrative and "防火墙" in narrative


def test_write_vulnerabilities_workorders_read_grade_not_verification(
    tmp_path: Path,
) -> None:
    """工单的可信度标签是 `grade`，不是 `verification`。

    回归 2026-10-26 的缺陷：原代码读 `item.get("verification")`，而
    `kernel_ir.md` §六 的工单契约里**没有这个键**（只有 `grade`，且取值
    限于 `confirmed` / `needs_follow_up`）。于是每条 IR 工单都被判成
    `needs_follow_up`，哪怕模型明确写了 `"grade": "confirmed"`。
    """
    records = _write(
        tmp_path,
        None,
        [
            {
                "action": "隔离主机",
                "basis": "第 2 条结论",
                "risk": "短暂中断",
                "permission": "EDR 隔离权限",
                "grade": "confirmed",
            },
            {
                "action": "建议排查",
                "basis": "第 5 条",
                "risk": "无",
                "permission": "无",
                # 模型违规写 ruled_out —— 工单里必须降级（"排除了"不产生动作）
                "grade": "ruled_out",
            },
        ],
    )
    by_title = {r["title"]: r for r in records}
    assert by_title["隔离主机"]["verification"] == "confirmed"
    assert by_title["隔离主机"]["confidence"] == "high"
    assert by_title["建议排查"]["verification"] == "needs_follow_up"
    assert by_title["建议排查"]["confidence"] == "medium"


def test_write_vulnerabilities_workorders_flag_missing_segments(tmp_path: Path) -> None:
    """缺段的工单仍要保留，但可信度必须降级且写明缺了什么。

    与 `app/kernel/workorder.py::parse_workorders` 的 `effective_grade`
    同一条纪律：宁可低估不可高估。一条缺 `risk` 的建议**仍然值得人看**
    （"他们想到了要封这个 IP，只是没写风险"），丢掉它等于让真实发现消失。
    """
    records = _write(
        tmp_path,
        None,
        [
            {
                "action": "阻断该 IP",
                "basis": "第 3 条",
                # 故意不给 risk / permission
                "grade": "confirmed",
            }
        ],
    )
    assert len(records) == 1
    rationale = records[0]["confidence_rationale"]
    assert "risk" in rationale
    assert "permission" in rationale
    assert "待跟进" in rationale


def test_write_vulnerabilities_wraps_string_workorders(tmp_path: Path) -> None:
    # 模型偷懒只给一句话的工单，也不能丢。
    records = _write(tmp_path, None, ["检查 cron.d/system-update 的持久化"])
    assert len(records) == 1
    assert "system-update" in records[0]["title"]
    assert records[0]["discovered_by"] == "agent-workorder"


def test_write_vulnerabilities_keeps_both_findings_and_workorders(tmp_path: Path) -> None:
    # 两个列表同时有值时都写，不互相覆盖。
    records = _write(
        tmp_path,
        [
            {
                "title": "渗透发现",
                "severity": "低",
                "target": "http://x/",
                "verification": "confirmed",
                "evidence_kind": "cross_identity",
            }
        ],
        [{"title": "IR 工单", "severity": "中", "verification": "needs_follow_up"}],
    )
    titles = sorted(r["title"] for r in records)
    assert titles == ["IR 工单", "渗透发现"]


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
        image="suvex-sandbox-ir",
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
        image="suvex-sandbox-ir",
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
    """超时收尾的 outcome 带一句人话原因；**没有**收集器时不假造发现。"""

    class _Spec:
        timeout_reason = "扫描超过设置页指定的单次超时（30 分钟），已被引擎停止。"
        finding_sink = None

    outcome = agent_runner._timeout_outcome(_Spec())
    assert outcome.error and "超时" in outcome.error
    assert not outcome.findings


# ══════════════════════════════════════════════════════════
# 3b. ★ v0.73：超时取消不丢已确认发现（活收集器）
#
# 事故：一次 500 轮的渗透扫描跑满 60 分钟被墙钟超时取消，子智能体已确认
# 6 条漏洞，入库 findings=0。旧代码把"超时必有 0 发现"写进了上面那条测试
# 的断言（`assert not outcome.findings`），等于把缺陷固化成了预期行为。
# ══════════════════════════════════════════════════════════


def test_timeout_outcome_harvests_confirmed_findings() -> None:
    """超时收尾时，已登记到 `finding_sink` 的发现必须被收割出来。

    ## 反面对照（变异会红）

    把 `_timeout_outcome` 里 `outcome.findings = unique` 那段删掉（回到
    v0.72 的空 outcome），这条立刻因为没有 findings 而失败。
    """

    class _Spec:
        timeout_reason = "扫描超过设置页指定的单次超时（60 分钟），已被引擎停止。"
        finding_sink = [
            {"title": "CORS 任意源反射", "severity": "medium"},
            {"title": "登录时序枚举", "severity": "medium"},
        ]

    outcome = agent_runner._timeout_outcome(_Spec())
    assert outcome.error and "超时" in outcome.error
    assert isinstance(outcome.findings, list)
    assert len(outcome.findings) == 2
    assert outcome.findings[0]["title"] == "CORS 任意源反射"


def test_timeout_outcome_dedupes_harvested_findings_by_title() -> None:
    """同一个发现被两个子智能体重复交上来时只留一条（按标题去重）。

    `multiagent._merge_outcomes` 用同一条口子，这里跟它保持一致；
    否则超时路径会把同一条漏洞重复入库两次。
    """

    class _Spec:
        timeout_reason = "超时"
        finding_sink = [
            {"title": "CORS 任意源反射", "severity": "medium"},
            {"title": "CORS 任意源反射", "severity": "high"},  # 重复标题
            {"title": "NUL 字节 500", "severity": "low"},
        ]

    outcome = agent_runner._timeout_outcome(_Spec())
    assert [f["title"] for f in outcome.findings] == ["CORS 任意源反射", "NUL 字节 500"]


def test_sink_findings_ignores_non_list() -> None:
    """`FINAL.findings` 是模型给的原始值；非 list 时**不许**塞进收集器。

    ## 这条防的是"把字符串拆成单字符"

    `sink.extend("abc")` 会得到 `['a','b','c']` —— 三条垃圾记录。
    所以 `_sink_findings` 必须先判 `isinstance(findings, list)`。
    """
    from app.kernel import runner as kernel_runner

    class _Spec:
        finding_sink: list[object] = []

    for bogus in ("一条发现的字符串", {"title": "dict 不是 list"}, None, 42):
        kernel_runner._sink_findings(_Spec(), bogus)
    assert _Spec.finding_sink == []

    kernel_runner._sink_findings(_Spec(), [{"title": "真发现"}])
    assert len(_Spec.finding_sink) == 1


def test_sink_findings_noop_when_sink_disabled() -> None:
    """`finding_sink=None`（默认）时是彻底的 no-op —— 不影响 CTF 解题路径。"""

    class _Spec:
        finding_sink = None

    from app.kernel import runner as kernel_runner

    kernel_runner._sink_findings(_Spec(), [{"title": "x"}])  # 不该抛


def test_build_solve_spec_passes_finding_sink_through() -> None:
    """`build_solve_spec_from_scan(finding_sink=...)` 必须把它挂到 SolveSpec 上。

    ## 这条防的是"配了不等于生效"

    收集器要在**建 spec 之前**就存在并挂上去；只改了 `_run_engine` 的局部变量
    而没接进 `SolveSpec`，超时路径依然拿不到任何东西 —— 而这个形状的 bug
    在 v0.72 已经出现过一次（`parse_server_configs` 只吃 list+dict）。
    """
    from app.scans.runner import LaunchSpec

    sink: list[object] = []
    spec = LaunchSpec(
        scan_id="scan_x",
        target="http://example.test",
        target_kind="url",
        skills=("web",),
        brief="",
        goal=None,
        scan_mode="standard",
        scan_max_turns=10,
        run_dir=__import__("pathlib").Path("/tmp/scan_x"),
        log_path=__import__("pathlib").Path("/tmp/scan_x/scan.log"),
        llm_model="openai/x",
        llm_api_key="k",
        llm_api_base="http://gw/v1",
    )
    solve_spec = agent_runner.build_solve_spec_from_scan(spec, finding_sink=sink)
    assert solve_spec.finding_sink is sink

    # 默认不传 = None（对 CTF 解题路径行为逐字节不变）
    default_spec = agent_runner.build_solve_spec_from_scan(spec)
    assert default_spec.finding_sink is None


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




# ══════════════════════════════════════════════════════════
# 4. 续跑：从 events.jsonl 还原动作纪要（v0.70）
# ══════════════════════════════════════════════════════════


def _write_events(run_dir: Path, records: list[dict]) -> None:
    events = run_dir / "agent_run" / "events.jsonl"
    events.parent.mkdir(parents=True, exist_ok=True)
    events.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


def test_resume_digest_reads_tool_calls_only(tmp_path: Path) -> None:
    """纪要只收 tool_call，**不收 reasoning** —— 纪要是"我做过什么"，
    不是"我想过什么"，把推理混进去会让模型以为那些结论已验证过。"""
    _write_events(
        tmp_path,
        [
            {"seq": 1, "kind": "reasoning", "summary": "我在想该先探什么", "payload": {}},
            {"seq": 2, "kind": "tool_call", "summary": "run_shell nmap -sV 1.2.3.4", "payload": {}},
            {"seq": 3, "kind": "tool_call", "summary": "run_shell gobuster dir -u http://x", "payload": {}},
            {"seq": 4, "kind": "reasoning", "summary": "又想了想", "payload": {}},
        ],
    )
    digest = agent_runner._digest_from_events(tmp_path)
    assert isinstance(digest, tuple)
    assert len(digest) == 2
    assert "nmap" in digest[0]
    assert "gobuster" in digest[1]
    # 推理绝不出现。
    assert all("我在想" not in line for line in digest)


def test_resume_digest_skips_malformed_lines(tmp_path: Path) -> None:
    """引擎可能在两次写之间被杀，最后一行常是断的 —— 坏行要跳过不抛。"""
    events = tmp_path / "agent_run" / "events.jsonl"
    events.parent.mkdir(parents=True, exist_ok=True)
    events.write_text(
        json.dumps({"kind": "tool_call", "summary": "run_shell curl http://a", "seq": 1})
        + "\n{this is a broken half line\n"
        + json.dumps({"kind": "tool_call", "summary": "run_shell curl http://b", "seq": 3})
        + "\n",
        encoding="utf-8",
    )
    digest = agent_runner._digest_from_events(tmp_path)
    assert len(digest) == 2
    assert "http://a" in digest[0]
    assert "http://b" in digest[1]


def test_resume_digest_missing_file_is_empty_tuple(tmp_path: Path) -> None:
    """没有 events.jsonl → 空 tuple（当成全新扫描），**不抛**。"""
    digest = agent_runner._digest_from_events(tmp_path)
    assert digest == ()


def test_resume_digest_keeps_only_the_last_n() -> None:
    """纪要是索引不是正文：太多会吃预算。只留最后 `limit` 条。"""
    records = [
        {"kind": "tool_call", "summary": f"run_shell cmd-{i}", "seq": i} for i in range(100)
    ]
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _write_events(root, records)
        digest = agent_runner._digest_from_events(root, limit=10)
    assert len(digest) == 10
    # 保留的是**最后** 10 条（最关心"刚跑到哪"）。
    assert "cmd-99" in digest[-1]
    assert "cmd-0 " not in " ".join(digest)


def test_spec_resume_off_has_empty_digest(tmp_path: Path) -> None:
    """`resume=False`（默认）时 digest 恒空 —— 全新扫描行为逐字节不变。"""
    _write_events(tmp_path, [{"kind": "tool_call", "summary": "run_shell x", "seq": 1}])
    spec = _spec(run_dir=tmp_path)  # 未传 resume
    solve = agent_runner.build_solve_spec_from_scan(spec)
    assert solve.resume_digest == ()


def test_spec_resume_on_pulls_digest(tmp_path: Path) -> None:
    """`resume=True` 时把 events.jsonl 的动作抽成 digest 塞进 SolveSpec。"""
    _write_events(
        tmp_path,
        [
            {"kind": "tool_call", "summary": "run_shell nmap -sV", "seq": 1},
            {"kind": "tool_call", "summary": "run_shell whatweb http://x", "seq": 2},
        ],
    )
    spec = _spec(run_dir=tmp_path, resume=True)
    solve = agent_runner.build_solve_spec_from_scan(spec)
    assert len(solve.resume_digest) == 2
    assert "nmap" in solve.resume_digest[0]


# ══════════════════════════════════════════════════════════
# 4. 证据分级与观察落盘（v0.75）
# ══════════════════════════════════════════════════════════


def test_l0_findings_are_excluded_from_vulnerabilities(tmp_path: Path) -> None:
    """纯推断（没写 evidence_kind）不许进 vulnerabilities.json。

    反向验证：把 `_write_vulnerabilities` 里那道
    `if not evidence_mod.should_enter_findings(level): continue` 删掉，
    本条立刻 FAILED —— 漏洞统计一旦被未验证推断污染，对外报的数字就不再
    可信，这是本项目最贵的一条线。
    """
    records = _write(
        tmp_path,
        [
            {"title": "我猜这里有 SSRF", "evidence_kind": "inferred", "severity": "high"},
            {"title": "确认的越权", "evidence_kind": "cross_identity", "severity": "high"},
        ],
    )
    titles = [r["title"] for r in records]
    assert titles == ["确认的越权"], f"L0 漏进了 findings：{titles}"


def test_self_reported_confirmed_is_downgraded_without_control_experiment(
    tmp_path: Path,
) -> None:
    """模型说 confirmed，但只给了单次响应（L1）→ 必须降成 partial。

    这一条对应 P1 对照里那次真实误报：agent 报「可锁定全部登录」（high），
    实际只是单源限流。**自述不是验证**。
    """
    records = _write(
        tmp_path,
        [
            {
                "title": "限流可锁定全部登录",
                "verification": "confirmed",
                "evidence_kind": "single_response",
                "severity": "high",
            }
        ],
    )
    assert len(records) == 1
    assert records[0]["verification"] == "needs_follow_up"
    assert records[0]["confidence"] == "medium"


def test_control_experiment_keeps_verified(tmp_path: Path) -> None:
    # 反面：给了对照实验（两个身份）就是 L2，confirmed 得以保留。
    records = _write(
        tmp_path,
        [
            {
                "title": "user 令牌可读 admin 列表",
                "verification": "confirmed",
                "evidence_kind": "cross_identity",
                "severity": "high",
            }
        ],
    )
    assert records[0]["verification"] == "confirmed"
    assert records[0]["confidence"] == "high"


def test_observations_file_keeps_negative_and_l0(tmp_path: Path) -> None:
    """`observations.json` 里两样都要有：被排除的（阴性）与未验证的（L0）。

    反向验证：删掉 `_write_vulnerabilities` 末尾的 `_write_observations(...)`
    调用，本条立刻 FAILED —— 这正是 v0.75 之前 ruled_out 随报告蒸发、
    下一轮还要重走死路的原因。
    """
    spec = _spec(run_dir=tmp_path, log_path=tmp_path / "scan.log")
    outcome = _FakeOutcome(
        [
            {"title": "确认的越权", "evidence_kind": "cross_identity"},
            {"title": "我猜的 SSRF", "evidence_kind": "inferred"},
        ]
    )
    outcome.ruled_out = ["alg:none 被拒（401）", "SPA 回退，未知路径不是 404"]
    agent_runner._write_vulnerabilities(spec, outcome)

    obs_path = tmp_path / "observations.json"
    assert obs_path.is_file(), "observations.json 没写出来"
    payload = json.loads(obs_path.read_text(encoding="utf-8"))
    assert payload["schema"] == "suvex.observations/1"

    obs = payload["observations"]
    titles = {o["title"] for o in obs}
    assert "我猜的 SSRF" in titles, "L0 提案被丢了"
    assert "alg:none 被拒（401）" in titles, "阴性结果被丢了"

    negs = [o for o in obs if o["polarity"] == "negative"]
    assert len(negs) == 2
    # 阴性结果一律 L2（能引用），但 polarity 让它们永远不算漏洞
    assert all(o["trust_level"] == "L2" for o in negs)

    levels = payload["levels"]
    assert levels["L0"] == 1
    assert levels["L2"] >= 3  # 1 条越权 + 2 条阴性


def test_observations_not_written_when_nothing_to_say(tmp_path: Path) -> None:
    # 空运行不建空文件 —— 空文件会让"没写"和"没跑"看起来一样。
    agent_runner._write_vulnerabilities(
        _spec(run_dir=tmp_path, log_path=tmp_path / "scan.log"), _FakeOutcome(None)
    )
    assert not (tmp_path / "observations.json").exists()


def test_solve_with_stop_hands_the_wall_clock_to_the_engine(monkeypatch) -> None:
    """★ `_solve_with_stop` 必须把墙钟**写进 spec**，让主循环能提前催促。

    ## 这条钉的是什么

    墙钟原本只活在 `_solve_with_stop` 这个旁路的 `while` 里：到点
    `task.cancel()`，引擎**一无所知**。实测代价（扫描 32231）：
    ```
    墙钟 deadline           : 03:57:36
    最后一条 run_shell 发起 : 03:57:24   ← 只剩 12 秒
    该命令实际跑完          : 03:59:16   ← 逾期 100 秒，全白费
    ```
    而 `scan_max_turns=500` 只用了 78 次工具调用 —— 回合催促一档都没触发。

    ## 为什么断言"两个字段同源"

    见 `agent_runner._solve_with_stop` 里那段注释：`started_at` 只取一次
    `monotonic()`，`deadline` 与写进 spec 的起点都由它算出。分两次取会引入
    漂移，引擎报出的"剩余秒数"就和真正硬杀时刻对不上 —— 那比不催更糟。
    所以这里不只断言"写了"，还断言"**约束成立**"（约等式）。

    ## 变异会红

    · 删掉写入那两行 → `wall_clock_seconds is None` → 红；
    · 改成两次 `monotonic()` 分别取 → `started_at` 与 `deadline - timeout`
      的差超过容忍值 → 红。
    """
    import asyncio
    import threading

    from app.kernel import runner as kernel_runner

    captured: dict[str, object] = {}

    async def _capture(spec, **kwargs):  # noqa: ANN001, ANN003, ANN202
        # 引擎开跑时把 spec 上的值记下来 —— 这正是主循环会读到的东西。
        captured["wall_clock_seconds"] = spec.wall_clock_seconds
        captured["wall_clock_started_at"] = spec.wall_clock_started_at
        return kernel_runner.SolveOutcome(error=None)

    monkeypatch.setattr(kernel_runner, "solve_in_sandbox", _capture)

    spec = agent_runner.build_solve_spec_from_scan(_spec())
    asyncio.run(
        agent_runner._solve_with_stop(
            spec,
            sandbox=object(),
            gateway=object(),
            system_prompt="x",
            stop_event=threading.Event(),
            on_progress=lambda *a, **k: None,
            timeout_seconds=1800,
        )
    )

    assert captured["wall_clock_seconds"] == 1800, (
        "墙钟秒数没交给引擎 —— 主循环读到的会是 None，三档催促一个都不会触发"
    )
    assert captured["wall_clock_started_at"] is not None, (
        "起点没交给引擎 —— 只有总时长没有起点，算不出'已经过了多久'"
    )

    # 起点 / deadline 同源：两者相减必须回到 timeout（容忍毫秒级误差）。
    started = float(captured["wall_clock_started_at"])  # type: ignore[arg-type]
    deadline = started + float(captured["wall_clock_seconds"])  # type: ignore[arg-type]
    import time as _t

    drift = abs((deadline - started) - 1800)
    assert drift < 0.01, f"起点与时长不同源（drift={drift}s）"
    assert started <= _t.monotonic(), "起点不能是未来时刻（那会让剩余时间为负）"


def test_solve_with_stop_does_not_write_the_wall_clock_when_there_is_none(monkeypatch) -> None:
    """`timeout_seconds=None` 时**不许**往 spec 上写 —— 否则引擎会以为有期限。

    `wall_clock_seconds=None` 在引擎里是"没有墙钟"（`_wall_clock_band` 直接
    返回 None）。若这里误写成 0 或某个默认值，非计时的扫描会莫名其妙开始
    收到"快到点了"的催促。
    """
    import asyncio
    import threading

    from app.kernel import runner as kernel_runner

    captured: dict[str, object] = {}

    async def _capture(spec, **kwargs):  # noqa: ANN001, ANN003, ANN202
        captured["wall_clock_seconds"] = spec.wall_clock_seconds
        captured["wall_clock_started_at"] = spec.wall_clock_started_at
        return kernel_runner.SolveOutcome(error=None)

    monkeypatch.setattr(kernel_runner, "solve_in_sandbox", _capture)

    spec = agent_runner.build_solve_spec_from_scan(_spec())
    asyncio.run(
        agent_runner._solve_with_stop(
            spec,
            sandbox=object(),
            gateway=object(),
            system_prompt="x",
            stop_event=threading.Event(),
            on_progress=lambda *a, **k: None,
            timeout_seconds=None,
        )
    )

    assert captured["wall_clock_seconds"] is None
    assert captured["wall_clock_started_at"] is None
