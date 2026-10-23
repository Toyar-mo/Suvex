"""DB 存取器：把 `app/facts/service` 的仓储包成 kernel 能用的对象（v0.79）。

## 为什么需要这一层

`kernel` 不该 import SQLAlchemy —— 它是一层**纯编排逻辑**，要能被纯逻辑测试
（没有数据库）跑起来。所以 `SolveSpec.fact_store` 收的是一个**鸭子类型**
对象，只要有两个方法：

* `record(...) -> dict`（写一条，返回 `{fact_key, created, version}`）
* `get(fact_key) -> dict | None`（取全文）

这个文件就是那个对象的生产实现。

## 事务边界

**每次工具调用自己 commit。** 理由：一次长扫描里模型可能写 30 条事实，
如果攒到扫描结束才 commit，进程被砍（实测有：我重启服务打断过一次扫描）
就全丢了。而一次 commit 的代价远小于丢 30 条认知。

⚠️ 但这意味着 `record` 里的 `flush` 之后必须 `commit`，且**不能让异常
在 commit 后继续传播** —— 那会让上层以为失败了、实际已经写进去。
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.core.timeutil import now_utc
from app.facts import service as facts_service
from app.facts.models import ScanFact

logger = logging.getLogger(__name__)


class DbFactStore:
    """把一次扫描的黑板操作绑到一条 DB 会话上。

    ## 为什么持有 session 而不是每次开新的

    同一个 scan 的 `record` / `get` 在时间上紧邻（模型写完立刻可能查），
    复用 session 能吃到 identity map。而跨扫描（继承）发生在扫描**开始前**，
    走的是 `seed_inherited()`，那是另一个时机、另一个 session。

    ⚠️ `session` 的生命周期必须**覆盖整场扫描**。若调用方在每轮之间
    close 它，下次 `record` 会拿到 `PendingRollbackError` —— 所以这里
    在每次调用前检查 `is_active`，不活跃时明确报"黑板会话已关闭"，
    而不是让 SQLAlchemy 抛一个模型看不懂的内部错误。
    """

    def __init__(self, session: Session, *, scan_id: str | None, target: str) -> None:
        self.session = session
        self.scan_id = scan_id
        self.target = target

    # ── 写入 ────────────────────────────────────────────
    def record(
        self,
        *,
        fact_key: str,
        summary: str,
        category: str | None = None,
        confidence: str = "tentative",
        trust_level: str | None = None,
        detail: str | None = None,
        evidence_excerpt: str | None = None,
        edges: list[dict] | None = None,
        recorded_by: str = "",
    ) -> dict:
        """校验 → UPSERT → commit。校验失败抛 ValueError（由 kernel 转成 JSON 错误）。"""
        if not self.session.is_active:
            # 明确说清是什么坏了。模型看到这句会改用 add_fact 继续干活，
            # 而不是反复重试一个永远失败的调用。
            raise ValueError(
                "黑板会话已关闭（这通常意味着扫描已被终止或重启）。"
                "改用 `add_fact` 记内存态事实，不要重试 record_fact。"
            )
        fields = facts_service.validate_fact(
            fact_key=fact_key,
            summary=summary,
            category=category,
            confidence=confidence,
            trust_level=trust_level,
            edges=edges,
        )
        fact, created = facts_service.upsert_fact(
            self.session,
            scan_id=self.scan_id,
            target=self.target,
            recorded_by=recorded_by,
            fields=fields,
            detail=detail,
            evidence_excerpt=evidence_excerpt,
            now=now_utc(),
        )
        self.session.commit()
        return {
            "fact_key": fact.fact_key,
            "created": created,
            "version": fact.version,
        }

    # ── 读取 ────────────────────────────────────────────
    def get(self, fact_key: str) -> dict | None:
        """按 key 取全文。归一化后查（模型可能给大写）。"""
        key = facts_service.normalize_fact_key(fact_key)
        if not self.session.is_active:
            return None
        fact = (
            self.session.query(ScanFact)
            .filter(ScanFact.scan_id == self.scan_id, ScanFact.fact_key == key)
            .one_or_none()
        )
        if fact is None:
            return None
        return _to_dict(fact)

    def index_block(self, *, max_chars: int | None = None) -> str:
        """渲染进 system prompt 的索引块。空黑板返回空串。"""
        if not self.session.is_active:
            return ""
        rows = facts_service.load_facts(self.session, scan_id=self.scan_id or "")
        if max_chars is None:
            return facts_service.render_fact_index(rows)
        return facts_service.render_fact_index(rows, max_chars=max_chars)


def _to_dict(fact: ScanFact) -> dict:
    """ORM 对象 → 纯 dict（kernel 拿到它就不该再碰 ORM）。"""
    return {
        "fact_key": fact.fact_key,
        "category": fact.category,
        "summary": fact.summary,
        "detail": fact.detail or "",
        "evidence": fact.evidence_excerpt or "",
        "confidence": fact.confidence,
        "trust_level": fact.trust_level or "",
        "edges": list(fact.edges or []),
        "recorded_by": fact.recorded_by,
        "version": fact.version,
        "updated_at": fact.updated_at.isoformat() if fact.updated_at else "",
    }


def seed_inherited(session: Session, *, scan_id: str, target: str, limit: int = 40) -> int:
    """把**同一目标**其它扫描留下的 `ruled-out` / `constraint` 抄进本次扫描。

    返回抄了几条。抄进来的记录 `recorded_by = "inherited"` —— 让模型
    （和复盘的人）知道这条不是这次现场验证出来的。

    ## 为什么只抄这两类

    `endpoint` / `credential` / `surface` 是**会变的**：上次 `/auth/login`
    要求 Bearer，这次可能改成 Cookie；上次能改 role，这次打了补丁。
    把旧结论当事实继承，会让这次扫描**跳过真正该测的东西** —— 这比
    "什么都不知道"更糟。

    而 `ruled-out`（试过不行）与 `constraint`（结构性限制）失效得慢，
    重复验证它们的成本却最高。这是 CyberStrikeAI 那句
    「阴性结果也是可审计观察」的落地方式。
    """
    rows = facts_service.load_inherited_facts(
        session, target=target, exclude_scan_id=scan_id, limit=limit
    )
    copied = 0
    for src in rows:
        exists = (
            session.query(ScanFact)
            .filter(ScanFact.scan_id == scan_id, ScanFact.fact_key == src.fact_key)
            .one_or_none()
        )
        if exists is not None:
            # 本次已经写过同一个 key：现场证据优先，不覆盖。
            continue
        session.add(
            ScanFact(
                id=facts_service._new_id(),  # noqa: SLF001 - 同包内部函数
                scan_id=scan_id,
                fact_key=src.fact_key,
                category=src.category,
                summary=src.summary,
                detail=src.detail,
                evidence_excerpt=src.evidence_excerpt,
                confidence=src.confidence,
                trust_level=src.trust_level,
                edges=list(src.edges or []),
                recorded_by="inherited",
                target=target,
                version=1,
                created_at=now_utc(),
                updated_at=now_utc(),
            )
        )
        copied += 1
    if copied:
        session.commit()
        logger.info("继承了 %d 条上次扫描的事实（target=%s）", copied, target)
    return copied
