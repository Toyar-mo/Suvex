"""证据分级与阴性结果留存（v0.75，学 Taric 的 L0/L1/L2 治理边界）。

## 要解决的问题

AI 渗透最大的可信度问题是**误报**：模型说"这里能越权"，人得逐条回测。
v0.73 的 P1 对照实测就撞上过一次——agent 报「认证限流可锁定全部登录」
（high），strix 报同一条为「只锁自身出口 IP」（medium），我手工换源 IP
实测后确认 **agent 那条是误报**。

根因不是模型不会做事，而是**产出没有证据等级**：
"我跑了个请求看到 200" 与 "我用两个不同身份对比确认了越权" 在库里
长得一模一样，都进 `findings` 表，severity 也由模型自己写。

Taric 的做法是把这件事拆成**三个信任级别**，并且明确一条铁律：

    「机审通过 ≠ 事实正确，L1 ≠ 已验证 Finding」

本模块把那套边界落成 Python 侧的最小等价物。

## 三级定义

| 级别 | 名称 | 含义 | 能否进 `findings` 表 |
|---|---|---|---|
| `L0` | 提案 | 模型自述，无独立证据支撑 | ❌ 只能进 `observations` |
| `L1` | 机审候选 | 有工具输出佐证，但**同一身份/同一路径**自证 | ⚠️ 可进，但 `verification=partial` |
| `L2` | 独立验证 | 有**对照实验**（两个身份 / 两个源 IP / 前后对比）| ✅ `verification=verified` |

判级**不由模型自由发挥**：模型只能在结构化字段里声明"我怎么验的"
（`evidence_kind`），由本模块按固定规则折算成级别。这与 v0.72 那个
「`mcp_servers` 配了但静默失效」是同一个教训的反面用法——
**声明不是检查**，检查得由代码做。

## 阴性结果为什么也要存

`ruled_out` 现在是模型在 `FINAL` 里写的一段自由文本，扫描结束就跟着
报告消失。后果有两个：

1. **重复劳动**：下一次扫描（或另一个 agent、另一份报告）会重新排一遍
   同样的死路；
2. **无法自证工作量**：答辩时只能说"我们排除了不少"，说不出"排除了
   哪 15 条、每条的反例是什么"。

所以阴性结果和阳性发现一样落库，只是 `polarity="negative"`、
永不进 `findings` 表（它不是漏洞，是**已排除的可能性**）。

## 与既有代码的边界

本模块**只做纯函数**：判级、归一、构造记录。落库仍由
`app/scans/agent_runner.py` 写到 `run_dir/observations.json`，
读库仍走既有 `findings` 域。这样 `kernel` 不 import DB 层，
与 `runner` / `workorder` 的一贯切分保持一致。
"""

from __future__ import annotations

from typing import Any, Literal

#: 信任级别。顺序有意义：`TRUST_RANK` 越大越可信。
TrustLevel = Literal["L0", "L1", "L2"]

TRUST_RANK: dict[str, int] = {"L0": 0, "L1": 1, "L2": 2}

#: 人话标签，给报告与界面用。
TRUST_LABEL: dict[str, str] = {
    "L0": "提案（未验证）",
    "L1": "机审候选（需人工复核）",
    "L2": "独立验证",
}

