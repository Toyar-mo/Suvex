"""报告生成（任务 6）。

三条要求逐条对应：
1. 按范围+选项生成，格式对齐前端预览结构 —— `test_document_*` / `test_shape_*`
2. 导出 Markdown（PDF 的决策见 ADR-007）—— `test_markdown_*`
3. 历史存储/下载/删除 —— `test_history_*` / `test_download_*`

## 最要紧的三条断言

* `test_preview_and_generate_share_the_same_ir` —— 契约 §3.5.1 要求
  "预览与导出从同一份数据结构渲染"，那是"预览=导出"的物理保证；
* `test_excluded_count_is_frozen` —— 复现时**不许重算**，
  否则"收录 0 条"的报告会谎称"未发现问题"（**那不是没问题，那是没看**）；
* `test_finding_ids_order_is_frozen` 与 `test_frozen_membership_survives_later_changes`
  —— `findingIds` 的顺序是报告的一部分，生成之后改动漏洞也不许动它。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.findings import service as findings_service
from app.findings.models import Finding
from app.reports import document as ir
from app.reports import service
from app.reports.markdown import render_markdown
from tests.factories import make_scan


def vuln(**overrides) -> dict:
    record = {
        "id": "vuln-0001",
        "title": "调试接口未授权可读环境变量",
        "severity": "high",
        "timestamp": "2026-09-24 10:00:00 UTC",
        "description": "GET /debug/env 未做鉴权即可读取进程环境变量。",
        "technical_analysis": "路由注册在鉴权中间件之前。",
        "evidence": "HTTP/1.1 200 OK\nAWS_SECRET_ACCESS_KEY=***",
        "poc_script_code": "import requests\nprint(requests.get('https://x/debug/env').text)",
        "remediation_steps": "把该路由挂到鉴权中间件之后。",
        "confidence": "high",
        "cwe": "CWE-200",
    }
    record.update(overrides)
    return record


@pytest.fixture()
def scan(db_session: Session, tmp_path: Path) -> Scan:  # noqa: F821
    created = make_scan(db_session, status="completed", scan_id="scan_rpt", target="api.example.com")
    created.run_dir = str(tmp_path / "run")
    Path(created.run_dir).mkdir(parents=True, exist_ok=True)
    return created


@pytest.fixture()
def loaded(db_session: Session, scan) -> list[Finding]:  # noqa: ANN001
    """三条不同严重度/状态的漏洞。"""
    target = Path(scan.run_dir) / "strix_runs" / "run-1"
    target.mkdir(parents=True, exist_ok=True)
    (target / "vulnerabilities.json").write_text(
        json.dumps(
            [
                vuln(id="v1", title="严重问题", severity="critical", confidence="high"),
                vuln(id="v2", title="高危问题", severity="high", confidence="medium"),
                vuln(id="v3", title="低危问题", severity="low", confidence="low"),
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    findings_service.import_from_run(db_session, scan)
    return list(
        db_session.execute(
            select(Finding).where(Finding.scan_id == scan.id).order_by(Finding.source_ref)
        ).scalars()
    )


def _scope(scan) -> service.Scope:  # noqa: ANN001
    return service.Scope(kind="scan", scan_id=scan.id)


# ══════════════════════════════════════════════════════════
# 1. IR 结构
# ══════════════════════════════════════════════════════════


def test_document_matches_the_frontend_shape(db_session: Session, scan, loaded) -> None:  # noqa: ANN001
    """★ IR 的顶层字段集合与前端 `ReportDocument` 逐字一致。

    多一个字段是加法（前端忽略），**少一个会让渲染崩** ——
    所以这里对集合做**相等**断言，不是包含。
    """
    doc = service.preview(db_session, title="测试报告", scope=_scope(scan), options=ir.DEFAULT_OPTIONS)

    assert set(doc) == {
        "meta", "verdict", "stats", "distribution", "entries", "method", "severityDefinitions"
    }
    assert set(doc["meta"]) == {
        "code", "provisional", "title", "scope", "generatedAt", "options", "excludedCount"
    }
    assert set(doc["stats"]) == {
        "total", "bySeverity", "urgent", "verified", "fixed", "targets", "scans"
    }
    assert set(doc["method"]) == {"targets", "scans", "skills", "span"}
    assert set(doc["entries"][0]) == {
        "index", "findingId", "title", "severity", "severityLabel", "categoryLabel",
        "target", "affected", "statusLabel", "verificationLabel", "discoveredAt",
        "description", "reasoning", "evidence", "poc", "remediation", "dispositions",
    }


def test_preview_has_no_code(db_session: Session, scan, loaded) -> None:  # noqa: ANN001
    """★ 草稿的 `meta.code` 是**空串**，`provisional` 是 `true`。

    编号由生成动作分配，生成之前它不存在。草稿显示一个"将来的编号"
    会让预览与下载对不上，而编号恰恰是客户用来对账的东西。
    """
    doc = service.preview(db_session, title="草稿", scope=_scope(scan), options=ir.DEFAULT_OPTIONS)

    assert doc["meta"]["code"] == ""
    assert doc["meta"]["provisional"] is True


def test_entries_are_sorted_by_severity_then_time(db_session: Session, scan, loaded) -> None:  # noqa: ANN001
    """★ 报告内排序：严重度降序，同级按**发现时间升序**。

    同级用升序与列表页的"最近在前"**相反** ——
    报告叙述的是"我们依次发现了什么"。
    """
    doc = service.preview(db_session, title="t", scope=_scope(scan), options=ir.DEFAULT_OPTIONS)

    severities = [entry["severity"] for entry in doc["entries"]]
    assert severities == ["critical", "high", "low"]


def test_distribution_only_contains_non_zero(db_session: Session, scan, loaded) -> None:  # noqa: ANN001
    """`distribution` 只含**有值**的档，且按 SEVERITY_ORDER 排好。

    `bySeverity` 是 `Partial` —— 为 0 的档键不存在。
    """
    doc = service.preview(db_session, title="t", scope=_scope(scan), options=ir.DEFAULT_OPTIONS)

    assert [item["severity"] for item in doc["distribution"]] == ["critical", "high", "low"]
    assert doc["stats"]["bySeverity"] == {"critical": 1, "high": 1, "low": 1}
    assert "medium" not in doc["stats"]["bySeverity"]


def test_verdict_wording_matches_the_frontend(loaded) -> None:  # noqa: ANN001
    """结论措辞逐字一致（前端 `riskVerdict`）。

    报告开头需要一句**能被直接引用**的判断。措辞逐级收敛，
    且刻意避免"可能存在"这类无信息量的模糊表述。
    """
    stats = {"total": 3, "urgent": 1, "verified": 1}
    assert ir.risk_verdict(stats) == (
        "本次范围内共发现 3 个安全问题，其中 1 个为严重或高危，"
        "已通过可复现的验证脚本确认其中 1 个，建议优先处置。"
    )
    assert ir.risk_verdict({"total": 0, "urgent": 0, "verified": 0}) == "本次范围内未发现安全问题。"
    assert "均不构成直接可利用风险" in ir.risk_verdict({"total": 2, "urgent": 0, "verified": 0})


# ══════════════════════════════════════════════════════════
# 2. 选项
# ══════════════════════════════════════════════════════════


def test_options_trim_content_but_keep_conclusions(db_session: Session, scan, loaded) -> None:  # noqa: ANN001
    """★ 两个开关的行为差异（前端注释专门论证过）。

    * `includePoc` 关 → `poc` 为 null，但 **`verificationLabel` 保留**。
      报告仍写"已验证 PoC"，只是不附脚本 —— **结论不因附带材料而消失**；
    * `includeReasoning` 关 → `reasoning` 为 null，但描述与证据仍在。
    """
    doc = service.preview(
        db_session,
        title="t",
        scope=_scope(scan),
        options=ir.ReportOptions(include_poc=False, include_reasoning=False),
    )
    with_poc = next(entry for entry in doc["entries"] if entry["poc"] is None)
    assert with_poc["verificationLabel"]  # 结论标签还在
    assert all(entry["reasoning"] is None for entry in doc["entries"])
    # 证据**不裁剪**：它是结论的依据，去掉后报告就只剩下断言
    assert all(entry["evidence"] for entry in doc["entries"])


def test_include_poc_actually_includes_it(db_session: Session, scan, loaded) -> None:  # noqa: ANN001
    doc = service.preview(
        db_session, title="t", scope=_scope(scan), options=ir.ReportOptions(include_poc=True)
    )
    assert any(entry["poc"] for entry in doc["entries"])


def test_only_confirmed_keeps_confirmed_and_fixed(db_session: Session, scan, loaded) -> None:  # noqa: ANN001
    """★ 「只含已确认」保留 **confirmed + fixed**，不只是 confirmed。

    `fixed` = 曾经确认过、现在修了 —— 从交付角度它仍然发生并处理过，
    把它剔掉会让报告显示"什么都没发现"，那是失真。
    而 `new`（还没定论）与 `false_positive`（已判定不是问题）才该排除 ——
    给客户的报告里放这两种是失职。
    """
    from app.auth.deps import Identity

    identity = Identity(kind="session", label="s", scopes=frozenset({"read", "scan", "admin"}))
    findings_service.change_status(db_session, ids=[loaded[0].id], target="confirmed", note=None, identity=identity)
    findings_service.change_status(db_session, ids=[loaded[1].id], target="confirmed", note=None, identity=identity)
    findings_service.change_status(db_session, ids=[loaded[1].id], target="fixed", note=None, identity=identity)

    doc = service.preview(
        db_session, title="t", scope=_scope(scan), options=ir.ReportOptions(only_confirmed=True)
    )

    statuses = {entry["findingId"]: entry["statusLabel"] for entry in doc["entries"]}
    assert "已确认" in statuses.values()
    assert "已修复" in statuses.values()
    assert len(doc["entries"]) == 2  # 那条 low（new）被排除
    assert doc["meta"]["excludedCount"] == 1


# ══════════════════════════════════════════════════════════
# 3. 范围
# ══════════════════════════════════════════════════════════


def test_scope_filters_by_scan(db_session: Session, scan, loaded) -> None:  # noqa: ANN001
    assert len(service.findings_in_scope(db_session, _scope(scan))) == 3


def test_scope_filters_by_target(db_session: Session, scan, loaded) -> None:  # noqa: ANN001
    scope = service.Scope(kind="target", target="api.example.com")
    assert len(service.findings_in_scope(db_session, scope)) == 3
    assert service.findings_in_scope(db_session, service.Scope(kind="target", target="nope")) == []


def test_scope_range_is_inclusive_of_the_end_day(db_session: Session, scan, loaded) -> None:  # noqa: ANN001
    """★ 时间区间是**闭区间**，且 `to` 要覆盖当天最后一毫秒。

    用户说"到 9月24日"时心里想的是包含这一天的。
    开区间会让"我明明选了今天"发现少一条。
    """
    from datetime import datetime

    # 漏洞的 discoveredAt 是 2026-09-24 10:00:00 UTC
    scope = service.Scope(
        kind="range",
        from_=datetime.fromisoformat("2026-09-24T00:00:00+00:00"),
        to=datetime.fromisoformat("2026-09-24T00:00:00+00:00"),  # 只给到当天 0 点
    )
    assert len(service.findings_in_scope(db_session, scope)) == 3, "当天的发现被漏掉了"


def test_scope_rejects_mixed_shapes() -> None:
    """★ scope 是 union，不是"三个可选字段"（契约 §2.7）。

    三个可选字段会允许 `scanId` 和 `target` 同时有值，
    而"到底按哪个过滤"要靠读实现猜。
    """
    with pytest.raises(ValueError):
        service.Scope(kind="scan", scan_id="s", target="t")
    with pytest.raises(ValueError):
        service.Scope(kind="range", from_=None, to=None)


# ══════════════════════════════════════════════════════════
# 4. 冻结语义（报告是交付物）
# ══════════════════════════════════════════════════════════


def test_preview_and_generate_share_the_same_ir(db_session: Session, scan, loaded) -> None:  # noqa: ANN001
    """★★★ 契约 §3.5.1：**预览与导出必须走同一份 IR**。

    这条是"预览效果和导出内容一致"的**物理保证**。
    两者唯一允许的差别是 `meta.code` / `meta.provisional` ——
    预览没有编号（还没分配），生成之后有。

    所以这里的断言方式是：把预览的 code/provisional 归一化之后，
    **整份文档必须逐字节相同**。
    """
    options = ir.ReportOptions(include_poc=True, include_reasoning=True)

    preview = service.preview(db_session, title="对齐测试", scope=_scope(scan), options=options)
    report = service.generate(
        db_session, title="对齐测试", scope=_scope(scan), options=options, fmt="markdown"
    )
    generated = service.document_for(db_session, report)

    # 归一化两个"本来就该不同"的字段
    preview_norm = json.loads(json.dumps(preview, ensure_ascii=False, default=str))
    generated_norm = json.loads(json.dumps(generated, ensure_ascii=False, default=str))
    preview_norm["meta"]["code"] = "<code>"
    generated_norm["meta"]["code"] = "<code>"
    preview_norm["meta"]["provisional"] = False
    # `generatedAt` 也会不同（预览与生成是两个时刻）
    preview_norm["meta"]["generatedAt"] = "<at>"
    generated_norm["meta"]["generatedAt"] = "<at>"

    assert preview_norm == generated_norm, "预览与生成的内容不一致"


def test_excluded_count_is_frozen(db_session: Session, scan, loaded) -> None:  # noqa: ANN001
    """★★★ 复现时**不许重算** `excludedCount`。

    重算的后果（前端注释里的原话）：
    冻结快照里只有**被收录的** id，所以 `inScope - selected` 恒为 0，
    于是一份"只含已确认"、恰好把 2 条待确认全筛掉的报告
    会写着「本次范围内未发现问题」—— **那不是没问题，那是没看。**
    """
    from app.auth.deps import Identity

    identity = Identity(kind="session", label="s", scopes=frozenset({"read", "scan", "admin"}))
    findings_service.change_status(db_session, ids=[loaded[0].id], target="confirmed", note=None, identity=identity)

    report = service.generate(
        db_session,
        title="只含已确认",
        scope=_scope(scan),
        options=ir.ReportOptions(only_confirmed=True),
        fmt="markdown",
    )
    assert report.excluded_count == 2  # 另外两条被筛掉
    assert report.finding_count == 1

    # 复现：排除数必须仍然是 2，而不是重算出来的 0
    doc = service.document_for(db_session, report)
    assert doc["meta"]["excludedCount"] == 2
    assert "另有 2 条" in render_markdown(doc)


def test_finding_ids_order_is_frozen(db_session: Session, scan, loaded) -> None:  # noqa: ANN001
    """★ `findingIds` 的顺序是报告的一部分（严重度降序）。"""
    report = service.generate(
        db_session, title="t", scope=_scope(scan), options=ir.DEFAULT_OPTIONS, fmt="markdown"
    )
    out = service.to_out(report)

    # 顺序 = 严重度降序，与 IR 的 entries 一致
    doc = service.document_for(db_session, report)
    assert out["findingIds"] == [entry["findingId"] for entry in doc["entries"]]
    assert len(out["findingIds"]) == 3


def test_frozen_membership_survives_later_changes(db_session: Session, scan, loaded) -> None:  # noqa: ANN001
    """★★ 生成之后改动漏洞，**报告收录的条目集合与顺序不变**。

    ★ **这里要说清"冻结"到底冻结了什么** —— 契约 §2.7 的原文是
    「报告内容在生成时**冻结**」，听起来像是全部内容都冻结，
    但**前端自己的实现只冻结三样**（`rebuildReportDocument` 的实际行为）：

    | 冻结的 | 不冻结的 |
    | --- | --- |
    | `findingIds`（**收录哪些条目**） | 条目的 `statusLabel`（处置状态） |
    | `position`（**报告内顺序**） | 条目的 `discovery` 文本 |
    | `excludedCount` | —— |

    前端的 `rebuildReportDocument` 拿**活的** `allFindings` 按冻结 id 取回，
    所以复现出来的报告里 `statusLabel` 反映的是**当前**状态。

    后端必须**照做**，否则"预览=导出"就破了（那是任务 6 的验收标准）：
    前端预览显示"已修复"，后端下载的文件里写着"待确认"，客户拿到的和看到的不一致。

    > 如实记一处**前端注释与它的实现之间的落差**：注释里那句
    > 「三个月后回看会看到一份'和当初不一样'的报告」论证的是
    > **收录集合**要冻结，而实现也确实只冻结了集合与顺序。
    > 条目内的文本（描述、证据）本来就不会变（它们来自 agent 的产出），
    > 唯一会变的就是处置状态 —— 而"这条当初在报告里、现在修了"
    > 对读者其实是**更有用**的信息。
    """
    report = service.generate(
        db_session, title="t", scope=_scope(scan), options=ir.DEFAULT_OPTIONS, fmt="markdown"
    )
    before_ids = service.to_out(report)["findingIds"]

    # 改动其中一条的状态
    from app.auth.deps import Identity

    identity = Identity(kind="session", label="s", scopes=frozenset({"read", "scan", "admin"}))
    findings_service.change_status(
        db_session, ids=[loaded[0].id], target="confirmed", note=None, identity=identity
    )

    out = service.to_out(report)
    assert out["findingIds"] == before_ids, "收录集合变了 —— 交付物的成员不该变"

    # 条目数不变、顺序不变
    doc = service.document_for(db_session, report)
    assert [entry["findingId"] for entry in doc["entries"]] == before_ids


def test_entry_status_reflects_current_state_like_the_frontend(
    db_session: Session, scan, loaded
) -> None:  # noqa: ANN001
    """★ 条目里的 `statusLabel` 是**当前**状态 —— 这是与前端一致的行为。

    单独一条测试钉住它，是因为它看起来像 bug（"报告不是冻结的吗"），
    而实际是与前端对齐的**有意**行为。不写清楚的话，
    下一个人会"修"掉它，然后预览与导出就对不上了。
    """
    report = service.generate(
        db_session, title="t", scope=_scope(scan), options=ir.DEFAULT_OPTIONS, fmt="markdown"
    )
    target_id = service.to_out(report)["findingIds"][0]

    from app.auth.deps import Identity

    identity = Identity(kind="session", label="s", scopes=frozenset({"read", "scan", "admin"}))
    findings_service.change_status(db_session, ids=[target_id], target="confirmed", note=None, identity=identity)

    doc = service.document_for(db_session, report)
    entry = next(item for item in doc["entries"] if item["findingId"] == target_id)
    assert entry["statusLabel"] == "已确认"


def test_frozen_excluded_count_and_options_survive(db_session: Session, scan, loaded) -> None:  # noqa: ANN001
    """冻结的那三样里，`excludedCount` 与 `options` 都不随时间变。"""
    report = service.generate(
        db_session,
        title="t",
        scope=_scope(scan),
        options=ir.ReportOptions(only_confirmed=True),
        fmt="markdown",
    )
    doc = service.document_for(db_session, report)

    assert doc["meta"]["excludedCount"] == report.excluded_count == 3
    assert doc["meta"]["options"]["onlyConfirmed"] is True


def test_document_uses_the_frozen_options(db_session: Session, scan, loaded) -> None:  # noqa: ANN001
    """复现时用**当时**的选项，而不是当前界面上的选择。"""
    report = service.generate(
        db_session,
        title="含 PoC",
        scope=_scope(scan),
        options=ir.ReportOptions(include_poc=True),
        fmt="markdown",
    )
    doc = service.document_for(db_session, report)

    assert doc["meta"]["options"]["includePoc"] is True
    assert any(entry["poc"] for entry in doc["entries"])


# ══════════════════════════════════════════════════════════
# 5. Markdown
# ══════════════════════════════════════════════════════════


def test_markdown_has_all_sections(db_session: Session, scan, loaded) -> None:  # noqa: ANN001
    """Markdown 的五段与三个附录都在。"""
    report = service.generate(
        db_session, title="完整报告", scope=_scope(scan), options=ir.DEFAULT_OPTIONS, fmt="markdown"
    )
    _, content = service.render_for_download(db_session, report)

    assert content.startswith("# 完整报告")
    for heading in ("## 一、摘要", "## 二、风险总览", "## 三、漏洞详情"):
        assert heading in content
    for appendix in ("## 附录 A · 检测范围与方法", "## 附录 B · 严重程度说明", "## 附录 C · 内容范围"):
        assert appendix in content
    # 报告编号出现在正文里（客户拿它对账）
    assert report.code in content


def test_markdown_escapes_pipes_in_tables(db_session: Session, tmp_path: Path) -> None:  # noqa: ANN001
    """★ 表格里的 `|` 必须转义，否则整行列会错位。

    目标名出现竖线完全可能（`api.example.com|prod`），
    而它进的是附录 A 的扫描表。错位的表格**不报错**，只会静默地少一列。

    （第一版把断言写在了 `affected` 列表上 —— 那些是 `- \\`x\\`` 形式的
    列表项，本来就不该有转义。测试要盯着**真的进表格**的字段。）
    """
    scan = make_scan(db_session, status="completed", scan_id="scan_pipe", target="api.example.com|prod")
    scan.run_dir = str(tmp_path / "run")
    Path(scan.run_dir).mkdir(parents=True, exist_ok=True)
    target = Path(scan.run_dir) / "strix_runs" / "run-1"
    target.mkdir(parents=True, exist_ok=True)
    (target / "vulnerabilities.json").write_text(
        json.dumps([vuln(id="v1", target="api.example.com|prod", title="竖线目标")]),
        encoding="utf-8",
    )
    findings_service.import_from_run(db_session, scan)

    report = service.generate(
        db_session, title="竖线", scope=service.Scope(kind="scan", scan_id=scan.id),
        options=ir.DEFAULT_OPTIONS, fmt="markdown",
    )
    _, content = service.render_for_download(db_session, report)

    # 附录 A 的扫描表里有这个目标，竖线必须被转义
    assert "api.example.com\\|prod" in content

    # 转义之后，表格行的列数必须还对。
    #
    # ⚠️ 数的时候要把 `\|` 当成**一个字符**，否则转义后的竖线会被
    # 再一次当成列分隔符 —— 那样测试会在转义**正确**的时候失败
    # （第一版就是这么栽的）。
    for line in content.splitlines():
        if not line.startswith("| ") or "---" in line or "扫描名称" in line:
            continue
        cells = [cell for cell in line.strip("|").replace("\\|", "\x00").split("|")]
        assert len(cells) in {2, 3, 4}, f"表格列数不对（可能被竖线撑开）：{line}"


def test_markdown_uses_four_backtick_fence_when_needed(db_session: Session, tmp_path: Path) -> None:  # noqa: ANN001
    """★ 内容含三反引号时围栏换成**四个**。

    三反引号会让围栏提前闭合，**后面的内容全变成正文** ——
    这是 Markdown 生成器最典型的静默损坏点。
    """
    scan = make_scan(db_session, status="completed", scan_id="scan_fence", target="x.example.com")
    scan.run_dir = str(tmp_path / "run")
    Path(scan.run_dir).mkdir(parents=True, exist_ok=True)
    target = Path(scan.run_dir) / "strix_runs" / "run-1"
    target.mkdir(parents=True, exist_ok=True)
    (target / "vulnerabilities.json").write_text(
        json.dumps([vuln(id="v1", evidence="响应里嵌着 ```json\n{}\n``` 这样的块")]),
        encoding="utf-8",
    )
    findings_service.import_from_run(db_session, scan)

    report = service.generate(
        db_session, title="围栏", scope=service.Scope(kind="scan", scan_id=scan.id),
        options=ir.DEFAULT_OPTIONS, fmt="markdown",
    )
    _, content = service.render_for_download(db_session, report)

    assert "````http" in content


def test_markdown_reasoning_section_disappears_when_trimmed(db_session: Session, scan, loaded) -> None:  # noqa: ANN001
    """推理被选项裁掉时**整个小节不出现**，而不是留个空标题。"""
    report = service.generate(
        db_session,
        title="不含推理",
        scope=_scope(scan),
        options=ir.ReportOptions(include_reasoning=False),
        fmt="markdown",
    )
    _, content = service.render_for_download(db_session, report)

    assert "#### 判断依据" not in content


def test_markdown_discloses_excluded_entries(db_session: Session, scan, loaded) -> None:  # noqa: ANN001
    """★ 排除必须**写在报告里**。

    "一个诚实的安全报告，在给出'未发现'这个结论时，
    有义务同时说明有多少内容没被纳入。"
    """
    from app.auth.deps import Identity

    identity = Identity(kind="session", label="s", scopes=frozenset({"read", "scan", "admin"}))
    findings_service.change_status(db_session, ids=[loaded[0].id], target="confirmed", note=None, identity=identity)

    report = service.generate(
        db_session, title="t", scope=_scope(scan),
        options=ir.ReportOptions(only_confirmed=True), fmt="markdown",
    )
    _, content = service.render_for_download(db_session, report)

    assert "未纳入" in content
    assert "2 条" in content


def test_empty_report_says_so_honestly(db_session: Session, tmp_path: Path) -> None:  # noqa: ANN001
    """范围内一条都没有时，报告要说"未发现"，且**不出现"涉及目标 0 个"**。

    附录的覆盖目标**以"测了什么"为准，而不是"从中发现了什么"**——
    否则一次"测过但干净"的扫描会显示成"什么都没测"，与事实完全相反。
    """
    scan = make_scan(db_session, status="completed", scan_id="scan_clean", target="clean.example.com")

    report = service.generate(
        db_session, title="干净", scope=service.Scope(kind="scan", scan_id=scan.id),
        options=ir.DEFAULT_OPTIONS, fmt="markdown",
    )
    doc = service.document_for(db_session, report)

    assert doc["stats"]["total"] == 0
    assert doc["verdict"] == "本次范围内未发现安全问题。"
    # 覆盖目标退回扫描自身的目标 —— 那才是这次检测的真实覆盖面
    assert doc["method"]["targets"] == ["clean.example.com"]
    assert "涉及目标 | 1 个" in render_markdown(doc)


# ══════════════════════════════════════════════════════════
# 6. HTTP 层
# ══════════════════════════════════════════════════════════


def test_report_endpoints_require_auth(client: TestClient) -> None:
    assert client.get("/api/v1/reports").status_code == 401
    assert client.post("/api/v1/reports/preview", json={}).status_code == 401


def test_preview_route_is_not_shadowed(ready_client: TestClient, scan, loaded) -> None:  # noqa: ANN001
    """★ `/reports/preview` 不能被 `/{reportId}` 抢先匹配。

    路由顺序错了的症状是「预览接口 404 找不到报告 preview」。
    """
    response = ready_client.post(
        "/api/v1/reports/preview",
        json={
            "title": "预览",
            "scope": {"kind": "scan", "scanId": scan.id},
            "options": {},
        },
    )

    assert response.status_code == 200
    assert response.json()["meta"]["provisional"] is True


def test_generate_then_download(ready_client: TestClient, scan, loaded) -> None:  # noqa: ANN001
    """完整的生成 → 下载回路，文件名用**报告编号**。"""
    created = ready_client.post(
        "/api/v1/reports",
        json={
            "title": "下载测试",
            "scope": {"kind": "scan", "scanId": scan.id},
            "options": {},
            "format": "markdown",
        },
    )
    assert created.status_code == 201
    report = created.json()

    download = ready_client.get(f"/api/v1/reports/{report['id']}/download")

    assert download.status_code == 200
    assert download.headers["content-type"].startswith("text/markdown")
    # 文件名用编号而不是标题（标题是中文，直接当文件名会有编码差异）
    assert f'filename="{report["code"]}.md"' in download.headers["content-disposition"]
    assert "下载测试" in download.text


def test_download_of_a_pdf_report_explains_instead_of_404(
    ready_client: TestClient, scan, loaded
) -> None:  # noqa: ANN001
    """`format=pdf` 的报告被下载时给出**说明**，而不是 404。

    404 会让前端以为"这份报告不存在"，而它存在，只是没有服务端 PDF
    （契约 §3.5.1 定的：本轮 PDF 走浏览器打印）。
    """
    created = ready_client.post(
        "/api/v1/reports",
        json={
            "title": "PDF 报告",
            "scope": {"kind": "scan", "scanId": scan.id},
            "options": {},
            "format": "pdf",
        },
    ).json()

    download = ready_client.get(f"/api/v1/reports/{created['id']}/download")

    assert download.status_code == 200
    assert download.headers.get("x-suvex-note") == "pdf-via-browser-print"
    assert "浏览器打印" in download.text


def test_history_list_and_delete(ready_client: TestClient, scan, loaded) -> None:  # noqa: ANN001
    created = ready_client.post(
        "/api/v1/reports",
        json={"title": "历史", "scope": {"kind": "scan", "scanId": scan.id}, "options": {}},
    ).json()

    listed = ready_client.get("/api/v1/reports").json()
    assert [item["id"] for item in listed] == [created["id"]]

    assert ready_client.delete(f"/api/v1/reports/{created['id']}").status_code == 204
    assert ready_client.get("/api/v1/reports").json() == []
    assert ready_client.get(f"/api/v1/reports/{created['id']}").status_code == 404


def test_deleting_a_report_removes_its_links(db_session: Session, scan, loaded) -> None:  # noqa: ANN001
    """删除报告要级联删掉 `report_findings` 的关联行。

    留下悬空关联会让复现时读到一堆找不到的 id。
    """
    from app.reports.models import ReportFinding

    report = service.generate(
        db_session, title="t", scope=_scope(scan), options=ir.DEFAULT_OPTIONS, fmt="markdown"
    )
    assert db_session.execute(select(ReportFinding)).scalars().all()

    service.delete_report(db_session, report.id)

    assert db_session.execute(select(ReportFinding)).scalars().all() == []
    # 但漏洞本身**不受影响** —— 报告是派生物，删它不该动原始数据
    assert len(db_session.execute(select(Finding)).scalars().all()) == 3


def test_scope_validation_rejects_mixed_shapes_over_http(ready_client: TestClient) -> None:
    response = ready_client.post(
        "/api/v1/reports/preview",
        json={
            "title": "x",
            "scope": {"kind": "scan", "scanId": "s", "target": "t"},
            "options": {},
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_range_scope_accepts_the_from_key(ready_client: TestClient) -> None:
    """★ `from` 是 Python 关键字，但**契约的字段名就是 `from`**。

    进出参都必须是 `from` —— 不接受它会让前端的区间报告整个用不了。
    """
    response = ready_client.post(
        "/api/v1/reports/preview",
        json={
            "title": "区间",
            "scope": {"kind": "range", "from": "2026-09-01T00:00:00.000Z", "to": "2026-09-30T00:00:00.000Z"},
            "options": {},
        },
    )

    assert response.status_code == 200, response.text
    assert "至" in response.json()["meta"]["scope"]
