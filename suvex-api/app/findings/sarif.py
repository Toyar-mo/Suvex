"""把漏洞导出成 SARIF 2.1.0（Static Analysis Results Interchange Format）。

## 为什么要 SARIF，而不是再加一种 CSV

CSV 是"给人看的表"，SARIF 是"给工具链吃的格式"。它已经是 GitHub Code
Scanning、Azure DevOps、大部分 IDE 与 CI 安全门的**通用输入**：一份
SARIF 丢进任何支持它的系统，漏洞就自动变成一条可点开的告警（带位置、
带规则描述、带修复建议）。

我们做渗透评估产出的东西**恰恰是**这些工具链最缺的 —— 真实确认过的、
带证据链的发现，而不是扫描器的原始噪音。导出成 SARIF 等于把"确认过的
发现"直接接进别人已有的漏洞管理流水线，而不要求他们改用我们的平台。

## 设计取舍

- **每条 finding = 一个 result**；按 `ruleId`（用 category 映射到一条通用
  规则，或用 CWE）分组到 `rules`。这里**不**为每条发现造一条规则 ——
  规则是"这类问题长什么样"，结果是"这次在哪发现的"。混在一起会让
  规则表膨胀成结果表的镜像。
- **位置**：SARIF 的 `physicalLocation` 要求文件/行/列。我们的发现多是
  网络目标（URL / host:port），没有"文件行号"。用 `artifactLocation.uri`
  承载 target，行/列用 1（SARIF 校验要求正数；用 1 表示"整个目标"）。
  `detail_affected` 里的具体路径放进 `logicalLocations`。
- **严重度映射**：SARIF 的 `level` 只有 error / warning / note / none 四档，
  比我们的五档粗。映射见 `_SARIF_LEVEL`，并在 `properties.security-severity`
  里保留一个 0–10 的连续值（这是 GitHub Code Scanning 认的扩展字段）。
- **证据**：`message.text` 放"问题是什么 + 为什么这么判"，原始响应证据进
  `properties.evidence`（SARIF 没有原生字段放它，但 `properties` 是自由的
  字典，接收方可以读）。

## 不编造

没有 CVSS 的不填 CVSS，没有 CWE 的不编一个。SARIF 允许这些字段缺失，
硬填一个假的 CWE 比空着更糟 —— 它会让接收方的去重与统计失真。
"""

from __future__ import annotations

import json

from app.core.vocab import FindingCategory, Severity
from app.findings.models import Finding

#: SARIF 2.1.0 的 schema 地址（固定字符串，不是网络资源）。
SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
SARIF_VERSION = "2.1.0"

#: 严重度 → SARIF level（四档）。映射依据：critical/high 是"必须处理"的
#: 错误级；medium 是"应当处理"的警告；low/info 是提示。这与 GitHub 在
#: Code Scanning 里对第三方 SARIF 的展示口径一致。
_SARIF_LEVEL: dict[str, str] = {
    Severity.CRITICAL.value: "error",
    Severity.HIGH.value: "error",
    Severity.MEDIUM.value: "warning",
    Severity.LOW.value: "note",
    Severity.INFO.value: "note",
}

#: 严重度 → security-severity 连续分（0–10，GitHub Code Scanning 用它排序）。
#: 取每个档位的代表分：critical 9.0 / high 7.5 / medium 5.5 / low 3.0 / info 0.5。
#: **这不是 CVSS** —— 是展示排序用的档位中点，绝不写进 cvssScore。
_SECURITY_SEVERITY: dict[str, float] = {
    Severity.CRITICAL.value: 9.0,
    Severity.HIGH.value: 7.5,
    Severity.MEDIUM.value: 5.5,
    Severity.LOW.value: 3.0,
    Severity.INFO.value: 0.5,
}

#: 类别 → 规则名/描述。键是 `FindingCategory` 的真实取值（injection /
#: info_disclosure / auth …），**不是**扫描的 skills（web/api/container）。
#: 认不出的类别用通用规则（`suvex.finding`），绝不编一个不存在的规则名。
_RULE_TITLES: dict[str, str] = {
    FindingCategory.INJECTION.value: "注入类缺陷",
    FindingCategory.BROKEN_ACCESS_CONTROL.value: "越权访问",
    FindingCategory.AUTH.value: "认证与会话缺陷",
    FindingCategory.XSS.value: "跨站脚本",
    FindingCategory.SSRF.value: "服务端请求伪造",
    FindingCategory.MISCONFIGURATION.value: "安全配置缺陷",
    FindingCategory.INFO_DISCLOSURE.value: "信息泄露",
    FindingCategory.CRYPTO.value: "加密与敏感数据缺陷",
}
_GENERIC_RULE_ID = "suvex.finding"
_GENERIC_RULE_TITLE = "Suvex 确认的安全发现"


