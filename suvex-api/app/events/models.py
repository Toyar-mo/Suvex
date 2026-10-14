"""实时事件流领域。

> 任务的模块清单里这一块叫 `agents_ws`（WebSocket）。**按契约实现为 SSE**，
> 所以包名是 `events`。理由见契约 §0.1 的三条：事件流单向、`Last-Event-ID`
> 补发是内建的、前端 `EventSource` 侧代码已经是这个形状。
> `agents_ws` 这个名字留着会误导后来者去找 WebSocket 握手。
>
> 决策与偏离记录：`docs/adr-002-realtime-sse.md`。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.columns import JsonColumn, UtcDateTime
from app.core.db import Base
from app.core.timeutil import now_utc
from app.core.vocab import ActivityEventType, AgentPhase, AgentRole, AgentStatus, values


class Agent(Base):
    """扫描里的一名 agent。

    ## `id` 是全局唯一字符串，不是扫描内的 `agent-03`

    契约 §2.4 的举例是 `agent-03`，但那是**演示数据的风格举例**；
    契约的**规则**（§1.1）是「带前缀的字符串」。这里取规则不取举例，因为
    全局唯一是硬要求：契约 §4.1 的全局流 `/api/v1/events` 会同时推送
    多个扫描的 agent，而前端 `useActivityStream` 把 agent 收进一个按 id
    索引的 map。用扫描内序号会让两个扫描的 `agent-01` 互相覆盖 ——
    名册上少一个人，而失败是静默的。

    展示层要的「03」由前端按名册顺序渲染，它不需要成为主键。
    """

    __tablename__ = "agents"
    __table_args__ = (
        CheckConstraint(
            f"role IN ({', '.join(repr(v) for v in values(AgentRole))})", name="role_valid"
        ),
        CheckConstraint(
            f"status IN ({', '.join(repr(v) for v in values(AgentStatus))})", name="status_valid"
        ),
        Index("ix_agents_scan_id", "scan_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scan_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("scans.id", ondelete="CASCADE"), nullable=False
    )
    # agent **长期承担的角色**（名册上显示「信息收集」）。
    # 与 AgentPhase 是两套值 —— 一个 exploiter 角色的 agent 完全可能
    # 产生一个 scan 阶段的事件（契约 §2.4）。
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=AgentStatus.IDLE.value)
    target: Mapped[str] = mapped_column(String(512), nullable=False, default="")

    # 契约 §2.4：前端目前从事件流派生这两个字段，所以这里的值是
    # **初始值 / 兜底值**，前端会按事件流更新。这不是重复 ——
    # 它让「agent 失败了但没有后续事件」这种情况能被表达。
    event_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_active_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=now_utc)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Agent id={self.id!r} role={self.role!r} status={self.status!r}>"


class ActivityEvent(Base):
    """一条完整事件（8 种类型之一，带 `detail`）。

    后端**只发完整事件**，Overview 的 6 种投影由前端派生（契约 §2.5）。
    理由：投影是为「一行摘要」渲染优化的展示层视图模型，
    让后端产出它等于把展示逻辑搬到后端 —— UI 一改摘要规则就要动后端。

    ## 为什么有 `seq` 而 `id` 是 ULID

    两个标识符各司其职，不是冗余：

    * `id`（`evt_<ULID>`）是**对外**的标识，进 SSE 的 `id:` 帧字段，
      也是 `Last-Event-ID` 的内容；
    * `seq`（单调自增）是**对内**的游标。补发要先回答「`Last-Event-ID`
      指向的那条排第几」，再取它之后的 —— 用 ULID 做这件事要在字符串上
      比较，而字符串序在**同一毫秒内**是不确定的（ULID 的随机段无序）。
      自增列把这件事变成一次 `WHERE seq > ?`，走主键索引。

    ## `at` 是**事件发生时刻**，不是推送时刻

    契约 §4.4 专门警告过：不要用「发送时刻」当 `at`。补发时尤其明显 ——
    3 分钟前的事件补发出去，`at` 仍应是 3 分钟前。
    所以这一列由产生事件的一方写入并落库，推送路径只读不改。
    """

    __tablename__ = "activity_events"
    __table_args__ = (
        CheckConstraint(
            f"type IN ({', '.join(repr(v) for v in values(ActivityEventType))})", name="type_valid"
        ),
        CheckConstraint(
            f"agent_phase IN ({', '.join(repr(v) for v in values(AgentPhase))})",
            name="agent_phase_valid",
        ),
        # 补发的查询形状是「某个扫描里 seq 大于 X 的前 N 条」，这条索引正好覆盖。
        Index("ix_activity_events_scan_seq", "scan_id", "seq"),
        Index("ix_activity_events_at", "at"),
        # 幂等键：同一次扫描里，同一个外部行号只能有一条事件。
        #
        # ⚠️ 部分唯一索引（只对非空生效）：我们自己的生命周期事件
        # `source_row_id` 是 NULL，而 SQL 里 NULL 彼此不相等 ——
        # 不加 `WHERE` 本来也不冲突，但显式写出来更清楚，
        # 而且省下索引里那一大片 NULL。
        Index(
            "uq_activity_events_scan_source",
            "scan_id",
            "source_row_id",
            unique=True,
            postgresql_where=text("source_row_id IS NOT NULL"),
            sqlite_where=text("source_row_id IS NOT NULL"),
        ),
    )

    # 主键自增。PostgreSQL 上是 BIGSERIAL。
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)

    scan_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("scans.id", ondelete="CASCADE"), nullable=False
    )
    # ⚠️ **非空**，且这不是随手定的：前端 `data/activity.ts` 的
    # `ActivityEvent.agentId` 声明就是 `string`（不是 `string | null`），
    # 而契约 §2.5 的字段表里它也没有可空标记。
    #
    # 一开始这里写的是可空（理由：「扫描级的 completed 事件不属于任何 agent」），
    # 但那个理由是错的 —— 扫描级事件由 `reporter` 角色的 agent 产生，
    # 它仍然有 agent。允许可空会造出前端渲染不了的响应，
    # 而失败会出现在前端（`undefined.xxx`），排查方向完全跑偏。
    #
    # `ondelete=CASCADE`：agent 与事件同属一次扫描，一起生一起灭。
    # 用 SET NULL 会违反上面那条非空约束 —— 数据库会在删 agent 时报错，
    # 而那个报错发生在"删除扫描"这种和事件无关的操作里。
    agent_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )

    # ⚠️ **完整事件里也要带 `agentPhase`**，这是契约 §2.5 相对前端现状的
    # 唯一一处新增字段：它在 Overview 投影里有、完整事件里没有，
    # 而 `ActivityFeedItem` 渲染阶段标签（「利用中」）时必须它。
    # 前端两个形状各缺对方有的东西，后端必须带全，前端才好投影。
    agent_phase: Mapped[str] = mapped_column(String(16), nullable=False)

    type: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)

    # 8 种判别式载荷之一。**整体可为 null**（纯状态事件，契约 §2.6）。
    # 存 JSONB 而不是摊平：载荷有 8 种互不相同的形状，
    # 摊平会得到一张列数爆炸且每行大部分是 NULL 的表。
    detail: Mapped[dict | None] = mapped_column(JsonColumn, nullable=True)

    # 事件发生时刻（tz-aware UTC）。
    at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=now_utc)

    # ── 溯源：这条事件是从外部工具的哪一行导进来的（任务 4）────────
    #
    # 外部工具的会话库里每行消息有自己的自增 id。用它做**幂等键**：
    # 导入器会反复读同一个库（扫描还在写），没有这个键就会把同一批
    # 事件重复写进去 —— 而活动流里会看到每条都出现两次。
    #
    # 可空：我们自己发的事件（扫描生命周期：启动/完成/失败/终止）
    # 没有外部行号。
    #
    # ⚠️ 它同时是**导入游标**：导入器用
    # `max(source_row_id) WHERE scan_id = ?` 决定从哪继续读，
    # 所以不需要再加一列 cursor —— 那会是同一件事的第二个来源。
    source_row_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<ActivityEvent seq={self.seq} id={self.id!r} type={self.type!r}>"
