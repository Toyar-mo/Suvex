"""黑板的校验与渲染（v0.79）。

分两半：

* **纯函数**（`normalize_fact_key` / `validate_fact` / `render_fact_index`）——
  不碰 DB，可以脱离数据库单测，也是「模型填错了会怎样」的唯一判据所在；
* **仓储**（`upsert_fact` / `load_facts` / `load_inherited_facts`）——
  包一层 SQLAlchemy，把「同 key 覆盖」「version 自增」这两个语义钉在 SQL 里
  而不是让调用方各写一遍。

## 为什么校验要严格

这个工具是模型调的。模型会给：

* `fact_key = "PUT /auth/users/{id} 可以改 role"`（整句塞进 key）
* `confidence = "high"`（不在三态里 —— 它从 findings 那边学来的词）
* `category = "vulnerability"`（不在六类里）
* `summary` 写 800 字（该进 `detail`）

每一条**都必须返回可读的错误**，而不是静默纠正：静默纠正会让模型以为
它填对了，下次还这么填。错误文案要直接给出正确形状。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.facts.models import FACT_CATEGORIES, FACT_CONFIDENCES, ScanFact

#: `fact_key` 的合法形状：`category/slug`。slug 只允许小写字母、数字、`-`。
#:
#: 为什么强制小写：同一个点在两轮里被写成 `endpoint/PUT-Users` 和
#: `endpoint/put-users`，UPSERT 就认不出是同一条，黑板会出现两份互相矛盾的记录。
#: 归一化（`normalize_fact_key`）在再输入进校验之前把大写转小写。
_FACT_KEY_RE = re.compile(r"^[a-z0-9-]+/[a-z0-9-]+$")

#: 索引块里 summary 的中文截断长度。索引块的唯一职责是"让模型知道有这条"，
#: 细节靠 `get_scan_fact` 取 —— 见 `render_fact_index` 的页脚。
INDEX_SUMMARY_CHARS = 80

#: 索引块总字符上限。与 `multiagent.LIVE_STATE_MAX_CHARS` 同量级：
#: 它也是每轮进 system prompt 的东西，不该无限长。
INDEX_MAX_CHARS = 1800

#: 页脚：学 CyberStrikeAI 的 `factIndexFooterGetDetail`。
#:
#: ⚠️ 这段不是装饰。没有它，模型会拿 summary 当全文，然后**凭摘要臆造细节** ——
#: 那比没有黑板更糟：它会把 80 字的摘要扩写成一段看起来很具体的假证据。
INDEX_FOOTER = (
    "需要完整内容（攻击链、POC、请求响应等）时必须调用 get_scan_fact(fact_key)，"
    "禁止凭摘要臆造细节。"
)


def normalize_fact_key(raw: str) -> str:
    """把模型给的 key 归一成合法形状；归不成就抛 ValueError。

    归一化只做**无损**变换：
      * 去首尾空白、转小写
      * 把空格/下划线/连续横线折成单个 `-`
      * `category` 部分只保留第一个 `/` 前的片段（去掉多余斜杠）

    ⚠️ 不做"猜 category"（比如把 `vuln` 猜成 `endpoint`）——猜错了会产生
    一条看似合法、实则分类错误的记录，比报错难查得多。
    """
    s = (raw or "").strip().lower()
    if not s:
        raise ValueError("fact_key 不能为空，形状是 `category/slug`，例如 `endpoint/put-auth-users-id`")
    if "/" not in s:
        raise ValueError(
            f"fact_key 必须带分类前缀，形状是 `category/slug`，"
            f"例如 `endpoint/put-auth-users-id`；你给的是 {raw!r}"
        )
    category, _, slug = s.partition("/")
    def _squeeze(text: str) -> str:
        return re.sub(r"[\s_-]+", "-", text).strip("-")

    category = _squeeze(category)
    slug = _squeeze(slug)
    key = f"{category}/{slug}"
    if not _FACT_KEY_RE.match(key):
        raise ValueError(
            f"fact_key {raw!r} 归一后是 {key!r}，仍不合法：只能用**小写字母、数字、- 和斜杠**，"
            f"形状是 `category/slug`"
        )
    return key


def validate_fact(
    *,
    fact_key: str,
    summary: str,
    category: str | None = None,
    confidence: str = "tentative",
    trust_level: str | None = None,
    edges: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """校验一条待写入的事实，返回归一后的字段 dict；不合规抛 ValueError。

    错误文案统一带上「正确形状」—— 因为唯一读者是模型，它得知道怎么改。
    """
    key = normalize_fact_key(fact_key)
    derived_category = key.split("/", 1)[0]

    # category 参数可省略（默认取自 key 前缀）；给了就必须与前缀一致，
    # 否则同一 key 在两轮里落进不同分类，按分类统计就失真。
    cat = (category or derived_category).strip().lower()
    if cat != derived_category:
        raise ValueError(
            f"category={cat!r} 与 fact_key 前缀 {derived_category!r} 不一致。"
            f"要么不传 category（自动取前缀），要么让两者相同"
        )
    if cat not in FACT_CATEGORIES:
        raise ValueError(
            f"未知 category {cat!r}。可用：{', '.join(FACT_CATEGORIES)}"
        )

    text = (summary or "").strip()
    if not text:
        raise ValueError("summary 不能为空 —— 写一句话结论")
    if len(text) > 300:
        raise ValueError(
            f"summary 有 {len(text)} 字符，超过 300。把细节放进 detail 参数，"
            f"summary 只写一句话"
        )

    conf = (confidence or "tentative").strip().lower()
    if conf not in FACT_CONFIDENCES:
        raise ValueError(
            f"未知 confidence {conf!r}。可用："
            f"{', '.join(FACT_CONFIDENCES)}"
            "（confirmed=有对照实验或实际验证过；tentative=单次观察，待验证；"
            "deprecated=曾经成立但已被推翻）"
        )

    lvl = (trust_level or "").strip().upper() or None
    if lvl is not None and lvl not in ("L0", "L1", "L2"):
        raise ValueError(f"未知 trust_level {trust_level!r}。可用：L0 / L1 / L2，或省略")

    clean_edges: list[dict[str, str]] = []
    for e in edges or []:
        if not isinstance(e, dict):
            raise ValueError("edges 每项必须是 {type, fact_key} 形状的对象")
        etype = str(e.get("type") or "").strip().lower()
        ekey_raw = str(e.get("fact_key") or "").strip()
        if not etype or not ekey_raw:
            raise ValueError("edges 每项必须同时有 type 和 fact_key")
        clean_edges.append({"type": etype, "fact_key": normalize_fact_key(ekey_raw)})

    return {
        "fact_key": key,
        "category": cat,
        "summary": text,
        "confidence": conf,
        "trust_level": lvl,
        "edges": clean_edges,
    }


def render_fact_index(
    facts: Iterable[ScanFact],
    *,
    max_chars: int = INDEX_MAX_CHARS,
) -> str:
    """把事实列表渲染成**进 system prompt 的索引块**（只含 key + 摘要 + 边）。

    ## 排序：negative 在前

    与 `multiagent.render_live_state` 同一条理由 —— 已排除方向是防止
    重复劳动最值钱的东西。已确认的模型多半记得，已排除的它一定会重试。

    ## 超限怎么办

    从**末尾**截（前面的已排除项最值钱），并显式写「还有 N 条」。
    不静默丢：模型会以为清单就这些，然后重复劳动。
    """
    items = list(facts)
    if not items:
        return ""

    def _order(f: ScanFact) -> tuple[int, str]:
        # 0 = negative 优先；同档按 key 保证顺序稳定（否则每轮渲染顺序会抖，
        # 提示词缓存命中率下降 —— 这一点在长扫描里是真金白银）。
        return (0 if f.category == "ruled-out" else 1, f.fact_key)

    ordered = sorted(items, key=_order)
    lines: list[str] = []
    used = 0
    dropped = 0
    for f in ordered:
        summary = (f.summary or "").strip()
        if len(summary) > INDEX_SUMMARY_CHARS:
            summary = summary[:INDEX_SUMMARY_CHARS] + "…"
        tail = ""
        if f.edges:
            tail = "｜" + ", ".join(f"{e.get('type')}→{e.get('fact_key')}" for e in f.edges[:3])
        line = f"- [{f.confidence}] {f.fact_key}：{summary}{tail}"
        cost = len(line) + 1
        if used + cost > max_chars:
            dropped = len(ordered) - len(lines)
            break
        lines.append(line)
        used += cost
    body = "\n".join(lines)
    if dropped:
        body += f"\n…（还有 {dropped} 条未列出，用 get_scan_fact 按需查）"
    return body + "\n\n" + INDEX_FOOTER


# ══════════════════════════════════════════════════════════
# 仓储
# ══════════════════════════════════════════════════════════


def upsert_fact(
    db: Session,
    *,
    scan_id: str | None,
    target: str,
    recorded_by: str,
    fields: dict[str, Any],
    detail: str | None = None,
    evidence_excerpt: str | None = None,
    now: Any = None,
) -> tuple[ScanFact, bool]:
    """写一条事实。返回 `(对象, 是否新建)`。

    ## 为什么是 UPSERT 而不是 INSERT

    同一个 `fact_key` 在长扫描里会被反复修正（先是 `tentative`，验证后变
    `confirmed`）。若每次 INSERT，黑板会有五条 `endpoint/x` 而模型不知道信哪条；
    若让模型自己先查再决定插还是改，它会写错（而且多一次工具调用）。

    所以：**同 key 覆盖**，`version += 1`，`created_at` 保留首次写入时间。
    """
    from app.core.timeutil import now_utc

    at = now or now_utc()
    key = fields["fact_key"]
    # ⚠️ target 在**写入口**归一（见 `normalize_target` docstring）：
    # `load_inherited_facts` 只按归一形比字符串，两边都归一才会让
    # 「本次扫描写进去的」和「下次扫描要继承的」落在同一个键上。
    target_key = normalize_target(target)
    existing = db.execute(
        select(ScanFact).where(ScanFact.scan_id == scan_id, ScanFact.fact_key == key)
    ).scalar_one_or_none()

    if existing is None:
        fact = ScanFact(
            id=_new_id(),
            scan_id=scan_id,
            fact_key=key,
            category=fields["category"],
            summary=fields["summary"],
            detail=detail,
            evidence_excerpt=(evidence_excerpt or None),
            confidence=fields["confidence"],
            trust_level=fields["trust_level"],
            edges=fields["edges"],
            recorded_by=recorded_by,
            target=target_key,
            version=1,
            created_at=at,
            updated_at=at,
        )
        db.add(fact)
        db.flush()
        return fact, True

    existing.category = fields["category"]
    existing.summary = fields["summary"]
    if detail is not None:
        existing.detail = detail
    if evidence_excerpt is not None:
        existing.evidence_excerpt = evidence_excerpt
    existing.confidence = fields["confidence"]
    existing.trust_level = fields["trust_level"]
    existing.edges = fields["edges"]
    existing.recorded_by = recorded_by
    existing.target = target_key or existing.target
    existing.version += 1
    existing.updated_at = at
    db.flush()
    return existing, False


def load_facts(db: Session, scan_id: str) -> list[ScanFact]:
    """读本次扫描的全部事实。"""
    return list(
        db.execute(
            select(ScanFact).where(ScanFact.scan_id == scan_id).order_by(ScanFact.fact_key)
        ).scalars()
    )


def normalize_target(raw: str | None) -> str:
    """把目标归一成一个**可比较的继承键**。

    ## 为什么不能直接拿 `spec.target` 比字符串

    同一个目标在两次扫描里几乎从来不是同一个字符串：
    `http://172.17.0.1:8080`、`172.17.0.1:8080`、`HTTP://172.17.0.1:8080/`
    是同一台靶机的三种写法。而在**第一次**接上继承功能时，库里一条数据
    都没有 —— 如果这一步写错，表现不是报错，是"继承永远 0 条"，
    看起来像功能没生效，实际是键没对上。这正是这个函数存在的唯一理由。

    ## 规则（顺序不能换）

    1. 去掉首尾空白、整体转小写（scheme / host 都大小写不敏感）
    2. 去 scheme（`http://` / `https://`）
    3. 去尾部路径 `/`（但保留 `:8080` 这种端口）
    4. 去 URL 里的用户信息（`user:pass@host` —— 口令会变，不是目标的一部分）

    ⚠️ **刻意不做的事**：
    - 不去端口：`172.17.0.1:8080` 与 `172.17.0.1:8443` 是两个服务面，
      继承会让人跳过该测的那个端口 —— 这与"endpoint 类不继承"同一条理由。
    - 不解析 IPv6 / 不做 DNS：扫描目标是用户填的字符串，解析失败时
      "少继承"是可接受的失败方向，"错继承"不是。
    - 空目标返回空串 —— 调用方据此跳过继承（没有目标就没有"同一个目标"）。
    """
    key = (raw or "").strip().lower()
    if not key:
        return ""
    for scheme in ("https://", "http://"):
        if key.startswith(scheme):
            key = key[len(scheme) :]
            break
    # 只切掉**最后**一层用户信息：`user:pass@host:8080` → `host:8080`
    if "@" in key.rsplit(":", 1)[0]:
        key = key[1 + key.index("@"):]
    while key.endswith("/"):
        key = key[:-1]
    return key


def load_inherited_facts(
    db: Session, *, target: str, exclude_scan_id: str | None = None, limit: int = 40
) -> list[ScanFact]:
    """读**同一个目标**在其它扫描里留下的事实，供本次继承。

    ## 为什么只继承 `ruled-out` 与 `constraint`

    另外三类（endpoint / credential / surface）是**会变的**：上次 `/auth/login`
    要求 Bearer，这次可能改成 Cookie；上次能改 role，这次被修了补丁。
    把旧的 endpoint 结论当事实继承，会让这次扫描跳过真正该测的东西 ——
    这比"什么都不知道"更糟。

    而 `ruled-out`（试过不行）与 `constraint`（结构性限制，如只读挂载）
    恰恰相反：它们失效得慢，重复验证它们的成本却最高。

    ## ⚠️ 两边都要归一化

    写入口 `upsert_fact` 把 `target` 存成归一形，但**调用方传进来的**
    （`solve_spec.target` = 用户在界面上填的那串）几乎从来不是归一形：
    `http://172.17.0.1:8080` vs `172.17.0.1:8080` vs `HTTP://…/`。
    如果这里按原样比字符串，继承会**永远收 0 条且不报错** —— 这一条是
    实测抓到的（第一版只在写侧归一化，`seed_inherited` 一条都抄不到）。
    """
    key = normalize_target(target)
    if not key:
        # 空目标不能当通配符：那会让"没填目标"的扫描继承全场所有事实。
        return []
    stmt = (
        select(ScanFact)
        .where(
            ScanFact.target == key,
            ScanFact.category.in_(("ruled-out", "constraint")),
        )
        .order_by(ScanFact.updated_at.desc())
        .limit(limit)
    )
    if exclude_scan_id:
        stmt = stmt.where((ScanFact.scan_id.is_(None)) | (ScanFact.scan_id != exclude_scan_id))
    return list(db.execute(stmt).scalars())


def _new_id() -> str:
    """与项目其它实体同形的时间有序 id。"""
    from app.core.ids import new_id

    return new_id("fact")
