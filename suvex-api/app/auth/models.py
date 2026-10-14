"""认证领域的表。

三张表，对应契约 §1.2 的**两条独立通道**（不要合并）：

| 表              | 服务谁          | 凭证                        | 校验方式          |
| --------------- | --------------- | --------------------------- | ----------------- |
| `admin_accounts`| 管理员本人      | 口令（argon2id）            | 登录时比对        |
| `auth_sessions` | 人在浏览器      | 会话 Cookie（不透明随机串） | 每次请求查摘要    |
| `api_tokens`    | 机器 / CI       | `Authorization: Bearer svx_…`| 每次请求查摘要    |

`auth_sessions` 而不是 `sessions`：`Session` 是 SQLAlchemy 的核心概念，
表名也叫 sessions 会让「session」在代码里指两件事 —— 读代码时要靠上下文猜。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.columns import JsonColumn, UtcDateTime
from app.core.db import Base
from app.core.timeutil import now_utc


class AdminAccount(Base):
    """一个登录账号（v0.24 起支持多个）。

    ## 从"单管理员"到"多用户"改了什么

    表结构其实**早就为这一刻留了余地**（id、时间戳、sessions 关系都在），
    当时注释写的是"将来真加第二个人时，改的是插入逻辑而不是表结构"。
    那句话基本兑现了，但漏了两件事 —— 因为单账号时代**它们不需要存在**：

      · `username` —— 登录要用它区分是谁。原来只输口令，因为"一个账号的
        系统里要用户名词只是多一个必填框"；
      · `role` —— 权限分档的依据。

    两个都加了**唯一/非空约束**，而不是"可空 + 应用层保证"：
    数据完整性交给数据库，因为它不会忘记。

    ## ⚠️ 关于 `role` 的默认值

    默认 `admin` 是为了**存量数据回填**（迁移时那唯一一行必须是管理员，
    否则升级后没人能改设置了）。而新建账号时**必须显式给角色** ——
    见 `service.create_user`，它没有默认参数。

    这个区分很重要：给 `create_user` 一个 `role="admin"` 的默认值，
    会让"忘了传角色"静默地造出一个管理员。而管理员能做的第一件事
    就是发起扫描（对外发包）。
    """

    __tablename__ = "admin_accounts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    # 登录名。**大小写不敏感**（见 `service.normalize_username`）：
    # 用户不该因为记错大小写而登不进去，而"Alice"与"alice"是两个账号
    # 只会带来困惑（谁是谁？）。
    #
    # 存的是**归一化后**的值（小写），原始大小写不保留 —— 与 `Scan.target`
    # 存归一化值是同一条原则：让同一个东西只有一种表示。
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)

    # argon2id 的完整编码串（含参数与盐），长度约 97 字符。用 Text 留余量：
    # 将来调高 memory_cost 会让串变长，String(128) 会在那时变成生产事故。
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)

    # 角色。取值见 `core.vocab.UserRole`。
    #
    # ⚠️ 这里**不加 CHECK 约束**（与项目里其它枚举列的做法不同）。
    #    理由是它的取值集合将来最可能变（加一档"能扫描但不能改设置"
    #    是很自然的演进），而每加一档就要写一个迁移重建 CHECK ——
    #    与 `TargetKind` 那条"加枚举值要手写迁移"的坑是同一个。
    #    未知角色由 `scopes_for_role` 兜成空集（403），是安全的方向。
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="admin")

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=now_utc, onupdate=now_utc
    )

    sessions: Mapped[list[AuthSession]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        # ⚠️ 绝不把 password_hash 放进 repr：repr 会进日志、进异常消息、进调试器面板。
        return f"<AdminAccount id={self.id!r} username={self.username!r} role={self.role!r}>"


class AuthSession(Base):
    """浏览器会话。

    **只存令牌摘要**（SHA-256）。理由不是「怕拖库」这么泛：
    这个平台的危害是「替攻击者发包」，一个能读到会话表的攻击者
    可以直接用会话调 `POST /scans`。存摘要让「读表」不等于「拿到会话」。

    `revoked_at` 是软吊销而不是删行：契约 §1.2 要求「改密码后旧令牌立即失效」，
    而删行会让「这条会话什么时候没的」无法回答 —— 安全事件复盘时需要它。
    """

    __tablename__ = "auth_sessions"
    __table_args__ = (Index("ix_auth_sessions_user_id", "user_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # 唯一索引同时承担「查找」与「防重」两个职责。
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("admin_accounts.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=now_utc)
    # 滚动过期：每次使用都往后推（见 service.touch_session）。
    # 绝对过期时间落在这一列上，不是「创建时刻 + TTL」现算 ——
    # 现算的话续期就没有载体了。
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=now_utc)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    # 来源信息。契约 §5.4 把「服务端时刻与来源」列为留痕的真正价值 ——
    # 客户端可以伪造 confirmedAt，但伪造不了这两项。
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped[AdminAccount] = relationship(back_populates="sessions")


class ApiToken(Base):
    """机器令牌。

    `prefix` 与 `token_hash` 是两件事，别合并：
    prefix 是**明文的前 8 字符**，用于界面上辨认「这是哪一个令牌」；
    hash 用于校验。契约 §2.9 明确「`ApiToken` 只有 `prefix`，永远没有 `value`」。

    `expires_at` 可空 = 永不过期，且这是**显式选项**不是偷懒的默认值
    （契约 §6）：强制过期会被绕过（用户写死一个永久 token 然后忘记它），
    不如让它显式并标注风险。
    """

    __tablename__ = "api_tokens"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    # scope 列表。用 JSONB 而不是关联表：它是个短小的无序集合，
    # 从不单独查询「哪些令牌有 scan 权限」以外的用法，建表是过度设计。
    scopes: Mapped[list[str]] = mapped_column(JsonColumn, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=now_utc)
    last_used_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    def is_active(self, at: datetime | None = None) -> bool:
        moment = at or now_utc()
        if self.revoked_at is not None:
            return False
        return self.expires_at is None or self.expires_at > moment
