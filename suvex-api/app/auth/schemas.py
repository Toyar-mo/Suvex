"""认证域的出入参。

字段名逐字对齐契约 §3.1。注意契约里有一处**形状不一致**，这里照抄不"修"：

* `POST /auth/setup`、`POST /auth/login` → `{ "user": { id, createdAt } }`
* `GET  /auth/me`                        → `{ id, createdAt }`（直接就是用户对象）

照抄的理由是契约是唯一事实来源，而"前端已经按某个形状写了消费代码"比
"两个端点形状统一"更重要。真要统一，先改契约。

`password` 用 `str` 而不是 `SecretStr`：`SecretStr` 的价值在于防止
`repr()` 泄漏，但 FastAPI 不会打印请求体，而它会让几处校验多一层
`.get_secret_value()`。真正的防线是**绝不把口令写进日志**
（`service` 里没有任何 logger 调用带它）。
"""

from __future__ import annotations

from pydantic import Field

from app.core.schemas import CamelModel, StrictModel
from app.core.timeutil import IsoDateTime


class AuthState(CamelModel):
    """`GET /auth/state` 的响应 —— 前端启动时的第一个请求。

    只返回两个布尔值。不泄露账号数量、创建时间、是否有默认口令之类的信息。
    """

    # 系统初始化了没有（决定显示初始化页还是登录页）
    initialized: bool
    # 当前是否已登录（决定要不要跳登录）
    authenticated: bool


class PasswordRequest(StrictModel):
    """登录 / 初始化共用的请求体。

    单管理员系统里 `login` 只需要口令（契约 §3.1）——
    一个账号的系统里要用户名词只是多一个必填框。
    """

    password: str = Field(min_length=1)


class UserOut(CamelModel):
    """用户投影。`createdAt` 由 alias 生成器映射到 ORM 的 `created_at`。"""

    id: str
    createdAt: IsoDateTime


class UserEnvelope(CamelModel):
    """setup / login 的响应外壳。"""

    user: UserOut


class ApiTokenOut(CamelModel):
    """令牌的**投影**：只有 prefix，永远没有完整值（契约 §2.9）。

    前端 `ApiToken` 类型里没有、也永远不该有 `value` 字段 ——
    完整令牌只在创建响应里出现一次。所以这个类里没有它，
    而不是"有但填 None"：填 None 会让人以为某天它会填上值。
    """

    id: str
    label: str
    prefix: str
    createdAt: IsoDateTime
    # 从未使用为 null。
    lastUsedAt: IsoDateTime | None
    # null = 永不过期。允许它是**显式选项**，不是偷懒的默认值（契约 §6）。
    expiresAt: IsoDateTime | None
    scopes: list[str]


class CreatedTokenOut(CamelModel):
    """`POST /settings/tokens` 的响应（契约 §3.7）。

    ⚠️ `plaintext` 只在创建响应里出现**一次**。之后任何接口都拿不到它 ——
    服务端存的是哈希。这是所有正经产品的做法（GitHub、Stripe 都如此）。
    """

    token: ApiTokenOut
    plaintext: str
