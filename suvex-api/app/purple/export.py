"""检测对抗导出（v1.82）：ATT&CK 层、MISP、Sigma/Suricata 草案。

## 诚实边界

- **ATT&CK 层**与 **MISP** 是事实映射：CWE → 技术编号、finding → IoC。
- **Sigma / Suricata** 是**草案**，不是可直接部署的规则：它们需要按实际
  日志结构/流量方向调整。输出里带 `status: experimental` 与注释，避免
  读者把草案当成品。这一条与 `remediation` 的补丁草案同一纪律。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.findings.models import Finding
from app.purple.models import PurpleRule

#: CWE → ATT&CK 技术编号（只收能直接对应的几类）。
CWE_ATTACK: dict[str, list[str]] = {
    "CWE-89": ["T1190"],
    "CWE-79": ["T1190"],
    "CWE-918": ["T1190"],
    "CWE-22": ["T1190"],
    "CWE-78": ["T1059"],
    "CWE-77": ["T1059"],
    "CWE-502": ["T1203"],
    "CWE-306": ["T1078"],
    "CWE-798": ["T1552.001"],
    "CWE-200": ["T1552"],
}

#: Sigma 草案的关键词（按 CWE）。只作为模板起点。
_SIGMA_KEYWORDS: dict[str, list[str]] = {
    "CWE-89": ["union select", "or 1=1", "sleep(", "benchmark("],
    "CWE-79": ["<script", "javascript:", "onerror="],
    "CWE-918": ["169.254.169.254", "file://", "gopher://"],
    "CWE-78": ["/bin/sh -c", "cmd.exe /c", "powershell -enc"],
    "CWE-22": ["../", "..%2f", "/etc/passwd"],
}


def _severity_to_level(severity: str) -> str:
    return severity if severity in ("low", "medium", "high", "critical") else "medium"


def build_attack_layer(rules: list[PurpleRule]) -> dict[str, Any]:
    """MITRE ATT&CK Navigator 图层（只含有 CWE 映射的规则）。"""
    techniques: dict[str, dict[str, Any]] = {}
    for rule in rules:
        for technique in CWE_ATTACK.get((rule.cwe or "").upper(), []):
            entry = techniques.setdefault(
                technique,
                {"techniqueID": technique, "enabled": True, "comment": "", "score": 0},
            )
            entry["score"] = int(entry["score"]) + 1
            entry["comment"] = (entry["comment"] + f"\n{rule.name}").strip()
    return {
        "name": "Suvex purple coverage",
        "versions": {"attack": "16", "navigator": "5.1.0", "layer": "4.5"},
        "domain": "enterprise-attack",
        "description": "Rules with CWE mapped to ATT&CK techniques; comment lists rule names.",
        "techniques": list(techniques.values()),
    }


def _ioc_type(value: str) -> str | None:
    text = (value or "").strip().lower()
    if text.startswith("http://") or text.startswith("https://"):
        return "url"
    if text.startswith(("ip:",)):
        return "ip-dst"
    parts = text.split(":")
    host = parts[0]
    if host.replace(".", "").isdigit() and host.count(".") == 3:
        return "ip-dst"
    if "." in host and all(p.isdigit() for p in parts[1:]):
        return "ip-dst"
    if "." in host:
        return "domain"
    return None


def build_misp_event(db: Session) -> dict[str, Any]:
    """把未删除的 findings 的 target 转成 MISP 事件（属性去重）。"""
    findings = list(
        db.execute(select(Finding).where(Finding.deleted_at.is_(None))).scalars()
    )
    attributes: dict[tuple[str, str], dict[str, Any]] = {}
    for finding in findings:
        value = (finding.target or "").strip()
        kind = _ioc_type(value)
        if kind is None:
            continue
        key = (kind, value.lower())
        attributes[key] = {
            "type": kind,
            "category": "Network activity",
            "to_ids": True,
            "value": value,
            "comment": f"{finding.title} ({finding.cwe or 'no-cwe'}, {finding.severity})",
        }
    return {
        "Event": {
            "info": "Suvex findings (auto-generated IoCs)",
            "distribution": "0",
            "threat_level_id": "2",
            "analysis": "1",
            "Attribute": list(attributes.values()),
            "Tag": [
                {"name": f"suvex:severity={finding.severity}"}
                for finding in findings[:1]
            ],
        }
    }


def build_sigma_draft(rule: PurpleRule) -> str:
    """一条 Sigma **草案**。关键词只是起点，需按真实日志调整。"""
    keywords = _SIGMA_KEYWORDS.get((rule.cwe or "").upper(), [])
    if not keywords:
        keywords = [rule.title_contains or rule.name]
    lines = [
        "# DRAFT: generated from a Suvex purple rule; adjust logsource/keywords before use.",
        "title: " + f"Suvex detection draft - {rule.name}",
        "id: " + f"suvex-{rule.id}",
        "status: experimental",
        "description: Draft rule for a finding class; not validated against production logs.",
        "logsource:",
        "  category: webserver",
        "detection:",
        "  keywords:",
        *[f"    - {keyword!r}" for keyword in keywords],
        "  condition: keywords",
        "falsepositives:",
        "  - Legitimate input containing SQL/HTML-like keywords",
        "level: " + _severity_to_level(rule.severity),
    ]
    return "\n".join(lines) + "\n"


def build_suricata_draft(finding: Finding, sid: int) -> str:
    """一条 Suricata **草案**：只在 target 是 URL 时给出 host 方向。"""
    host = (finding.target or "").strip()
    if host.startswith(("http://", "https://")):
        host = host.split("://", 1)[1].split("/", 1)[0]
    if not host:
        host = "any"
    return (
        "# DRAFT: direction/sid are placeholders; validate on a mirror before deploy.\n"
        f'alert http $HOME_NET any -> $EXTERNAL_NET any (msg:"Suvex finding: '
        f'{finding.title[:60]}"; flow:to_server; http.host; content:"{host}"; '
        f"nocase; sid:{sid}; rev:1;)\n"
    )


def export_body(db: Session, rules: list[PurpleRule], fmt: str) -> str:
    if fmt == "attack":
        return json.dumps(build_attack_layer(rules), ensure_ascii=False, indent=2) + "\n"
    if fmt == "misp":
        return json.dumps(build_misp_event(db), ensure_ascii=False, indent=2) + "\n"
    if fmt == "sigma":
        return "\n".join(build_sigma_draft(rule) for rule in rules)
    if fmt == "suricata":
        findings = list(
            db.execute(select(Finding).where(Finding.deleted_at.is_(None))).scalars()
        )
        return "\n".join(
            build_suricata_draft(finding, 1_900_000 + index)
            for index, finding in enumerate(findings)
        )
    raise ValueError(f"unsupported format: {fmt}")


__all__ = [
    "build_attack_layer",
    "build_misp_event",
    "build_sigma_draft",
    "build_suricata_draft",
    "export_body",
]