def _rule_for(finding: Finding) -> tuple[str, str]:
    """返回 (ruleId, ruleTitle)。认不出类别 → 通用规则。"""
    cat = (finding.category or "").strip().lower()
    if cat in _RULE_TITLES:
        return f"suvex.{cat}", _RULE_TITLES[cat]
    return _GENERIC_RULE_ID, _GENERIC_RULE_TITLE


def _level_for(severity: str) -> str:
    return _SARIF_LEVEL.get((severity or "").strip().lower(), "note")


def _message_for(finding: Finding) -> str:
    """message.text：问题是什么 + 为什么这么判（人读得懂的一段话）。"""
    parts = [finding.title or "（未命名发现）"]
    desc = (finding.detail_description or "").strip()
    if desc:
        parts.append(desc)
    reasoning = (finding.detail_reasoning or "").strip()
    if reasoning:
        parts.append(f"判定依据：{reasoning}")
    return "\n\n".join(parts)


def _result_for(finding: Finding) -> dict:
    """一条 finding → 一个 SARIF result。"""
    rule_id, _rule_title = _rule_for(finding)
    # 位置：target 作 artifactLocation.uri，具体路径放 logicalLocations。
    uri = finding.target or "(unknown)"
    logical = [
        {"name": str(p)}
        for p in (finding.detail_affected or [])
        if str(p).strip()
    ]
    physical_location: dict = {
        "artifactLocation": {"uri": uri},
        # SARIF 的行/列必须 >= 1；用 1 表示"整个目标"（我们没有行号概念）。
        "region": {"startLine": 1, "startColumn": 1},
    }
    if logical:
        physical_location["logicalLocations"] = logical

    props: dict = {
        # GitHub Code Scanning 识别的排序字段（档位中点，不是 CVSS）。
        "security-severity": f"{_SECURITY_SEVERITY.get(finding.severity, 0.5):.1f}",
        # 我们自己的严重度原值（五档），保留给能读扩展字段的接收方。
        "suvexSeverity": finding.severity,
        "suvexVerification": finding.verification,
    }
    if finding.cwe:
        # 有 CWE 就带上（不编）。GitHub 会据此关联到 CWE 知识库。
        props["tags"] = [finding.cwe]
    evidence = (finding.detail_evidence or "").strip()
    if evidence:
        props["evidence"] = evidence
    if finding.remediation:
        props["remediation"] = finding.remediation

    result: dict = {
        "ruleId": rule_id,
        "level": _level_for(finding.severity),
        "message": {"text": _message_for(finding)},
        "locations": [{"physicalLocation": physical_location}],
        "properties": props,
    }
    # CVSS：只有真的算过才填（SARIF 的 rule 上没有 cvss，放 result.properties）。
    if finding.cvss_score is not None:
        props["cvssScore"] = finding.cvss_score
    return result


def build_sarif(rows: list[Finding], *, tool_version: str = "1.0.0") -> str:
    """把一批 finding 渲染成 SARIF 2.1.0 JSON 文本。

    `rows` 来自 `service.list_findings`（与 CSV 导出同一批，保证两种导出
    对同一个筛选给出同一批发现）。
    """
    # 按 ruleId 聚合规则（每类一条），保持稳定顺序（按 id 排序）。
    rules_seen: dict[str, str] = {}
    results: list[dict] = []
    for finding in rows:
        rule_id, rule_title = _rule_for(finding)
        rules_seen.setdefault(rule_id, rule_title)
        results.append(_result_for(finding))

    rules = [
        {
            "id": rule_id,
            "name": rule_id,
            "shortDescription": {"text": title},
            # defaultConfiguration.level 给接收方一个"这类问题的默认级别"；
            # 具体 result 的 level 才是这一条的实际级别。
            "defaultConfiguration": {"level": "warning"},
        }
        for rule_id, title in rules_seen.items()
    ]

    doc = {
        "$schema": SARIF_SCHEMA,
        "version": SARIF_VERSION,
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "Suvex",
                        "informationUri": "https://github.com/Toyar-mo/Suvex",
                        "version": tool_version,
                        "rules": rules,
                    }
                },
                "results": results,
            }
        ],
    }
    return json.dumps(doc, ensure_ascii=False, indent=2)


def sarif_summary(rows: list[Finding]) -> dict[str, int]:
    """按 severity 统计条数（供调用方在响应头/日志里快速回报）。"""
    counts: dict[str, int] = {s.value: 0 for s in Severity}
    for finding in rows:
        sev = (finding.severity or "").strip().lower()
        if sev in counts:
            counts[sev] += 1
    return counts


__all__ = ["build_sarif", "sarif_summary", "SARIF_SCHEMA", "SARIF_VERSION"]
