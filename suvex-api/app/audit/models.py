"""审计日志的表。

## 为什么单独一张表，而不是复用 `activity_events`

两者长得像（都有"谁 / 什么时候 / 发生了什么"），但它们的**生命周期与用途
完全不同**，合并会同时毁掉两边：

| | `activity_events` | `audit_log` |
| --- | --- | --- |
| 记什么 | 扫描**内部**发生了什么（工具调用、推理、发现） | **人对系统**做了什么（发起扫描、改状态、下载报告） |
| 谁产生 | strix 的 agent（自动） | 鉴权后的调用方（人 / CI 令牌） |
| 保留多久 | 跟着扫描走，可随扫描一起清理 | **必须长于应用日志**，用于事后追责 |
| 可改吗 | 可（重新导入会覆盖） | **只追加**，任何 UPDATE 都是 bug |
| 谁是主体 | `scan_id` 非空 | `scan_id` **可空**（下载报告不一定绑定某次扫描） |

合并的直接后果是：清理扫描历史时会把审计记录一起删掉 ——
而那正是出事之后要找的东西。

## 只追加（append-only）

这里**不提供** `update` / `delete` 的服务函数，模型上也不放 `updated_at`。
数据库层面没有触发器强制它（那会让"清理过期审计"这条合法的运维操作
也变得不可能），约束靠两件事：

1. 服务层只导出 `record()` 与查询；
2. `tests/test_audit.py` 里有一条测试断言**没有任何更新路径**。

如实记一条限制：**能改数据库的人仍然能改审计**。本平台是单机自托管，
数据库口令就写在同一个 compose 文件里，所以"防管理员篡改"在这里
不是一个能靠应用层解决的问题 —— 真需要的话要用外部日志收集
（把审计同时推到一个只增不改的远端），那超出本轮范围。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.columns import JsonColumn, UtcDateTime
from app.core.db import Base
from app.core.timeutil import now_utc


class AuditLog(Base):
    """一条审计记录。**只能追加。**"""

    __tablename__ = "audit_log"
    __table_args__ = (
        # 取值约束交给数据库，不只是应用层的枚举 —— 审计表的完整性
        # 不能依赖"所有写入路径都记得用那个枚举"。
        #
        # ⚠️ 名字只写 `actor_kind`：`core/db.py` 的 naming_convention 是
        # `ck_%(table_name)s_%(constraint_name)s`，会自己加上表名前缀。
        # 写成 `ck_audit_log_actor_kind` 会得到
        # `ck_audit_log_ck_audit_log_actor_kind`（autogenerate 真这么干了）。
        CheckConstraint(
            "actor_kind IN ('session', 'token')",
            name="actor_kind",
        ),
        # 最常见的一次查询是"最近发生了什么"，所以按时间倒序建索引。
        Index("ix_audit_log_at", "at"),
        # "这个扫描相关的操作"（追责时按目标查）。
        # ⚠️ 不用外键：审计记录必须在**被引用的对象删掉之后仍然存在**。
        # 一条指向已删除扫描的记录，比一条被级联删掉的记录有用得多。
        Index("ix_audit_log_target", "target"),
        Index("ix_audit_log_action_at", "action", "at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    # ★ 服务端时刻。**不接受客户端传入** —— 可伪造的时间戳在追责时没有价值，
    # 这正是契约 §5.4 把 `authorizationReceivedAt` 与客户端 `confirmedAt`
    # 分成两列的原因，这里是同一个判断。
    at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=now_utc)

    # 见 `core.vocab.AuditAction`。存字符串而不是数据库 enum 类型：
    # 加一个动作时不该需要一条 migration。
    action: Mapped[str] = mapped_column(String(48), nullable=False)

    # ── 主体（谁）────────────────────────────────────────
    # "会话"与"令牌"是两条独立通道（契约 §1.2），审计里必须能区分 ——
    # "管理员本人点的"与"某个 CI 令牌干的"是两件不同的事。
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # 可直接展示的称呼（"管理员会话" / 令牌的 label）。
    # 冗余存一份 label 而不是每次去 join `api_tokens`：令牌可以被删除，
    # 而"这条记录当初是哪个令牌干的"必须还能读出来。
    actor_label: Mapped[str] = mapped_column(String(120), nullable=False)
    # 管理员账号 id，或令牌通道下的 token_id。可空 —— 历史数据可能解析不出。
    actor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # ── 客体（对什么）────────────────────────────────────
    # 扫描 id / 报告编号 / 漏洞 id 列表。**不是外键**（见上面的说明）。
    target: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # 可直接展示的中文一句话。存在的理由与 `ApiError.message` 一样：
    # 读日志的人不该自己去拼「谁 + 做了什么 + 对什么」。
    summary: Mapped[str] = mapped_column(Text, nullable=False)

    # ── 来源（服务端才知道的，不可伪造）──────────────────
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 动作相关的最小补充。**绝不含凭据** —— 写它之前先过一遍
    # "这会不会把 key / 口令 / 连接串带进来"。见 app/audit/service.py。
    detail: Mapped[dict | None] = mapped_column(JsonColumn, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<AuditLog {self.action} by {self.actor_label} at {self.at}>"
