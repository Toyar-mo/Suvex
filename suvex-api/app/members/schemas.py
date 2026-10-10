"""处理人名单的请求/响应模型（契约 §3.10）。"""

from __future__ import annotations

from pydantic import Field

from app.core.schemas import CamelModel, StrictModel
from app.core.timeutil import IsoDateTime


class MemberOut(CamelModel):
    id: str
    name: str
    email: str | None
    active: bool
    createdAt: IsoDateTime


class MemberCreateIn(StrictModel):
    name: str = Field(min_length=1, max_length=60)
    email: str | None = Field(default=None, max_length=160)


class MemberUpdateIn(StrictModel):
    """改名 / 改邮箱 / 停用启用。**只送要改的字段**（PATCH 语义）。"""

    name: str | None = Field(default=None, min_length=1, max_length=60)
    email: str | None = Field(default=None, max_length=160)
    active: bool | None = None


class MemberDeleteResult(CamelModel):
    """删除的结果。

    ★ **不用 204**（任务 10 P2 实测后改的）。

    删掉一个处理人会**解除他名下所有漏洞的指派**（`ON DELETE SET NULL`）。
    204 没有响应体，前端就拿不到那个条数 —— 于是"5 条漏洞的负责人
    静默变成了未指派"，而用户看到的只是列表里少了一个人。

    实测时正是这么发生的：删完之后那 5 条的负责人无声消失，
    页面上没有任何提示。所以返回这个数字，让界面能说清代价。
    """

    unassigned: int