#: 模型可以声明的"我是怎么验的"。**这张表就是判级规则本身** ——
#: 加新kind 必须同时想清楚它够不够得上 L2。
#:
#: 判据只有一条：**L2 必须含有对照实验**。对照实验的定义是"把同一个
#: 假设放到两个不同条件下各测一次，结果不同"。没有对照，就只是
#: "我发了个请求，它返回了我预期的东西"——那连 L1 都勉强（可能是
#: 缓存、可能是别人的响应、可能是我读错了）。
EVIDENCE_KIND_TO_LEVEL: dict[str, str] = {
    # ── L2：有对照实验 ──
    #: 两个身份的响应对比（越权/BOLA/BFLA 的唯一有效证法）
    "cross_identity": "L2",
    #: 两个源 IP / 两次不同来源的对比（限流、IP 信任、SSRF 回连）
    "cross_origin": "L2",
    #: 改动前后对比（配置生效、修复验证）
    "before_after": "L2",
    #: 带外回连实际收到（SSRF/RCE 的硬证据，比响应体可靠）
    "out_of_band": "L2",
    #: 时间侧信道：有对照组的中位数差（不是"感觉慢"）
    "timing_control": "L2",
    # ── L1：有工具输出，但没有对照 ──
    #: 单次请求的原始响应（状态码/头/体），同身份自证
    "single_response": "L1",
    #: 静态产物（源码片段、配置、日志行）
    "static_artifact": "L1",
    #: 报错信息差异（能说明"处理路径不同"，但不等于可利用）
    "error_differential": "L1",
    # ── L0：纯自述 ──
    #: 模型根据经验推断，没有实际执行
    "inferred": "L0",
}

#: 模型没写 `evidence_kind` 时按 L0 处理。
#:
#: 理由：默认给高等级会让"忘了写"变成"自动升级"，那是把可信度
#: 建立在模型自觉上。默认最低级，写清楚才升 —— 与 `verification`
#: 的默认值 `needs_follow_up` 同一条纪律。
DEFAULT_LEVEL: str = "L0"


def level_for_evidence_kind(kind: object) -> str:
    """把模型声明的证据类型折算成信任级别。

    认不出的 kind 一律 L0（不猜、不提拔）。空/非字符串同理。
    """
    if not isinstance(kind, str):
        return DEFAULT_LEVEL
    return EVIDENCE_KIND_TO_LEVEL.get(kind.strip().lower(), DEFAULT_LEVEL)


def level_for(record: dict[str, Any]) -> str:
    """算一条记录的信任级别。

    优先级：
    1. 记录里**显式**写的 `trust_level`（已归一，合法才认）；
    2. 否则按 `evidence_kind` 折算；
    3. 都没有 → `DEFAULT_LEVEL`。

    显式 `trust_level` 优先是为了让"人工复核后升级"成为可能——
    人在界面上确认过，就是比模型自述更硬。
    """
    explicit = record.get("trust_level")
    if isinstance(explicit, str) and explicit.strip().upper() in TRUST_RANK:
        return explicit.strip().upper()
    return level_for_evidence_kind(record.get("evidence_kind"))


def verification_for_level(level: str) -> str:
    """信任级别 → `findings` 域认识的 `verification` 取值。

    `app/findings/strix_vulns.py` 只认两个词：`verified` / `partial`
    （见那边的 `_normalize_verification`）。映射：

    - `L2` → `verified`（独立验证过，可以直接给人看）
    - `L1` / `L0` → `partial`（进库，但界面要标"待复核"）
    """
    return "verified" if level == "L2" else "partial"


def should_enter_findings(level: str) -> bool:
    """这条记录该不该进 `findings` 表（=会不会被当成"漏洞"统计）。

    **L0 不进**。理由：L0 是纯推断，进 `findings` 就污染了漏洞统计，
    而漏洞统计是本项目对外最常引用的数字。让 L0 只留在
    `observations` 里，既不失真也不丢信息。
    """
    return TRUST_RANK.get(level, 0) >= TRUST_RANK["L1"]


