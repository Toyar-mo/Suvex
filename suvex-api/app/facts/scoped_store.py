"""`DbFactStore` 的一次性会话变体（v0.79）。

## 为什么需要第二个实现

`app/facts/store.py::DbFactStore` 持有**一个长会话**，适合"有可控 db 会话"的
调用方。但扫描引擎跑在独立线程的独立事件循环里（见 `agent_runner._run_engine`
docstring：**引擎主循环不碰 DB**），跨轮复用一个长会话会把连接钉住一整次扫描
（可能几十分钟），把连接池耗干 —— 这正是 `_hint_provider` 注释里写过的坑。

所以这个变体改用 `session_scope()`：**每次工具调用开一个短会话、写完即关**。
代价是拿不到 identity map 复用（同一条 fact 在一次调用里查两次会走两趟 DB），
收益是连接池不可能被钉死。黑板每次调用只读/写一两条记录，这个代价可以忽略。

## 会话失效怎么办

`session_scope` 每次新开，所以不存在"会话已关闭"的状态 —— 但反过来，
如果 DB 本身连不上，`record` 会抛出来。接口约定与长会话版一致：
**校验错误抛 ValueError（kernel 会转成 JSON 错误），其余异常也由
kernel 的 `except Exception` 兜住**，不会让编排停摆。
"""

from __future__ import annotations

import logging

from app.core.db import session_scope
from app.facts import service as facts_service
from app.facts.models import ScanFact
from app.facts.store import _to_dict, seed_inherited

logger = logging.getLogger(__name__)


class ScopedFactStore:
    """每次操作开一个短会话的黑板存取器。

    接口与 `DbFactStore` 完全一致（`record` / `get` / `index_block`），
    这样 `SolveSpec.fact_store` 不需要知道用的是哪一版。
    """

    def __init__(self, *, scan_id: str, target: str) -> None:
        self.scan_id = scan_id
        self.target = target

    # ── 扫描开始前：继承 ────────────────────────────────
    def seed(self) -> int:
        """把同一个目标其它扫描的 `ruled-out` / `constraint` 抄进本次扫描。

        ## 为什么这一步必须在**扫描开始前**跑

        索引块是 `index_block()` 渲染的，而它只读 `scan_id == 本次`。
        继承若发生在扫描中途，前面若干轮的 system prompt 里就没有这些
        旧结论 —— 而"上次这个点试过不行"最该在**第一次**打这个点之前
        出现，之后才出现等于没防住重复劳动。

        ## 为什么失败不抛

        继承是**增强**：拿不到旧结论仍应照常扫。而它发生在扫描入口，
        抛出去会让"迁移没跑 / 库抖一下"变成"这次扫描起不来"——
        那正是 `record_fact` 在 kernel 里用「未启用」分支避开的同一种坏结果。
        所以这里吞异常、记 warning、返回 0。
        """
        if not facts_service.normalize_target(self.target):
            # 没有目标就没有"同一个目标"。让空 target 去查会变成
            # "匹配所有 target='' 的历史行"，是错继承。
            return 0
        try:
            with session_scope() as session:
                return seed_inherited(
                    session, scan_id=self.scan_id, target=self.target
                )
        except Exception:  # noqa: BLE001 - 见 docstring：继承是增强，不是前置条件
            logger.warning("继承上次扫描的事实失败（按无继承继续）", exc_info=True)
            return 0

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
        # ⚠️ 校验先做（不需要会话）—— 模型填错时不该先付一次 DB 连接的开销。
        fields = facts_service.validate_fact(
            fact_key=fact_key,
            summary=summary,
            category=category,
            confidence=confidence,
            trust_level=trust_level,
            edges=edges,
        )
        with session_scope() as session:
            fact, created = facts_service.upsert_fact(
                session,
                scan_id=self.scan_id,
                target=self.target,
                recorded_by=recorded_by,
                fields=fields,
                detail=detail,
                evidence_excerpt=evidence_excerpt,
            )
            # `session_scope` 退出时 commit；这里先取需要的标量值，
            # 免得退出后对象过期（detached instance）。
            result = {
                "fact_key": fact.fact_key,
                "created": created,
                "version": fact.version,
            }
        return result

    def get(self, fact_key: str) -> dict | None:
        key = facts_service.normalize_fact_key(fact_key)
        with session_scope() as session:
            fact = (
                session.query(ScanFact)
                .filter(ScanFact.scan_id == self.scan_id, ScanFact.fact_key == key)
                .one_or_none()
            )
            if fact is None:
                return None
            return _to_dict(fact)

    def index_block(self, *, max_chars: int | None = None) -> str:
        with session_scope() as session:
            rows = facts_service.load_facts(session, scan_id=self.scan_id)
            if max_chars is None:
                return facts_service.render_fact_index(rows)
            return facts_service.render_fact_index(rows, max_chars=max_chars)
