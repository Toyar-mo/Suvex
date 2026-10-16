"""把 strix 的漏洞记录翻成契约 §2.2 的 `Finding`。

## 格式来源（不是猜的）

`vulnerabilities.json` 是一个**列表**，每个元素由
`report/state.py` 的 `add_vulnerability_report` 拼出来，
写入点是 `report/writer.py`。字段全集（逐字取自源码）：

```jsonc
{
  "id": "vuln-0001",                     // strix 自己的编号
  "title": "...",
  "severity": "high",                    // ★ 见下：不是每次都有
  "timestamp": "2026-09-24 10:00:00 UTC", // ⚠️ 不是 ISO 8601
  "description": "...", "impact": "...", "target": "...",
  "technical_analysis": "...",           // ★ 它就是契约里的 reasoning
  "poc_description": "...", "poc_script_code": "...",
  "remediation_steps": "...",            // ★ 契约里的 remediation
  "evidence": "...",
  "assumptions": "...", "counterevidence": "...",   // ★ 见下：诚实分级
  "confidence": "high|medium|low",       // ★ 契约 verification 轴的来源
  "confidence_rationale": "...", "severity_change_conditions": "...",
  "fix_effort": "...", "cvss": 8.1, "cvss_breakdown": {...},
  "endpoint": "...", "method": "...", "cve": "...", "cwe": "...",
  "code_locations": [{"file": "...", "start_line": 12, "fix_before": "...", "fix_after": "..."}],
  "fix_verification": "...", "fix_pr_body": "...",
  "finding_class": "dynamic|dependency_cve",
  "dependency_metadata": {...},
  "agent_id": "...", "agent_name": "..."
}
```

所有可选字段都是**"有才写"**（`if description: report["description"] = ...`），
所以解析时必须全部按"可能不存在"处理。

## ★ 一次对账：任务 4 的一处启发式假设被推翻了

任务 4 里我按"名字最可能对得上"猜了 `create_vulnerability_report` 的参数结构
（`args["severity"]` 等），并在代码里明确标了"需要一次真实扫描来核对"。
这次读 `tools/reporting/tool.py` 的签名，结论是：

* **`severity` 不是参数** —— 它由 `cvss_breakdown` 经 CVSS3 算出来
  （`_calculate_cvss`：`cvss.severities()[0].lower()`，`none` 归一成 `info`）。
  所以任务 4 里读 `args["severity"]` 是**读不到的**，那个分支实际上从来没命中过。
* `confidence` 的合法取值是 `high|medium|low`（源码里的 `_VALID_CONFIDENCE`）。
* `code_locations` 的键是 `file` / `start_line` / `fix_before` / `fix_after`。

所以本模块的 `severity` **优先从 `vulnerabilities.json` 取**（那里一定有），
而不是从 `create_vulnerability_report` 的参数里取 —— 后者根本没有。
任务 4 的解析层也一并按这个结论修正了。

## 两个轴的映射

契约 §2.2 强调 `verification` 与 `status` **绝不可合并**。strix 那边：

| 契约轴 | strix 来源 |
| --- | --- |
| `verification`（证据强度，agent 产出） | `confidence`：`high`→`verified`、`medium`→`partial`、`low`/缺失→`none` |
| `status`（处置状态，人工流转） | 不来自 strix —— 外部工具不产生人的判断，新条目一律 `new` |

这个映射是**语义对应**而不是凑数：strix 的 `confidence` 就是"这条发现有多确信"，
而 `tool.py` 的 docstring 写明 filing 的前提是"100% sure it's a real issue"。
两个轴都在表达"证据有多硬"，方向一致。

## 诚实分级：`counterevidence` 与 `assumptions` 不能丢

这两个字段恰恰是"诚实标注证据强度"的关键材料（**反证**与**假设**）。
契约的 `FindingDetail` 里没有它们的位置，而**丢掉它们是错的** ——
丢掉之后，一条"发现了但存在反证"的记录在界面上和一条铁证如山的记录
长得一模一样。

处理方式：折进 `reasoning`，带明确的小标题。不改契约的字段表，
信息也不丢。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.text import split_fenced_code, strip_nul, strip_nul_deep
from app.core.vocab import FindingCategory, Severity, Verification

logger = logging.getLogger("suvex.findings")

# strix 的 severity 取值 → 契约的 5 档。
# strix 用的是 CVSS3 的分档（none/low/medium/high/critical），
# 而它的 `_calculate_cvss` 已经把 `none` 归一成 `info`，所以两边能直接对上。
# `informational` 是 strix 别处的写法（`report_pdf._normalize_severity` 处理它），
# 这里也一并收下。
_SEVERITY_ALIASES = {
    "critical": Severity.CRITICAL,
    "high": Severity.HIGH,
    "medium": Severity.MEDIUM,
    "low": Severity.LOW,
    "info": Severity.INFO,
    "informational": Severity.INFO,
    "none": Severity.INFO,
}

# strix 的 confidence → 契约的证据强度轴。
# `high` 对应 `verified` 有源码依据：`tools/reporting/tool.py` 的 docstring
# 写明 filing 的前提是 "you're 100% sure it's a real issue"。
_CONFIDENCE_TO_VERIFICATION = {
    "high": Verification.VERIFIED,
    "medium": Verification.PARTIAL,
    "low": Verification.NONE,
}

# CWE 编号 → 契约的 8 个分类。
#
# ⚠️ 这是**映射**，不是等价：CWE 有 900 多条，契约只有 8 个桶。
# 所以只覆盖最常见的那些，认不出来的一律 `misconfiguration`（最中性的兜底），
# 而不是硬塞进 `injection` 那种会误导人的桶。
#
# 编号取自 CWE 官方列表，按"它属于契约的哪一类"归类。
_CWE_TO_CATEGORY: dict[int, FindingCategory] = {
    # 注入类
    77: FindingCategory.INJECTION,  # Command Injection
    78: FindingCategory.INJECTION,  # OS Command Injection
    89: FindingCategory.INJECTION,  # SQL Injection
    90: FindingCategory.INJECTION,  # LDAP Injection
    94: FindingCategory.INJECTION,  # Code Injection
    95: FindingCategory.INJECTION,  # Eval Injection
    917: FindingCategory.INJECTION,  # Expression Language Injection
    # XSS
    79: FindingCategory.XSS,
    80: FindingCategory.XSS,
    81: FindingCategory.XSS,
    83: FindingCategory.XSS,
    87: FindingCategory.XSS,
    # SSRF / 请求伪造
    918: FindingCategory.SSRF,
    441: FindingCategory.SSRF,
    # 认证
    287: FindingCategory.AUTH,
    288: FindingCategory.AUTH,
    290: FindingCategory.AUTH,
    294: FindingCategory.AUTH,
    306: FindingCategory.AUTH,
    307: FindingCategory.AUTH,
    384: FindingCategory.AUTH,
    521: FindingCategory.AUTH,
    522: FindingCategory.AUTH,
    798: FindingCategory.AUTH,
    640: FindingCategory.AUTH,
    # 访问控制
    22: FindingCategory.BROKEN_ACCESS_CONTROL,
    23: FindingCategory.BROKEN_ACCESS_CONTROL,
    284: FindingCategory.BROKEN_ACCESS_CONTROL,
    285: FindingCategory.BROKEN_ACCESS_CONTROL,
    352: FindingCategory.BROKEN_ACCESS_CONTROL,
    425: FindingCategory.BROKEN_ACCESS_CONTROL,
    639: FindingCategory.BROKEN_ACCESS_CONTROL,
    862: FindingCategory.BROKEN_ACCESS_CONTROL,
    863: FindingCategory.BROKEN_ACCESS_CONTROL,
    # 信息泄漏
    200: FindingCategory.INFO_DISCLOSURE,
    201: FindingCategory.INFO_DISCLOSURE,
    209: FindingCategory.INFO_DISCLOSURE,
    215: FindingCategory.INFO_DISCLOSURE,
    312: FindingCategory.INFO_DISCLOSURE,
    319: FindingCategory.INFO_DISCLOSURE,
    538: FindingCategory.INFO_DISCLOSURE,
    548: FindingCategory.INFO_DISCLOSURE,
    # 加密
    295: FindingCategory.CRYPTO,
    310: FindingCategory.CRYPTO,
    326: FindingCategory.CRYPTO,
    327: FindingCategory.CRYPTO,
    328: FindingCategory.CRYPTO,
    330: FindingCategory.CRYPTO,
    338: FindingCategory.CRYPTO,
    916: FindingCategory.CRYPTO,
    # 配置
    16: FindingCategory.MISCONFIGURATION,
    118: FindingCategory.MISCONFIGURATION,
    693: FindingCategory.MISCONFIGURATION,
    942: FindingCategory.MISCONFIGURATION,
    1004: FindingCategory.MISCONFIGURATION,
    1021: FindingCategory.MISCONFIGURATION,
}


@dataclass(slots=True)
class ParsedFinding:
    """一条解析好的发现（还没落库）。

    `source_ref` 是 strix 自己的编号（`vuln-0001`），用作**幂等键** ——
    导入器要反复读同一个文件（扫描还在写），没有它就会重复落库。
    """

    source_ref: str
    title: str
    severity: str
    category: str
    verification: str
    target: str
    discovered_at: datetime
    description: str
    affected: list[str] = field(default_factory=list)
    reasoning: str = ""
    evidence: str = ""
    poc_language: str | None = None
    poc_code: str | None = None
    remediation: str | None = None
    cwe: str | None = None
    cvss_score: float | None = None
    # strix 侧产生它的 agent（内部保留，便于回溯"是哪一次尝试发现的"）
    agent_ref: str | None = None


def vulnerabilities_path(run_dir: Path) -> Path:
    return run_dir / "vulnerabilities.json"


def find_vulnerabilities_file(run_dir: Path) -> Path | None:
    """在运行目录里找 `vulnerabilities.json`。

    正常位置是 `strix_runs/<run>/vulnerabilities.json`（`writer.py` 写在那儿），
    但 run 目录名是 strix 生成的、我们不一定知道，所以做一次浅搜索。
    """
    direct = vulnerabilities_path(run_dir)
    if direct.is_file():
        return direct
    base = run_dir / "strix_runs"
    if not base.is_dir():
        return None
    candidates = [p for p in base.glob("*/vulnerabilities.json") if p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def read_vulnerabilities(run_dir: Path) -> tuple[list[dict[str, Any]], str | None]:
    """读 `vulnerabilities.json`。返回 `(记录列表, 错误)`。

    **坏文件不抛异常，但也绝不静默当成空**：strix 自己遇到损坏会
    `raise RuntimeError`（`state.py` 的注释解释了为什么 —— 静默吞掉会让
    下一次分配同一个 `vuln-0001` 并覆盖磁盘上已有的报告，是数据丢失）。
    我们这边读的是**副本式的消费**，不会写回，所以不必崩；
    但要**返回错误**让调用方知道"这次读到的空不是因为真没有"。
    """
    path = find_vulnerabilities_file(run_dir)
    if path is None:
        return [], None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("读取 %s 失败：%s", path, exc)
        return [], str(exc)
    if not isinstance(data, list):
        return [], "vulnerabilities.json 不是一个列表"
    return [item for item in data if isinstance(item, dict)], None


# ══════════════════════════════════════════════════════════
# 翻译
# ══════════════════════════════════════════════════════════


def parse(records: list[dict[str, Any]], *, default_target: str = "") -> list[ParsedFinding]:
    """把 strix 的漏洞记录翻成我们的形状。

    `default_target` 是扫描的目标 —— strix 的 `target` 字段是**可选**的，
    缺失时用扫描的目标兜底，而不是留空：契约里 `Finding.target` 是非空字符串，
    而"受影响目标"在列表页上要显示。
    """
    parsed: list[ParsedFinding] = []
    for index, record in enumerate(records):
        finding = _parse_one(record, index=index, default_target=default_target)
        if finding is not None:
            parsed.append(finding)
    return parsed


def _parse_one(record: dict[str, Any], *, index: int, default_target: str) -> ParsedFinding | None:
    title = _text(record.get("title"))
    if not title:
        # 没有标题的条目无法在界面上显示。跳过而不是造一个"未命名"——
        # 那会让列表里出现一条点了没内容的记录。
        logger.warning("跳过一条没有 title 的漏洞记录（第 %d 条）", index)
        return None

    code_locations = record.get("code_locations")
    affected = _affected_positions(record, code_locations)
    # `affected` 不走 `_text`（它是从多处拼出来的列表），所以单独过一遍清洗。
    affected = [strip_nul_deep(item) for item in affected]

    poc_code = _text(record.get("poc_script_code")) or None
    # strix 把 agent 生成的脚本**包在带语言标签的 markdown 围栏里**存
    # （`report/writer.py`）。它自己的每个消费者都会先拆围栏 ——
    # TUI 用 `ParseFencedCode`、viewer 用 `parseFencedCode`、
    # PDF 用 `_strip_code_fence`。我们以前不拆，于是围栏行连同
    # ```python 一起进了 `poc_code`，前端的代码块里会多出两行脏东西。
    fenced_language, poc_code = split_fenced_code(poc_code) if poc_code else (None, poc_code)
    remediation = _text(record.get("remediation_steps")) or None

    return ParsedFinding(
        # `id` 缺失时用序号兜底：幂等键不能为空，否则重复导入会重复落库。
        source_ref=_text(record.get("id")) or f"vuln-index-{index}",
        title=title,
        severity=_severity(record.get("severity")),
        category=_category(record),
        # ⚠️ 证据强度轴——来自 strix 的 confidence，**不是**处置状态。
        # 处置状态由人流转，新条目一律 new（见 service.create_from_import）。
        verification=_verification(record.get("confidence")),
        target=_text(record.get("target")) or _text(record.get("endpoint")) or default_target,
        discovered_at=_parse_timestamp(record.get("timestamp")),
        description=_text(record.get("description")),
        affected=affected,
        reasoning=_reasoning(record),
        evidence=_text(record.get("evidence")),
        poc_language=fenced_language or _poc_language(poc_code),
        poc_code=poc_code,
        remediation=remediation,
        cwe=_text(record.get("cwe")) or None,
        cvss_score=_number(record.get("cvss")),
        agent_ref=_text(record.get("agent_id")) or None,
    )


def _severity(value: Any) -> str:
    """severity → 5 档之一。认不出来的**不编**，落到 `info` 并记日志。

    落到 `info` 而不是 `medium`：`info` 在契约里是最低档，
    而把一条严重度不明的发现显示成"中危"是在**放大**它的分量。
    宁可低估也不要高估 —— 一份"全是中危"的报告会让人对整套分级失去信任。
    """
    raw = _text(value).lower()
    mapped = _SEVERITY_ALIASES.get(raw)
    if mapped is None:
        if raw:
            logger.warning("认不出的 severity %r，按 info 处理", raw)
        return Severity.INFO.value
    return mapped.value


def _verification(value: Any) -> str:
    """confidence → 证据强度轴。缺失时 `none`（而不是 `verified`）。"""
    return _CONFIDENCE_TO_VERIFICATION.get(
        _text(value).lower(), Verification.NONE
    ).value


def _category(record: dict[str, Any]) -> str:
    """分类：优先按 CWE 编号映射，映射不到再按标题里的关键词兜底。

    ⚠️ 两步都是**映射**而不是判定。契约只有 8 个桶而 CWE 有 900 多条，
    所以必然有损。认不出来的一律 `misconfiguration`（最中性的兜底），
    而不是塞进 `injection` 那种会误导人的桶 —— 分类是用来筛选的，
    填错比填"其他"更糟。
    """
    cwe = _text(record.get("cwe")).upper()
    for token in (cwe.replace("CWE-", ""), cwe):
        digits = "".join(ch for ch in token if ch.isdigit())
        if digits:
            mapped = _CWE_TO_CATEGORY.get(int(digits))
            if mapped is not None:
                return mapped.value
            break

    title = _text(record.get("title")).lower()
    keywords = (
        (("sql", "command injection", "rce", "injection"), FindingCategory.INJECTION),
        (("xss", "cross-site scripting"), FindingCategory.XSS),
        (("ssrf", "server-side request"), FindingCategory.SSRF),
        (("csrf", "authentication", "auth bypass", "credential", "password"), FindingCategory.AUTH),
        (("idor", "access control", "privilege", "authorization"), FindingCategory.BROKEN_ACCESS_CONTROL),
        (("disclosure", "leak", "exposure", "信息泄露"), FindingCategory.INFO_DISCLOSURE),
        (("crypto", "encryption", "tls", "weak hash"), FindingCategory.CRYPTO),
    )
    for needles, category in keywords:
        if any(needle in title for needle in needles):
            return category.value
    return FindingCategory.MISCONFIGURATION.value


def _reasoning(record: dict[str, Any]) -> str:
    """契约的 `reasoning` = "为什么判断这里有问题"，不要与 description 重复。

    strix 的 `technical_analysis` 正是这个东西，优先用它。
    然后把 **`counterevidence`（反证）与 `assumptions`（假设）** 折进来 ——
    它们恰恰是"诚实标注证据强度"的关键材料，而契约的 `FindingDetail`
    里没有它们的位置。丢掉它们之后，一条"有反证"的记录会和一条铁证
    在界面上长得一模一样。

    用带标签的小标题而不是拼成一坨：读的人要能一眼看出哪一段是反证。
    """
    parts: list[str] = []
    analysis = _text(record.get("technical_analysis"))
    if analysis:
        parts.append(analysis)

    confidence = _text(record.get("confidence")).lower()
    rationale = _text(record.get("confidence_rationale"))
    if rationale:
        parts.append(f"【置信度依据（{confidence or '未标注'}）】{rationale}")

    assumptions = _text(record.get("assumptions"))
    if assumptions:
        parts.append(f"【成立假设】{assumptions}")

    counterevidence = _text(record.get("counterevidence"))
    if counterevidence:
        parts.append(f"【反证】{counterevidence}")

    conditions = _text(record.get("severity_change_conditions"))
    if conditions:
        parts.append(f"【严重度升降条件】{conditions}")

    return "\n\n".join(parts)


def _affected_positions(record: dict[str, Any], code_locations: Any) -> list[str]:
    """受影响位置。可以有多个（契约里 `affected` 是 `string[]`）。

    优先级：代码位置（白盒）→ endpoint + method（黑盒）→ target。
    代码位置来自 `code_locations`，它的键是 `file` / `start_line`
    （依据 `report/sarif.py` 的读取）。
    """
    positions: list[str] = []
    if isinstance(code_locations, list):
        for location in code_locations:
            if not isinstance(location, dict):
                continue
            file_path = _text(location.get("file"))
            if not file_path:
                continue
            line = location.get("start_line")
            positions.append(f"{file_path}:{line}" if line else file_path)

    endpoint = _text(record.get("endpoint"))
    if endpoint:
        method = _text(record.get("method")).upper()
        positions.append(f"{method} {endpoint}".strip() if method else endpoint)

    if not positions:
        fallback = _text(record.get("target"))
        if fallback:
            positions.append(fallback)
    return positions


def _poc_language(code: str | None) -> str | None:
    """PoC 代码的语言 —— 只用在**围栏没给语言**的时候。

    ⚠️ **strix 不存语言**，它只有一个 `poc_script_code` 字符串。
    但那个字符串**通常带围栏且围栏上有语言标签**（`report/writer.py`），
    所以调用方要先过 `split_fenced_code`：围栏说了语言就用围栏的，
    只有围栏没说（或根本没有围栏）才轮到这里的启发式。

    启发式靠 shebang 与常见语法特征，认不出来就写 `text`
    （而不是编一个 `python` —— 前端会据此显示语法高亮，
    猜错会让代码变成一片红）。

    真实做法应该是让 strix 记下语言，那是上游的事；
    这里如实标注这是启发式。
    """
    if not code:
        return None
    head = code.lstrip()[:200]
    if head.startswith("#!"):
        first_line = head.splitlines()[0].lower()
        if "python" in first_line:
            return "python"
        if "bash" in first_line or "/sh" in first_line:
            return "bash"
        if "node" in first_line:
            return "javascript"
    if "curl " in head or head.startswith("$ "):
        return "bash"
    if "import requests" in head or "def " in head or "print(" in head:
        return "python"
    if "fetch(" in head or "require(" in head or "console.log" in head:
        return "javascript"
    if head.startswith(("<?php",)):
        return "php"
    return "text"


def _parse_timestamp(value: Any) -> datetime:
    """strix 的时间戳 → tz-aware datetime。

    ⚠️ **它不是 ISO 8601。** `report/state.py` 写的是
    `strftime("%Y-%m-%d %H:%M:%S UTC")`，形如 `2026-09-24 10:00:00 UTC`。
    直接 `datetime.fromisoformat` 会抛 —— 所以先换掉那个 `UTC` 后缀。

    解析不了就退回"现在"：一条时间戳坏掉的发现不该让整次导入失败，
    而给一个坏值会让它在列表里乱跳（排序靠 `discoveredAt`）。
    """
    text = _text(value)
    if not text:
        return datetime.now(UTC)
    normalized = text.replace(" UTC", "+00:00").replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        logger.warning("认不出的时间戳 %r，按当前时刻处理", text)
        return datetime.now(UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _text(value: Any) -> str:
    """外部记录里的任意值 → 干净的字符串。

    ★ 这里**必须**过一遍 `strip_nul`：它是漏洞所有文本字段的必经之处
    （标题、描述、证据、PoC 代码……都走它）。agent 抓到的 HTTP 响应里
    可能带二进制，而 PostgreSQL 的 text/jsonb 存不了 NUL 字节 ——
    插进去是一条硬错误，整条漏洞都落不了库。见 `app.core.text`。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return strip_nul(value).strip()
    return strip_nul(str(value)).strip()


def _number(value: Any) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
