"""`findings` 领域（漏洞条目与时间线）。

⚠️ **本文件最要紧的一件事：`verification` 与 `status` 是两列。**

`data/finding.ts` 顶部用一整段注释论证过：一条漏洞可以是
「已用 PoC 证明存在（verified），但判定不影响业务（false_positive）」。
合成一个字段，这个真实场景就无处表达 —— 而它是渗透测试里最常见的争议点。

所以表结构上必须是两列。合并省下的复杂度会在别处以 bug 的形式还回来：
「误报」和「验证不足」在同一个字段里，统计与筛选就永远分不清。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.columns import JsonColumn, UtcDateTime
from app.core.db import Base
from app.core.timeutil import now_utc
from app.core.vocab import FindingCategory, FindingStatus, Severity, Verification, values


class Finding(Base):
    """一条漏洞。

    `detail` 在前端是一个嵌套对象（`FindingDetail`），这里**摊平成列**而不是
    存一个 JSON 块：
    * `severity` / `category` / `verification` / `status` 都要能筛选和排序，
      而它们在 JSON 里查起来要么全表扫、要么建表达式索引（多一份口径要维护）；
    * `poc_code` 是可能很长的文本，混在 JSON 里会让每行都很大；
    * 摊平之后加字段就是加列，迁移是显式的 —— JSON 里加字段是隐式的，
      而隐式 schema 变更正是「文档与实现漂移」最喜欢藏身的地方。

    ⚠️ `detail.poc` 与 `detail.remediation` 各自可空，**且可空有确切含义**：
    `poc: null` = 没有脚本；`remediation: null` = agent 没把握给建议。
    这两者与「空字符串」不是一回事，所以列可空、默认 NULL，
    不要用 `""` 兜底（那会把「没有」变成「有但是空的」）。
    """

    __tablename__ = "findings"
    __table_args__ = (
        CheckConstraint(
            f"severity IN ({', '.join(repr(v) for v in values(Severity))})", name="severity_valid"
        ),
        CheckConstraint(
            f"category IN ({', '.join(repr(v) for v in values(FindingCategory))})",
            name="category_valid",
        ),
        CheckConstraint(
            f"verification IN ({', '.join(repr(v) for v in values(Verification))})",
            name="verification_valid",
        ),
        CheckConstraint(
            f"status IN ({', '.join(repr(v) for v in values(FindingStatus))})", name="status_valid"
        ),
        Index("ix_findings_scan_id", "scan_id"),
        # 列表页默认按发现时间倒序，且常带 severity/status 过滤 —— 组合索引
        # 让「筛选 + 排序」走同一条索引，不用先筛后排。
        Index("ix_findings_severity_discovered_at", "severity", "discovered_at"),
        Index("ix_findings_status_discovered_at", "status", "discovered_at"),
        # 「按负责人筛选」（任务 10 P2）。声明在这里而不只是写在迁移里：
        # 两处不一致的话，下一次 `--autogenerate` 会认为"该建"而重复生成一次。
        Index("ix_findings_assignee_id", "assignee_id"),
        # 「临期 / 超期」筛选与"最该催的排前面"（任务 10 P3）
        Index("ix_findings_sla_due_at", "sla_due_at"),
        # 回收站查询（任务 10 P6-a）。**部分索引**：只索引 `deleted_at`
        # 非空的行（回收站里那些，通常是极少数）。
        #
        # ★ 给少数派建索引才有价值：常规查询带 `deleted_at IS NULL`
        #   （几乎全部行），给它建索引等于给每行都建一条目，
        #   查询计划器最终还是会选顺序扫描。
        #
        # 声明在这里而不只是写在迁移里：两处不一致的话，下一次
        # `--autogenerate` 会认为"该建"而重复生成一次（同
        # `ix_findings_assignee_id` 那条注释的理由）。
        Index(
            "ix_findings_deleted_at",
            "deleted_at",
            postgresql_where=text("deleted_at IS NOT NULL"),
            sqlite_where=text("deleted_at IS NOT NULL"),
        ),
        # ★ 幂等键（任务 5）：同一次扫描里，外部工具的同一个发现只能有一条记录。
        #
        # 导入器会反复读 `vulnerabilities.json`（扫描还在写它），
        # 没有这道索引就会把同一条漏洞重复落库 —— 而 Findings 页上会出现
        # 五条一模一样的记录，用户会以为工具重复报了五次。
        #
        # 部分唯一索引（只对非空生效）：手工建的、或将来别的来源建的记录
        # `source_ref` 是 NULL，它们本来也不冲突。
        Index(
            "uq_findings_scan_source",
            "scan_id",
            "source_ref",
            unique=True,
            postgresql_where=text("source_ref IS NOT NULL"),
            sqlite_where=text("source_ref IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scan_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("scans.id", ondelete="CASCADE"), nullable=False
    )

    # 外部工具的发现编号（`vuln-0001`）。幂等键 + 溯源。
    # 可空：手工建的记录、或将来别的来源没有这个概念。
    source_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 外部工具的 CWE / CVSS。**内部保留**（不出现在响应里）：
    # 契约 §2.2 的 `Finding` 没有这两个字段，而加字段是前端类型变更。
    # 留着是为了将来做"按 CWE 聚合"与排序时有据可查，而不是现在就用。
    cwe: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cvss_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    target: Mapped[str] = mapped_column(String(512), nullable=False)

    # ── 两个轴（绝不可合并）────────────────────────────────
    # 轴一：证据强度。**agent 产出**，人工流转不改它。
    verification: Mapped[str] = mapped_column(String(16), nullable=False, default=Verification.NONE.value)
    # 轴二：处置状态。**人工流转**，agent 不改它。流转规则见 core/vocab。
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=FindingStatus.NEW.value)

    discovered_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=now_utc)

    # ── 指派（任务 10 P2）──────────────────────────────────
    #
    # ★ 指向 `members.id` 而不是登录账号（`admin_accounts.id`）。
    #
    # 本系统是**单管理员**的：`/auth/setup` 一次性初始化，之后没有注册入口，
    # 也没有第二个账号。所以"指派给谁"根本不可能是"指派给某个登录用户" ——
    # 它要回答的是"这条漏洞该由**谁**去修"，而那个人往往是客户端的人、
    # 运维、或者某个同事，他们**不登录这个系统**。
    #
    # 用独立名单表而不是自由文本：按负责人筛选才不会被错别字撕成好几组
    # （"张三" / "张 三" / "zhangsan" 会变成三个不同的负责人）。
    #
    # ON DELETE SET NULL：成员被停用/删除后，漏洞不该跟着消失，
    # 只是变成"未指派"。用 `active=False` 停用而不是删除，
    # 历史指派仍能显示名字 —— 见 `Member` 的说明。
    assignee_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("members.id", ondelete="SET NULL"),
        nullable=True,
    )
    assigned_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    # ── SLA 到期时刻（任务 10 P3）──────────────────────────
    #
    # ★ **冗余存储**，不是每次查询现算 `discovered_at + N 天`。
    #
    # 到期提醒要能**排序**（"最该催的排前面"）也要能**筛**
    # （"只看超期的"），而现算的表达式用不上索引 —— 那会变成每次
    # 切筛选都全表扫。冗余的代价是三个时机要重算：状态变更、规则变更、
    # 以及导入时。三处都在服务端，**前端永远不算 SLA**。
    #
    # NULL 有两种含义，都是对的：
    #   · 这条的严重度档位设了 0 天（不设时限）；
    #   · 它已经到了终态（已关闭 / 误报），不再计时。
    # 两者在界面上都表现为"不显示倒计时"，所以不必区分。
    sla_due_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    # ── FindingDetail（摊平的列）───────────────────────────
    detail_description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 受影响位置，可能多个。
    detail_affected: Mapped[list[str]] = mapped_column(JsonColumn, nullable=False, default=list)
    # ⚠️ 「为什么判断这里有问题」—— 不是 description 的同义反复。
    detail_reasoning: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 真实响应片段，**原样保留不翻译**。
    detail_evidence: Mapped[str] = mapped_column(Text, nullable=False, default="")

    poc_language: Mapped[str | None] = mapped_column(String(32), nullable=True)
    poc_code: Mapped[str | None] = mapped_column(Text, nullable=True)

    # NULL = agent 没把握给建议。不要用空串兜底。
    remediation: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=now_utc, onupdate=now_utc
    )

    # ── 软删除（任务 10 P6-a）──────────────────────────────
    #
    # ★ 与 `scans.deleted_at` **同一套语义**（契约 §2.1 的那条规则）：
    #   用时间戳不用布尔 —— 布尔丢掉"什么时候删的"，而回收站要按时间
    #   算到期、界面上也要能说"3 天前删的"。
    #
    # 非空 = 在回收站。**列表与统计默认都排除它**，只有显式
    # `?deleted=true` 才看得到 —— 见 `_filter_conditions`。
    #
    # 为什么要软删除而不是直接删：漏洞是**交付物的组成部分**
    # （报告会引用它们），误点删除就丢数据是不可接受的。真正的
    # 物理删除只在 `purge_finding`（用户在回收站里明确要求）
    # 且**没有被报告引用**时发生。
    deleted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    attachments: Mapped[list[FindingAttachment]] = relationship(
        back_populates="finding",
        cascade="all, delete-orphan",
    )

    timeline: Mapped[list[FindingTimelineEntry]] = relationship(
        back_populates="finding",
        cascade="all, delete-orphan",
        # 契约 §2.2：`timeline` 是**倒序**（最新在前）。
        # 但 `at` 只到毫秒，同一毫秒内的两条无法定序 —— 所以补一个 `seq`
        # 作为「同一时刻内的写入顺序」。只按 at 排会让时间线在快速连续
        # 流转时随机换位，而那种抖动看起来就像丢了一条。
        order_by="FindingTimelineEntry.at.desc(), FindingTimelineEntry.seq.desc()",
    )

    @property
    def poc(self) -> dict[str, str] | None:
        """`detail.poc` 的投影。两列都空 ⇒ None（不是空对象）。"""
        if self.poc_code is None and self.poc_language is None:
            return None
        return {"language": self.poc_language or "", "code": self.poc_code or ""}

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"<Finding id={self.id!r} severity={self.severity!r} "
            f"verification={self.verification!r} status={self.status!r}>"
        )


class FindingTimelineEntry(Base):
    """时间线的一条。

    `actor` 区分「agent 产出」与「人工流转」；`from`/`to` **仅 `status_changed` 有值**。

    ⚠️ 幂等流转**不写时间线**（契约 §2.3）：把已经是 `confirmed` 的条目
    再标 `confirmed` 要返回成功但 `changed: 0`，且不留记录 ——
    否则时间线里会出现「待确认 → 待确认」的噪音，把真正的流转淹掉。

    `seq` 是 finding 内的自增序号。它不对外暴露，只用来给同一毫秒内的
    多条记录定序（见 `Finding.timeline` 的注释）。
    """

    __tablename__ = "finding_timeline"
    __table_args__ = (
        CheckConstraint("actor IN ('agent', 'user')", name="actor_valid"),
        UniqueConstraint("finding_id", "seq", name="uq_finding_timeline_finding_seq"),
        Index("ix_finding_timeline_finding_at", "finding_id", "at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    finding_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("findings.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)

    at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=now_utc)
    actor: Mapped[str] = mapped_column(String(16), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)

    # ⚠️ 列名用 `from_status`；JSON 字段名必须是 `from`（契约 §2.2）。
    # `from` 是 Python 关键字，不能直接当属性名 —— 转换在 schemas 里做，
    # 用 alias 而不是改名。改名会违反「字段名与前端逐字一致」。
    from_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(16), nullable=True)

    finding: Mapped[Finding] = relationship(back_populates="timeline")


class FindingAttachment(Base):
    """一条漏洞的附件（任务 10 P6-d）。

    ## ★ 存储路径**不由上传的文件名决定**

    `stored_path` 是服务端从附件 id 生成的（`<root>/<finding_id>/<aid><ext>`），
    而 `filename` 只是**给用户看的原始名字**。

    这是这一项里最要紧的一条安全决定。用上传文件名拼路径会同时打开两个洞：

    1. **路径穿越**：`../../etc/passwd` 或 `..\\..\\config.py` 会让写入
       落到附件根之外。即使做了 `os.path.basename`，符号链接、
       Windows 的保留名（`CON`、`NUL`）、大小写不敏感文件系统上的
       覆写都还有各自的坑。
    2. **覆写**：两个不同的人上传 `screenshot.png`，后者会**静默覆盖**前者 ——
       而"证据被另一个人的截图替换了"是不可接受的。

    从 id 生成就把这两个洞一起关掉了：路径完全由服务端控制、天然唯一，
    且原始文件名仍然完整保留在 `filename` 里供显示与下载。

    ## 为什么 `size_bytes` 与 `content_type` 冗余存

    列表要在**不读磁盘**的前提下显示"3 个附件 · 共 2.4 MB"。
    每次都去 `stat()` 一遍会让列表接口依赖文件系统（文件被手工删掉时
    还会不一致）。它们在上传时写定，是那时的事实。

    ## 物理文件与数据库行的一致性

    `ON DELETE CASCADE` 只管数据库行 —— **删行不会删磁盘上的文件**。
    所以删除附件的代码必须先删文件再删行（或者反过来但容忍残留），
    见 `attachments.delete_attachment` 的说明。
    """

    __tablename__ = "finding_attachments"
    __table_args__ = (
        Index("ix_finding_attachments_finding_id", "finding_id"),
        # 被附件占用的存储路径必须唯一 —— 它是从 id 生成的，
        # 所以这条约束真正防的是"生成逻辑写错了导致两个附件撞路径"。
        UniqueConstraint("stored_path", name="uq_finding_attachments_stored_path"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    finding_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("findings.id", ondelete="CASCADE"), nullable=False
    )

    # 用户上传时的原始文件名。**只用于显示与下载**，不参与路径构造。
    filename: Mapped[str] = mapped_column(String(300), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 服务端生成的相对路径（相对 `attachments_dir`）。
    # 存相对路径而不是绝对路径：换部署目录时不用改数据。
    stored_path: Mapped[str] = mapped_column(String(512), nullable=False)

    # 谁传的。单管理员系统下恒为同一个账号，但留痕要能被独立理解
    # （契约 §5.4 的同一条理由）。
    uploaded_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=now_utc)

    finding: Mapped[Finding] = relationship(back_populates="attachments")
