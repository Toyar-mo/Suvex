"""可重放的漏洞验证器（v1.81）。

## 为什么需要这一层

「模型说这里能打穿」和「这里真的能打穿」是两件事。Suvex 已有 L0/L1/L2
证据分级（`app/kernel/evidence.py`），但分级依据是**模型自述的
`evidence_kind`** —— 它只能约束"你有没有声称做过对照"，不能证明"那次对照
真的发生了、且结果可复现"。

市场头部的做法（XBOW / CodeMender）都有一条独立的验证层：XBOW 用
确定性 validator（例如无头浏览器真执行 XSS payload）确认每条发现；
CodeMender 用差分测试、fuzzing、SMT 与 LLM judge 验证补丁。它们的共同点是
**验证动作可重放、结果可复核**，而不是再问模型一次。

本模块先落两类最通用、最不依赖特定漏洞的验证：

1. `replay` —— 同一请求重复 N 次，响应指纹（状态码 / 长度 / 正文哈希 /
   标记词）必须一致。用来排除偶发、缓存、一次性噪声；
2. `differential` —— 对照组 vs 实验组，比较状态码、长度、标记词是否真的
   出现差异。用来证明"这个输入造成了不同结果"（布尔盲注、越权、信息泄露
   的最小判据）。

## 边界（刻意不做的事）

* **不判断 URL 是否在授权范围内**。scope guard 在调用方（agent 工具 /
  沙箱）——这一层只负责"请求发出后结果稳不稳"。
* **不读环境代理**（`trust_env=False`）：与项目里所有出站客户端同一条纪律，
  否则 `HTTP_PROXY` 会把内网地址也劫走。
* **不自动提交 finding**。输出 `ValidationOutcome`，由上层决定它是否足以
  把 L1 升到 L2。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

import httpx

#: 默认超时。验证请求应该比扫描请求更短：它只负责复核，不负责探索。
DEFAULT_TIMEOUT_SECONDS = 10.0

#: 长度差异达到这个比例才算"有差异"。
#:
#: 太小会把动态页面（时间戳/CSRF token）判成差异，太大又会漏掉只差几行的
#: 布尔盲注。0.2 是保守起点：真正的越权/注入通常远超这个幅度，而动态噪声
#: 通常只占几个百分点。
LENGTH_DELTA_RATIO = 0.2


@dataclass(frozen=True)
class HttpRequestSpec:
    """一条可重放的 HTTP 请求。

    `body` 用 bytes：调用方可能发 JSON、表单或原始 payload，这一层不做
    编码猜测。`headers` 原样发送（scope/凭据由调用方负责）。
    """

    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes | None = None


@dataclass(frozen=True)
class ResponseObservation:
    """一次响应的指纹。"""

    status: int
    length: int
    digest: str
    markers: frozenset[str]


@dataclass(frozen=True)
class ValidationOutcome:
    """验证结果。`confidence` 直接对应证据强度。"""

    kind: str
    passed: bool
    confidence: str
    summary: str
    evidence: str
    attempts: int = 0


ClientFactory = Callable[[], httpx.Client]


def _default_client_factory() -> httpx.Client:
    # ★ trust_env=False：与 `app/providers/llm.py`、`app/kernel/attachments.py`
    #   同一条纪律，出站请求不读环境代理。
    return httpx.Client(
        timeout=DEFAULT_TIMEOUT_SECONDS,
        follow_redirects=False,
        trust_env=False,
    )


def _observe(
    client: httpx.Client,
    spec: HttpRequestSpec,
    markers: tuple[str, ...],
) -> ResponseObservation:
    response = client.request(
        spec.method,
        spec.url,
        headers=dict(spec.headers),
        content=spec.body,
    )
    body = response.content
    found = frozenset(marker for marker in markers if marker.encode() in body)
    return ResponseObservation(
        status=response.status_code,
        length=len(body),
        digest=hashlib.sha256(body).hexdigest(),
        markers=found,
    )


def _describe(observation: ResponseObservation) -> str:
    return (
        f"status={observation.status} length={observation.length} "
        f"sha256={observation.digest[:16]} markers={sorted(observation.markers)}"
    )


def replay(
    spec: HttpRequestSpec,
    *,
    attempts: int = 3,
    markers: tuple[str, ...] = (),
    client_factory: ClientFactory | None = None,
) -> ValidationOutcome:
    """同一请求重复发送，检查响应是否稳定。

    判定：
    * 所有指纹一致 → `passed=True / high`；
    * 多数一致（≥2/3）→ `passed=True / medium`（可能是缓存或轻微动态内容）；
    * 都不一致 → `passed=False / low`。
    """
    if attempts < 2:
        raise ValueError("replay 至少需要 2 次请求，否则无法判断稳定性。")
    factory = client_factory or _default_client_factory
    observations: list[ResponseObservation] = []
    with factory() as client:
        for _ in range(attempts):
            observations.append(_observe(client, spec, markers))

    digests = [obs.digest for obs in observations]
    most_common = max(set(digests), key=digests.count)
    matching = digests.count(most_common)
    if matching == attempts:
        passed, confidence = True, "high"
    elif matching >= max(2, attempts - 1):
        passed, confidence = True, "medium"
    else:
        passed, confidence = False, "low"

    return ValidationOutcome(
        kind="replay",
        passed=passed,
        confidence=confidence,
        summary=(
            f"{matching}/{attempts} 次重放响应一致"
            if passed
            else f"仅 {matching}/{attempts} 次重放响应一致，结果不稳定"
        ),
        evidence="\n".join(_describe(obs) for obs in observations),
        attempts=attempts,
    )


def differential(
    control: HttpRequestSpec,
    experiment: HttpRequestSpec,
    *,
    markers: tuple[str, ...] = (),
    client_factory: ClientFactory | None = None,
) -> ValidationOutcome:
    """对照组 vs 实验组，判断输入是否造成了可观测差异。

    只认三种**硬信号**，不做语义猜测：

    * 状态码不同；
    * 标记词只在实验组出现（例如注入 payload 的回显、错误信息）；
    * 正文长度差异 ≥ `LENGTH_DELTA_RATIO`。

    动态内容造成的少量长度波动不会被判成差异。
    """
    factory = client_factory or _default_client_factory
    with factory() as client:
        base = _observe(client, control, markers)
        probe = _observe(client, experiment, markers)

    status_changed = base.status != probe.status
    marker_changed = probe.markers - base.markers != set()
    length_ratio = (
        abs(probe.length - base.length) / max(base.length, 1)
    )
    length_changed = length_ratio >= LENGTH_DELTA_RATIO
    passed = status_changed or marker_changed or length_changed

    reasons: list[str] = []
    if status_changed:
        reasons.append(f"状态码 {base.status} → {probe.status}")
    if marker_changed:
        reasons.append(f"新增标记 {sorted(probe.markers - base.markers)}")
    if length_changed:
        reasons.append(f"长度变化 {base.length} → {probe.length}（{length_ratio:.0%}）")

    return ValidationOutcome(
        kind="differential",
        passed=passed,
        confidence="high" if passed else "low",
        summary=(
            "对照组与实验组存在可观测差异：" + "；".join(reasons)
            if passed
            else "对照组与实验组无可观测差异"
        ),
        evidence=f"control: {_describe(base)}\nexperiment: {_describe(probe)}",
        attempts=2,
    )


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "LENGTH_DELTA_RATIO",
    "HttpRequestSpec",
    "ResponseObservation",
    "ValidationOutcome",
    "differential",
    "replay",
]
