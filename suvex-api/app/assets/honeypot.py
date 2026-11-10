"""蜜罐与反 AI 陷阱的静态信号（v1.82）。

## 定位

借鉴 artex-ymh 的 `honeydetect` 分层思路，**独立实现**（AGPL，不复制源码）：
这里只做 L1 静态签名，不做行为探针、不做登录后检查。它回答"这段
banner/title/响应里有没有已知蜜罐或反 AI 陷阱的特征"。

## 诚实边界

- 命中是**信号**，不是判决；单信号只降级，双信号才建议封锁。
- **全阴性不等于非蜜罐**：高交互/加固蜜罐对静态识别免疫。
- 签名库会随蜜罐版本演进，需要运营。

## 反 AI 陷阱

attestation/自证类响应在真实业务中不存在；出现即高置信标记。
这类信号与普通蜜罐分开标注（`category="anti-ai-trap"`），处置是
"拒绝提交任何数据 + 上报"，不是"继续探测"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Signature:
    id: str
    category: str  # honeypot | anti-ai-trap | tarpit
    confidence: float
    patterns: tuple[str, ...]
    source: str


#: 首批签名。全部来自公开默认配置/研究，**不编造指纹**。
SIGNATURES: tuple[Signature, ...] = (
    Signature(
        "cowrie-ssh-default",
        "honeypot",
        0.95,
        ("ssh-2.0-openssh_6.0p1 debian-4+deb7u2",),
        "Cowrie default banner (Morishita IM'19)",
    ),
    Signature(
        "opencanary-ssh-default",
        "honeypot",
        0.9,
        ("ssh-2.0-openssh_5.1p1 debian-4",),
        "OpenCanary default settings",
    ),
    Signature(
        "hfish-http-default",
        "honeypot",
        0.8,
        ("hfish", "honeypot"),
        "HFish default pages",
    ),
    Signature(
        "glastopf-footer",
        "honeypot",
        0.85,
        ("glastopf",),
        "Glastopf default page",
    ),
    Signature(
        "kippo-bad-packet",
        "honeypot",
        0.85,
        ("bad packet length",),
        "Kippo/twisted SSH behavior",
    ),
    Signature(
        "ai-attestation",
        "anti-ai-trap",
        0.95,
        ("attestation", "client_unclassified", "x-request-context"),
        "Reverse prompt injection / agent-attestation traps",
    ),
    Signature(
        "ai-prompt-exfiltration",
        "anti-ai-trap",
        0.9,
        ("system prompt", "tool list", "show your instructions", "ignore previous instructions"),
        "Reverse prompt injection wording",
    ),
    Signature(
        "tarpit-infinite-links",
        "tarpit",
        0.6,
        ("ai labyrinth", "endless", "nepenthes"),
        "AI crawler tarpits (weak signal; needs behavior check)",
    ),
)


def _haystack(banner: str, title: str, headers: dict[str, str], body: str) -> str:
    parts = [banner or "", title or "", body or ""]
    parts.extend(f"{k}: {v}" for k, v in (headers or {}).items())
    return "\n".join(parts).lower()


def score(
    *,
    banner: str = "",
    title: str = "",
    headers: dict[str, str] | None = None,
    body: str = "",
) -> dict[str, Any]:
    """返回 `{score, verdict, matches, note}`。

    `score` 取命中签名置信度的最大值，命中多个不同类别时加 0.05/个
    （上限 0.99）。`verdict`：>=0.8 confirmed / >=0.5 suspected / none。
    """
    text = _haystack(banner, title, headers or {}, body)
    matches: list[dict[str, Any]] = []
    for signature in SIGNATURES:
        hit = next((pattern for pattern in signature.patterns if pattern in text), None)
        if hit is None:
            continue
        matches.append(
            {
                "id": signature.id,
                "category": signature.category,
                "confidence": signature.confidence,
                "matched": hit,
                "source": signature.source,
            }
        )

    if not matches:
        return {
            "score": 0.0,
            "verdict": "none",
            "matches": [],
            "note": "全阴性不等于非蜜罐：高交互/加固蜜罐对静态识别免疫。",
        }

    best = max(match["confidence"] for match in matches)
    distinct = len({match["category"] for match in matches})
    combined = min(0.99, best + 0.05 * (distinct - 1))
    verdict = "confirmed" if combined >= 0.8 else "suspected"
    return {
        "score": round(combined, 2),
        "verdict": verdict,
        "matches": matches,
        "note": (
            "命中是信号不是判决；单信号只降级交互，双信号才建议封锁。"
            if verdict == "suspected"
            else "高置信：停止在该方向投入高价值动作；反 AI 陷阱一律拒绝自证并上报。"
        ),
    }


def list_signatures() -> list[dict[str, Any]]:
    return [
        {
            "id": signature.id,
            "category": signature.category,
            "confidence": signature.confidence,
            "patterns": list(signature.patterns),
            "source": signature.source,
        }
        for signature in SIGNATURES
    ]


__all__ = ["SIGNATURES", "Signature", "list_signatures", "score"]
