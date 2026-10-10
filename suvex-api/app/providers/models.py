"""`providers` 领域（LLM Provider 账号）。

★ **本表的凭据列是密文。** 这一条是整个任务 2 的核心，值得说清为什么
表结构长成这样：

| 列                    | 内容                                        | 为什么单独一列                      |
| --------------------- | ------------------------------------------- | ----------------------------------- |
| `api_key_encrypted`   | Fernet 密文（`core/crypto.py`）             | 唯一持有凭据的地方                  |
| `api_key_hint`        | 可展示掩码，形如 `sk-****abcd`              | 让**读接口不必解密**                |

`api_key_hint` 单独存而不是每次解密算出来，是一个刻意的取舍：
解密一次就要把明文带进内存、越过 service 层，而读接口（GET /providers）
**根本不需要明文**。把掩码预先算好存下来，读路径上就永远不出现明文 ——
"明文只在真的要调用 LLM 的那一刻才被解开"这句话，因此是可以验证的，
而不只是一句承诺。

代价：掩码与密文必须同时更新。所以它们只在 `providers/service.py` 的
一个函数里被写入，别处不允许碰这两列。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.columns import UtcDateTime
from app.core.db import Base
from app.core.timeutil import now_utc
from app.core.vocab import ProviderKind


class ProviderAccount(Base):
    """一个 provider 的**凭据与连接配置**。

    与 `data/providers.ts` 的 `ProviderConfig` 的分工（前端注释里专门论证过）：
    `ProviderConfig` 是给消费者看的投影（要不要展示、能不能选），
    `ProviderAccount` 是凭据本身。向导只需要知道「这个能用」，
    所以 **`apiKey` 不进入向导的数据结构** —— 它会渲染进 DOM、可能被打印、被截图。
    """

    __tablename__ = "provider_accounts"
    __table_args__ = (
        # 「全部账号中至多一个 isDefault=true」——用**部分唯一索引**在数据库层强制。
        #
        # 这一条不是"顺手加的约束"，它是任务 2 的要求之一：
        # 「同一时间只能有一个默认，切换默认时要有事务保证不会出现两个同时为默认的脏数据」。
        #
        # 关键在于**约束在哪一层**。只在 service 里判断的话，
        # 两个并发请求同时"设为默认"会各自看到"当前没有冲突"，然后都写下去 ——
        # 留下两个 true，而症状是「扫描用了哪个 provider」变得不确定。
        # 数据库层的唯一索引让第二个事务直接失败，而不是静默留下脏数据。
        #
        # 两个方言各写一份 where：SQLite 没有 `WHERE is_default` 这种布尔列语法。
        Index(
            "uq_provider_accounts_default",
            "is_default",
            unique=True,
            postgresql_where=text("is_default"),
            sqlite_where=text("is_default = 1"),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # 品牌名保留原文（"DeepSeek" 译成中文反而不可检索）
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    model: Mapped[str] = mapped_column(String(160), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default=ProviderKind.CLOUD.value)

    # ── 凭据（★ 密文）───────────────────────────────────────
    # 可空：没有凭据时是 NULL，不是"加密过的空串"。
    # 本地模型（Ollama）合法地不需要 key，所以 NULL 是一个正常状态。
    api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 可展示掩码。**不含明文**，且经过 crypto.mask_secret 生成 ——
    # 手写这一列会被测试抓出来（test_secret_hygiene）。
    api_key_hint: Mapped[str | None] = mapped_column(String(40), nullable=True)

    # 空串 = 用官方默认地址（契约 §2.9）。刻意不用 NULL 表示「未配置」——
    # 前端类型是 `string`，空串有确切含义，混入 NULL 会多出一种「空」。
    base_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # ★ 改 key 必须把这两列重置（契约 §3.2）。
    # 沿用旧的 verified=true 会让一个从未验证过的新 key 显示成「已验证可用」，
    # 而新建扫描向导的 `configured` 读的正是它 —— 用户会在扫描跑到一半时认证失败。
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    verify_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=now_utc, onupdate=now_utc
    )

    # ⚠️ 这里**没有** `api_key` 属性，也刻意不提供。
    #
    # 让 ORM 对象根本没有一条通往明文的路，比"提供一个会返回空串或掩码的
    # api_key 属性"更安全：后者会诱使别处写 `provider.api_key`，
    # 而读到的东西取决于那个属性当时的实现 —— 某天有人把它改成返回明文
    # 来"修一个 bug"，就没有任何东西会挡住。
    # 明文只能通过 `service.decrypt_provider_key()` 拿到，那个函数的名字
    # 就说明了代价。

    @property
    def has_key(self) -> bool:
        """契约 §2.9 的只读字段 `hasKey`。

        GET 里 `apiKey` 一律是空串，但空串在前端有确切含义（「未配置」）。
        所以必须配一个 `hasKey` 才不歧义 —— 否则「配置了但不回填」
        和「根本没配」在响应里长得一模一样。
        """
        return bool(self.api_key_encrypted)

    @property
    def api_key_mask(self) -> str:
        """可展示的掩码；没有凭据时空串。"""
        return self.api_key_hint or ""

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        # ⚠️ 不含凭据、也不含密文。repr 会进日志、异常消息、调试器面板。
        # 密文不算明文，但它出现在日志里同样没有好处，而且会诱导别人
        # "反正只是密文" —— 那是一条会慢慢滑坡的边。
        return f"<ProviderAccount id={self.id!r} label={self.label!r} verified={self.verified}>"
