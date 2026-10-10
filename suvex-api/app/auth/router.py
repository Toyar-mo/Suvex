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

from app.auth import service
from app.auth.deps import Identity, client_ip, require_read, resolve_identity
from app.auth.schemas import AuthState, PasswordRequest, UserEnvelope, UserOut
from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import ApiError, ErrorCode
from app.core.ratelimit import Limiter

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
    summary="一次性初始化：设置管理员口令",
)
def setup(
    payload: PasswordRequest,
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
) -> UserEnvelope:
    """首次启动设置管理员口令，并**自动登录**。

    ★ 这个端点在「已初始化」后**彻底关闭**。否则它就是一个
    「任意人可重设管理员口令」的后门 —— 而本平台的危害不是读到数据，
    是替攻击者发包（契约 §0.3）。
    """
    _login_limiter().check(client_ip(request))

    if service.has_any_account(db):
        raise ApiError.conflict(
            ErrorCode.SETUP_ALREADY_DONE,
            "系统已完成初始化，无法再次设置口令。请直接登录。",
        )

    account = service.create_account(db, payload.password)
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
    """单管理员，所以只需要口令（没有用户名）。

    失败统一返回 `401 UNAUTHENTICATED`，`message` 只说「口令不正确」——
    不做「账号不存在」与「口令错误」的区分，那是账号枚举漏洞。

    ⚠️ **登录成功不清空限流计数**。清空的话，攻击者只要偶尔蒙对一次
    （或者从别处拿到一次有效登录）就能重置配额。而正常用户
    每分钟 5 次的上限完全够用。
    """
    _login_limiter().check(client_ip(request))

    account = service.authenticate(db, payload.password)
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
    """返回 `{ id, createdAt }`（契约 §3.1）。

    注意这里**不返回口令哈希、不返回会话数量、不返回最后登录时间**。
    单管理员系统里这些字段没有任何消费方，而每一个都是多余的信息面。
    """
    account = service.get_account(db)
    if account is None:  # pragma: no cover - require_read 已经保证了账号存在
        raise ApiError.unauthenticated()
    return UserOut.model_validate(account)