def normalize_observation(
    item: object,
    *,
    default_target: str = "",
    reported_by: str = "",
) -> dict[str, Any] | None:
    """把模型给的一条东西归一成「观察记录」。

    接受三种输入：
    - dict：原样取字段（title/summary/evidence/...）；
    - str：包成 `{"title": s}`；
    - 其他：返回 None（没有可用语义，包进去只是噪音）。

    返回的 dict **必定**含这些键，缺的给默认值 —— 下游（写文件、
    入库、界面）不用再判 None：

        title, summary, evidence, target, cwe, severity,
        evidence_kind, trust_level, polarity, reported_by
    """
    if isinstance(item, str):
        text = item.strip()
        if not text:
            return None
        data: dict[str, Any] = {"title": text[:200]}
    elif isinstance(item, dict):
        data = item
    else:
        return None

    title = str(data.get("title") or data.get("name") or "").strip()
    if not title:
        # 没标题的观察没有归一价值（也无从去重），丢掉比造空壳诚实。
        return None

    level = level_for(data)
    polarity = str(data.get("polarity") or "positive").strip().lower()
    if polarity not in ("positive", "negative"):
        polarity = "positive"

    return {
        "title": title[:200],
        "summary": str(data.get("summary") or data.get("description") or "").strip(),
        "evidence": str(data.get("evidence") or "").strip(),
        "target": str(data.get("target") or default_target or "").strip(),
        "cwe": str(data.get("cwe") or "").strip(),
        "severity": str(data.get("severity") or "").strip(),
        "evidence_kind": str(data.get("evidence_kind") or "").strip(),
        "trust_level": level,
        "polarity": polarity,
        "reported_by": str(data.get("reported_by") or reported_by).strip(),
    }


def make_negative_observation(
    text: object,
    *,
    default_target: str = "",
    reported_by: str = "",
) -> dict[str, Any] | None:
    """把 `FINAL.ruled_out` 里的一条自由文本变成「已排除」观察。

    阴性结果**一律 L2**：模型之所以能说"这条路走不通"，是因为它
    实际跑过并在文档里给了反例（`_final_stretch_note` 明确要求
    "附反例，不是感觉不行"）。这里不再区分它给的是哪种 `evidence_kind`
    —— 给阴性结论降级会让"排除了什么"变得不可引用。

    ⚠️ 但它 `should_enter_findings()` 恒为 False（`polarity="negative"`），
    所以永远不会被算成漏洞。
    """
    if not isinstance(text, str):
        return None
    body = text.strip()
    if not body:
        return None
    return {
        "title": body[:120],
        "summary": body,
        "evidence": "",
        "target": default_target,
        # `ruled_out` 的文本常常形如「X（CWE-307）：反例是…」，
        # 但用正则去挖 CWE 会在模型没写时造出假字段，所以留空。
        "cwe": "",
        "severity": "",
        "evidence_kind": "before_after",
        "trust_level": "L2",
        "polarity": "negative",
        "reported_by": reported_by,
    }


def build_observations(
    *,
    findings: object = None,
    ruled_out: object = None,
    default_target: str = "",
    reported_by: str = "",
) -> list[dict[str, Any]]:
    """把一次运行的 `findings` + `ruled_out` 合成观察列表（去重）。

    去重键 = `(polarity, title.lower())`。同一结论既出现在 findings
    又在 ruled_out 里时**都保留**——那是有信息量的冲突（一个 agent
    说成立、另一个说排除），丢掉任何一边都是在掩盖分歧。冲突由
    下游按 `trust_level` 排序决定采信，而不是在这里悄悄合并。
    """
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def _add(rec: dict[str, Any] | None) -> None:
        if rec is None:
            return
        key = (rec["polarity"], rec["title"].lower())
        if key in seen:
            return
        seen.add(key)
        out.append(rec)

    if isinstance(findings, list):
        for item in findings:
            _add(normalize_observation(item, default_target=default_target, reported_by=reported_by))

    if isinstance(ruled_out, list):
        for item in ruled_out:
            _add(make_negative_observation(item, default_target=default_target, reported_by=reported_by))

    return out


def summarize_levels(observations: list[dict[str, Any]]) -> dict[str, int]:
    """统计各级别条数，给报告/界面直接用。"""
    counts: dict[str, int] = {"L0": 0, "L1": 0, "L2": 0}
    for rec in observations:
        level = level_for(rec)
        counts[level] = counts.get(level, 0) + 1
    return counts
