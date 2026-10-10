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
    """**登录**的请求体（v0.24 起要用户名）。

    ## 为什么现在需要用户名

    单管理员时代这里只有 `password`，注释写的是"一个账号的系统里要
    用户名词只是多一个必填框"。多用户之后那句话不再成立 ——
    登录必须能区分是谁，否则审计日志里的"谁"只能记成会话 id，
    而那回答不了"上周是谁把 submitMode 改成 auto 的"。

    ## 与 `/auth/setup` 的分工

    `setup` 用 `SetupRequest`（含 username），因为它同时在**创建**账号；
    这里只做校验，不创建任何东西。
    """

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1)


class SetupRequest(StrictModel):
    """**首次初始化**的请求体。

    与登录分开定义，而不是共用 `PasswordRequest`：`setup` 的
    `username` 是"你要创建的这个管理员叫什么"，而登录的 `username`
    是"你是谁" —— 两者的语义不同，而共用一个类会让将来想给
    其中一个加字段时误伤另一个（比如给 setup 加"确认口令"）。
    """

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1)


class UserOut(CamelModel):
    """用户投影。

    ⚠️ **不含 `passwordHash`**（那是显然的），也**不含 `updatedAt`** ——
    它没有任何消费方，而每一个多余字段都是一个信息面。
    `role` 必须有：前端要据此决定显不显示"用户管理"入口。
    """

    id: str
    username: str
    role: str
    createdAt: IsoDateTime


class UserEnvelope(CamelModel):
    """setup / login 的响应外壳。"""

    user: UserOut


class CreateUserRequest(StrictModel):
    """`POST /users` —— 管理员加人。

    `role` **必填**（没有默认值）：给默认值会让"忘了传"静默地
    造出一个管理员，而管理员能做的第一件事就是发起扫描（对外发包）。
    """

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1)
    role: str


class UpdateUserRequest(StrictModel):
    """`PATCH /users/{id}` —— 改角色。

    目前**只能改角色**。改名与改口令是另外两件事：
    改名会让审计日志里的历史记录与当前名字对不上（那是有损的），
    改口令应当由本人走 `/settings/password`（管理员替别人改口令
    意味着管理员知道别人的口令，那不是一个好模型）。
    """

    role: str


class UserListOut(CamelModel):
    """`GET /users`。**裸数组**，与 `GET /settings/tokens` 同形状。"""

    users: list[UserOut]


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
