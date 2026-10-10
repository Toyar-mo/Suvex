"""认证域的 HTTP 端点（契约 §3.1）。

| 方法 | 路径           | 鉴权                       |
| ---- | -------------- | -------------------------- |
| GET  | `/auth/state`  | public                     |
| POST | `/auth/setup`  | public（**仅未初始化时**） |
| POST | `/auth/login`  | public                     |
| POST | `/auth/logout` | 幂等（见下）               |
| GET  | `/auth/me`     | read                       |

关于 `logout`：契约 §3.1 写的是 `read`，这里放宽成「幂等、不需要有效会话」，
并同步改了契约那一格。理由是登出的语义是**丢弃自己的凭据** ——
如果凭据已经过期，`401` 会让前端只能跳登录页而不能清掉那个失效 Cookie，
而 Cookie 留在浏览器里没有任何好处。放宽也不会被利用：
登出不是特权动作，跨站触发它最多让人重新登录一次（而且
`SameSite=Strict` 下跨站请求根本带不上 Cookie，实际连这个都做不到）。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.orm import Session

from app.audit import service as audit
from app.auth import service
from app.auth.deps import (
    Identity,
    client_ip,
    require_admin_session,
    require_read,
    resolve_identity,
)
from app.auth.schemas import (
    AuthState,
    CreateUserRequest,
    PasswordRequest,
    SetupRequest,
    UpdateUserRequest,
    UserEnvelope,
    UserListOut,
    UserOut,
)
from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import ApiError, ErrorCode
from app.core.ratelimit import Limiter
from app.core.timeutil import now_utc
from app.core.vocab import AuditAction, UserRole

router = APIRouter(prefix="/auth", tags=["认证"])


@lru_cache(maxsize=1)
def _login_limiter() -> Limiter:
    """登录/初始化的限流器。

    契约 §3.1：**必须限流**（建议同一来源 5 次/分钟、15 次/小时）。
    口令是本系统唯一的凭据，不限流等于允许在线爆破。

    延迟构造（`lru_cache` 而不是模块级常量）是为了让测试能改配置后重建 ——
    模块级常量一旦导入就固定了，测试里改 `login_limit_per_minute`
    不会生效，而那种测试会「永远通过」。
    """
    cfg = get_settings()
    return Limiter(
        [(cfg.login_limit_per_minute, 60), (cfg.login_limit_per_hour, 3600)],
        name="auth",
    )


def reset_limiters() -> None:
    """清空限流状态。**只给测试用** ——

    测试之间必须互相隔离，否则第二个用例会因为第一个用例打满了配额而失败，
    而失败的样子是「偶发 429」，排查起来会往限流实现上找错方向。
    """
    _login_limiter.cache_clear()


def _set_session_cookie(response: Response, raw_token: str) -> None:
    """下发会话 Cookie。四个属性缺一不可（契约 §1.2 有后果表）：

    | 属性              | 漏掉的后果                                          |
    | ----------------- | --------------------------------------------------- |
    | `HttpOnly`        | 页面渲染不可信文本（evidence、poc.code），XSS 一成功就偷走会话 |
    | `SameSite=Strict` | **CSRF**：任意网页都能替用户发起扫描                |
    | `Secure`          | 明文信道泄露会话                                    |
    | `Max-Age`         | 会话永久有效                                        |
    """
    cfg = get_settings()
    response.set_cookie(
        key=cfg.session_cookie_name,
        value=raw_token,
        max_age=cfg.session_ttl_hours * 3600,
        httponly=True,  # JS 读不到
        samesite="strict",  # 跨站请求不带它
        secure=cfg.cookie_secure,  # 生产走 https 时必须 true
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    """删 Cookie。

    ⚠️ 删除时必须带上**与设置时相同的 `path` / `secure` / `samesite`**。
    浏览器按 (name, domain, path) 匹配 Cookie，path 不一致时
    `Set-Cookie: ...; Max-Age=0` 删掉的是另一个不存在的 Cookie ——
    表现是「点了登出，刷新一下又登录着」。这是最容易踩的一次。
    """
    cfg = get_settings()
    response.delete_cookie(
        key=cfg.session_cookie_name,
        path="/",
        httponly=True,
        samesite="strict",
        secure=cfg.cookie_secure,
    )


@router.get("/state", response_model=AuthState, summary="启动状态（前端第一个请求）")
def read_state(
    request: Request, db: Annotated[Session, Depends(get_db)]
) -> AuthState:
    """回答两个问题：系统初始化了没有、当前是否已登录。

    ⚠️ 这个端点**必须保持 public** —— 它在登录前就要能调用。
    但它只返回两个布尔值，不泄露任何其他信息（没有账号数量、没有创建时间、
    没有"口令是不是默认值"之类的暗示）。
    """
    return AuthState(
        initialized=service.has_any_account(db),
        authenticated=resolve_identity(request, db) is not None,
    )


@router.post(
    "/setup",
    status_code=status.HTTP_201_CREATED,
    response_model=UserEnvelope,
    summary="一次性初始化：创建第一个管理员",
)
def setup(
    payload: SetupRequest,
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
) -> UserEnvelope:
    """首次启动创建第一个管理员，并**自动登录**。

    ★ 这个端点在「已初始化」后**彻底关闭**。否则它就是一个
    「任意人可创建管理员」的后门 —— 而本平台的危害不是读到数据，
    是替攻击者发包（契约 §0.3）。

    ⚠️ 多用户之后**这个端点不负责加人** —— 那是 `POST /users`，
    它要求已登录的管理员。两者共用 `create_account`，但
    "第一个"与"后来加的"是两件事（见 `service.create_account` 的说明）。
    """
    _login_limiter().check(client_ip(request))

    if service.has_any_account(db):
        raise ApiError.conflict(
            ErrorCode.SETUP_ALREADY_DONE,
            "系统已完成初始化，无法再次创建管理员。请直接登录。",
        )

    account = service.create_account(
        db, payload.password, username=payload.username, role=UserRole.ADMIN.value
    )
    raw, _session = service.create_session(
        db,
        account,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        ttl_hours=get_settings().session_ttl_hours,
    )
    _set_session_cookie(response, raw)
    return UserEnvelope(user=UserOut.model_validate(account))


@router.post("/login", response_model=UserEnvelope, summary="登录")
def login(
    payload: PasswordRequest,
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
) -> UserEnvelope:
    """**用户名 + 口令**（v0.24 起要用户名）。

    失败统一返回 `401 UNAUTHENTICATED`，`message` 只说
    「用户名或口令不正确」—— 不做「用户不存在」与「口令错」的区分，
    那是账号枚举漏洞。而对一个**会对目标发包**的平台来说，
    拿到一个有效用户名就是爆破的一半。

    ⚠️ **登录成功不清空限流计数**。清空的话，攻击者只要偶尔蒙对一次
    （或者从别处拿到一次有效登录）就能重置配额。而正常用户
    每分钟 5 次的上限完全够用。
    """
    _login_limiter().check(client_ip(request))

    account = service.authenticate(db, payload.username, payload.password)
    raw, _session = service.create_session(
        db,
        account,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        ttl_hours=get_settings().session_ttl_hours,
    )
    _set_session_cookie(response, raw)
    return UserEnvelope(user=UserOut.model_validate(account))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="登出")
def logout(
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    """吊销会话并清除 Cookie。幂等：没有会话也是 204。"""
    raw = request.cookies.get(get_settings().session_cookie_name)
    if raw:
        service.revoke_session(db, raw)
    _clear_session_cookie(response)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/me", response_model=UserOut, summary="当前用户")
def read_me(
    identity: Annotated[Identity, Depends(require_read)],
    db: Annotated[Session, Depends(get_db)],
) -> UserOut:
    """返回**当前登录的那个用户**。

    ## ★ v0.24 修的一处真实缺陷

    原来写的是 `service.get_account(db)` —— 取"第一个账号"。
    单管理员时代那没问题（只有一个）。多用户之后它会让**每个用户
    都看到同一个人的身份**，包括 `role`。

    后果不是"显示错个名字"那么轻：前端要据 `role` 决定显不显示
    "用户管理"入口，而一个 `viewer` 会看到管理入口（点进去被 403
    拦住，但"我为什么看得到"本身就是个 bug）。

    正确做法是从 `identity.user_id` 取 —— 那是**这次请求的凭据
    属于谁**，而不是"库里第一个是谁"。

    ⚠️ 令牌通道下 `user_id` 是 None（令牌不属于某个登录会话）。
    那时返回**签发它的账号**做不到（令牌没记这个），所以返回一个
    合成视图：`username` 用令牌的 label。这是诚实的 ——
    "用令牌访问时，你代表的是这个令牌"。
    """
    if identity.user_id is None:
        # 令牌通道。合成一个只读视图，而不是编一个不存在的账号。
        return UserOut(
            id=identity.token_id or "token",
            username=identity.label,
            role=UserRole.VIEWER.value,
            createdAt=now_utc(),
        )

    account = service.get_account_by_id(db, identity.user_id)
    if account is None:  # pragma: no cover - resolve_identity 已经保证了它存在
        raise ApiError.unauthenticated()
    return UserOut.model_validate(account)


# ══════════════════════════════════════════════════════════
# 用户管理（v0.24）
# ══════════════════════════════════════════════════════════
#
# ⚠️ 全部用 `require_admin_session` 而不是 `require_admin`。
#
# 差别不是"更严一点"，而是**一条明确的安全边界**：管用户是
# "对权限本身的操作"，而机器令牌若能做这件事，它就能自我放大
# （创建一个新管理员、或改掉签发者的角色）。令牌是长期有效的，
# 泄漏一个就等于永久拿到用户管理权。
#
# 详见 `deps.require_admin_session` 的说明。


@router.get("/users", response_model=UserListOut, summary="用户列表")
def list_users(
    _identity: Annotated[Identity, Depends(require_admin_session)],
    db: Annotated[Session, Depends(get_db)],
) -> UserListOut:
    """全部账号。

    ⚠️ 用 `require_admin_session` 而不是 `require_read`：
    谁能看到"这个系统有哪些账号"本身就是权限信息 ——
    对一个会对目标发包的平台，账号列表是爆破的起点。
    """
    return UserListOut(users=[UserOut.model_validate(a) for a in service.list_accounts(db)])


@router.post(
    "/users",
    status_code=status.HTTP_201_CREATED,
    response_model=UserOut,
    summary="新增用户",
)
def create_user(
    payload: CreateUserRequest,
    request: Request,
    identity: Annotated[Identity, Depends(require_admin_session)],
    db: Annotated[Session, Depends(get_db)],
) -> UserOut:
    """管理员加人。

    ⚠️ 这里**不走登录限流**（`_login_limiter`）—— 限流是防在线爆破
    的，而加人需要已登录的管理员身份，攻击面完全不同。
    给它加限流只会让"一次加 10 个人"变成一件做不到的事。
    """
    account = service.create_user(
        db,
        username=payload.username,
        password=payload.password,
        role=payload.role,
    )
    audit.record(
        db,
        request=audit.from_http(request, identity),
        action=AuditAction.USER_CREATE,
        summary=f"新建用户「{account.username}」（角色 {account.role}）",
        target=account.id,
        detail={"username": account.username, "role": account.role},
    )
    return UserOut.model_validate(account)


@router.patch("/users/{userId}", response_model=UserOut, summary="改用户角色")
def update_user(
    userId: str,
    payload: UpdateUserRequest,
    request: Request,
    identity: Annotated[Identity, Depends(require_admin_session)],
    db: Annotated[Session, Depends(get_db)],
) -> UserOut:
    """改角色。

    ★ 不能把**最后一个管理员**降级 —— 那会让系统再也没有人能改设置，
    而恢复只能直接改库。见 `service.update_user_role`。

    ⚠️ 改完**立刻生效**：`resolve_identity` 每次请求都读账号，
    所以不需要等会话过期。这不是性能取舍，是安全要求 ——
    "我刚把他降成只读，他还在发起扫描"是一个真实的权限漏洞。
    """
    before = service.get_account_by_id(db, userId)
    before_role = before.role if before else None

    account = service.update_user_role(db, userId, payload.role)

    audit.record(
        db,
        request=audit.from_http(request, identity),
        action=AuditAction.USER_ROLE_CHANGE,
        summary=f"把「{account.username}」的角色从 {before_role} 改为 {account.role}",
        target=account.id,
        detail={"username": account.username, "from": before_role, "to": account.role},
    )
    return UserOut.model_validate(account)


@router.delete("/users/{userId}", status_code=status.HTTP_204_NO_CONTENT, summary="删除用户")
def delete_user(
    userId: str,
    request: Request,
    identity: Annotated[Identity, Depends(require_admin_session)],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    """删账号。**硬删除**。

    两条拒绝（都是"通过界面走进去的死胡同"）：
      · 不能删自己（删完立刻掉线，若同时是最后一个管理员就再也进不来）
      · 不能删最后一个管理员

    会话随账号级联删除（`AuthSession.user_id` 上是 `ON DELETE CASCADE`），
    所以被删的人不会留着一条还能用的会话。
    """
    account = service.get_account_by_id(db, userId)
    username = account.username if account else userId

    service.delete_user(db, userId, actor_id=identity.user_id or "")

    audit.record(
        db,
        request=audit.from_http(request, identity),
        action=AuditAction.USER_DELETE,
        summary=f"删除用户「{username}」",
        target=userId,
        detail={"username": username},
    )
