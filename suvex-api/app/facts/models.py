"""`scan_facts` 领域：扫描过程中的**项目黑板**（v0.79）。

## 这个表存在的理由

v0.77 验证扫描实测：`run_shell` 238 次，`record_coverage` 只有 **2 次**，
`add_idea` / `add_fact` 各 **0 次**。工具都在、提示词写了、测试全绿，模型不用。

v0.79 实验 A 把「当前状态」强制推进 root 的 system prompt（`render_live_state`），
解决了"看不见"。但那是**内存态**的：进程一重启、扫描一结束，黑板就没了。
更关键的是 —— 记忆只在**一次扫描**内有效，跨扫描什么也留不下。

这张表学的是 CyberStrikeAI 的 `internal/project/`（`project_facts` +
`project_fact_edges`）：把「已确认事实 / 已排除方向 / 约束」落库，
下一次对同一目标扫描时能继承上次的结论。

## ⚠️ 与 `findings` 是两张表，不是一张

`findings` 是**对外交付的漏洞条目**：有严重度、有状态流转、有 SLA、要给人看。
`scan_facts` 是**过程中的认知**：包括"这个点我试过但走不通"（`negative`），
包括"这个接口要求 Bearer 而不是 Cookie"（`constraint`）。

把 negative 塞进 findings 会让漏洞统计被"我没找到"污染 —— 这正是 v0.75
`should_enter_findings` 把 L0 挡在外面要防的事。反过来，把 findings 塞进
scan_facts 又会让黑板被已交付内容撑爆。**两者服务不同读者**，所以两张表。

## 与 v0.75 分级（L0/L1/L2）的关系

`confidence` 三态（confirmed / tentative / deprecated）与 `TrustLevel` 是
**同方向的洞察，不同维度**，所以都留：

* `confidence` —— 这条结论**我们有多确信**（confirmed=有对照实验或实际验证过）
* `trust_level` —— 这条结论**凭什么算数**（L2 有对照 / L1 有输出无对照 / L0 纯推断）

一条事实可以是 `tentative` + `L1`（单次响应看到 401，但没做两身份对比）。
合成一个字段就装不下这个真实情况 —— 与 `findings` 里 `verification` 和
`status` 必须分两列是同一条理由。

## `fact_key` 的形状

`category/slug`，例如 `endpoint/put-auth-users-id`、`credential/admin-session`、
`ruled-out/settings-token-is-mock`。

**同一 key 覆盖**（不追加历史）：黑板要的是"现在的最新认知"，不是审计日志。
历史由 `updated_at` + `version` 两个字段体现"被改过几次"，具体改了什么
不进这张表 —— 那需要一张变更审计表，而它现在没有读者。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.columns import JsonColumn, UtcDateTime
from app.core.db import Base
from app.core.timeutil import now_utc

#: 三态信心。字符串而不是 Enum：与 `findings.severity` 等既有列保持同一风格
#: （`values()` 生成 CheckConstraint，见下）。
FACT_CONFIDENCES = ("confirmed", "tentative", "deprecated")

#: 一级分类。**故意留得很少** —— 分类一多，模型就会花回合纠结该填哪个，
#: 而黑板的唯一目的是让它下次能查到。
FACT_CATEGORIES = (
    "endpoint",  # 接口/路由的形态与鉴权要求
    "credential",  # 凭据、会话、token 的可用性
    "surface",  # 攻击面（可写字段、可访问的内部服务）
    "ruled-out",  # 试过但走不通（negative，防重复劳动最值钱）
    "constraint",  # 约束（只读、限流、超时、必须先登录）
    "note",  # 兜底
)


class ScanFact(Base):
    """扫描过程中的一条认知。

    ⚠️ **不存原始工具输出**。证据进 `evidence_excerpt`（≤1000 字符），
    需要全文去 `scan` 的 run_dir 或 `requests` 表查。理由：
    238 次 run_shell 的完整输出是 MB 级，存进黑板会让"读黑板"变成"重读全场"，
    而黑板的全部价值是**比读历史便宜**。
    """

    __tablename__ = "scan_facts"
    __table_args__ = (
        CheckConstraint(
            f"confidence IN ({', '.join(repr(c) for c in FACT_CONFIDENCES)})",
            name="scan_fact_confidence_valid",
        ),
        CheckConstraint(
            f"category IN ({', '.join(repr(c) for c in FACT_CATEGORIES)})",
            name="scan_fact_category_valid",
        ),
        # 同一扫描内 fact_key 唯一 —— 覆盖语义靠 UPSERT，靠约束保证不出现两条。
        UniqueConstraint("scan_id", "fact_key", name="uq_scan_facts_scan_key"),
        Index("ix_scan_facts_scan_id", "scan_id"),
        # 跨扫描继承要按目标查（"上次扫 172.17.0.1:8080 时发现了什么"）。
        Index("ix_scan_facts_target", "target"),
        Index("ix_scan_facts_category", "category"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scan_id: Mapped[str] = mapped_column(
        String(64),
        # ⚠️ 不级联删除：扫描记录被删时，黑板里的认知仍有参考价值
        # （特别是 ruled-out —— "这个点试过不行"是最该跨扫描保留的）。
        # NULL 是合法状态，表示"这次扫描的记录已不存在，但事实还在"。
        ForeignKey("scans.id", ondelete="SET NULL"),
        nullable=True,
    )

    #: `category/slug`。见模块 docstring。
    fact_key: Mapped[str] = mapped_column(String(200), nullable=False)

    #: 一级分类，冗余自 `fact_key` 的前缀 —— 冗余是有意的：
    #: 按分类统计（"这次排除了多少个方向"）不该去字符串切分。
    category: Mapped[str] = mapped_column(String(32), nullable=False)

    #: 一句话结论。**≤300 字符**，超了说明它该拆成两条。
    summary: Mapped[str] = mapped_column(String(300), nullable=False)

    #: 细节（可空）。`NULL` = 没有更多细节（与空字符串不是一回事）。
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: 证据摘录（可空，≤1000 字符）。为什么不是必填：
    #: `constraint` 类事实常常来自文档或前端 bundle，没有"响应"可摘。
    evidence_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: 见模块 docstring：`confidence` 与 `trust_level` 是不同维度，都留。
    confidence: Mapped[str] = mapped_column(
        String(16), nullable=False, default="tentative"
    )
    trust_level: Mapped[str | None] = mapped_column(String(4), nullable=True)

    #: 关系边（学 `project_fact_edges`，但先用 JSON 列而不是独立表）。
    #:
    #: ## 为什么先不开表
    #:
    #: 独立边表要的是"能按关系类型反向查"（"哪些事实 enables 了这条 RCE"）。
    #: 我们现在的读者只有一个 —— 渲染索引块时把边上的 key 列出来。
    #: 为它开表要让模型多一次 UPSERT、多一套外键校验，而收益要等到
    #: "攻击链可视化"真的做出来才兑现。JSON 列足够，改成表也是一次迁移的事。
    #:
    #: 形状：`[{"type": "enables", "fact_key": "surface/x"}]`。
    edges: Mapped[list[dict[str, str]]] = mapped_column(
        JsonColumn, nullable=False, default=list
    )

    #: 谁写进来的（agent id，或 `human`）。**分歧本身有价值**：
    #: 同一个 key 被两个 agent 写过不同结论，`updated_at` 与 `version` 能看到。
    recorded_by: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    #: 目标（冗余，跨扫描继承按它查）。与 `scans.target` 同形。
    target: Mapped[str] = mapped_column(String(512), nullable=False, default="")

    #: 被覆盖过几次。1 = 从没改过。
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=now_utc, onupdate=now_utc
    )

    def __repr__(self) -> str:  # pragma: no cover - 排错用
        return f"<ScanFact {self.fact_key!r} {self.confidence} target={self.target!r}>"


#: 让迁移与模型共享同一份分类清单 —— 两边各写一遍必然漂移。
__all__ = ["FACT_CATEGORIES", "FACT_CONFIDENCES", "ScanFact"]
