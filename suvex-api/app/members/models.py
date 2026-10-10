"""处理人名单的 ORM 模型。

⚠️ 表名是 `members`，与 `app/settings/models.py` 里的配置类**同一个库**，
但归属这个包 —— 它不是配置，是业务实体（会被 `findings.assignee_id` 引用）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.columns import UtcDateTime
from app.core.db import Base
from app.core.timeutil import now_utc

# 上限是必要的：它是给人用的名单，200 人已经远超任何单机自托管场景；
# 没有上限等于给脚本留了个撑爆的入口。
MAX_MEMBERS = 200
MAX_MEMBER_NAME = 60


class Member(Base):
    """处理人名单 —— 漏洞可以指派给谁（任务 10 P2）。

    ## 为什么不是登录账号

    本系统是**单管理员**的：`/auth/setup` 一次性初始化，之后没有注册入口，
    也没有第二个账号。所以"指派给谁"要回答的不是"指派给哪个登录用户"，
    而是"这条漏洞该由**谁**去修" —— 而那个人往往是客户端的人、运维、
    或者同事，他们**根本不登录这个系统**。

    把 `findings.assignee_id` 指向 `admin_accounts`，会让这个功能在
    单账号系统里退化成一个只能指派给自己的笑话。

    ## 为什么用名单表而不是自由文本

    按负责人筛选是它的主要用途之一，而自由文本会被错别字撕成好几组：
    「张三」/「张 三」/「zhangsan」变成三个不同的负责人，
    于是"这个人手上有几条"就再也数不准了。

    ## 停用而不是删除

    `active=False` 之后不再出现在"可指派"的候选里，但**历史指派仍然显示
    名字** —— 直接删掉会让所有指派过他的漏洞突然显示成"未指派"，
    而那抹掉的是一段真实的协作记录。真要硬删时，
    `findings.assignee_id` 上的 `ON DELETE SET NULL` 会兜住。
    """

    __tablename__ = "members"
    __table_args__ = (
        # 同名不允许 —— 这是"防止错别字撕成好几组"的延伸。
        # 入库前会 strip，所以差异只剩内部空格。
        UniqueConstraint("name", name="uq_members_name"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(MAX_MEMBER_NAME), nullable=False)
    email: Mapped[str | None] = mapped_column(String(160), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=now_utc, onupdate=now_utc
    )
