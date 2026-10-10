"""鉴权依赖：解析凭据、判定 scope、挡 CSRF。

## 两条通道（契约 §1.2：不要合并）

```
Authorization: Bearer svx_…   →  机器令牌（CI / 脚本）
Cookie: suvex_session=…       →  人在浏览器
```

两者**同时存在时以 Bearer 为准**。理由：显式头是调用方主动加的，
而 Cookie 是浏览器自动带的。一个 CI 脚本若碰巧带着某人的会话 Cookie
（比如调试时从浏览器复制过），应该按脚本自己的令牌判权限，
否则会出现「本地能跑、CI 里 403」这种只在特定机器上复现的问题。

## 管理员会话隐含 `admin`

契约 §1.2 原文：「**管理员会话隐含 `admin`** —— 人登录后不必再持有令牌。
令牌是给机器用的。」所以会话的 scope 直接给满。

机器令牌的 scope 是**累进**的（`admin ⊃ scan ⊃ read`），
判定逻辑在 `core.security.scope_allows`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.auth import service as auth_service
from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import ApiError, ErrorCode
from app.core.security import SCOPE_LABELS, scope_allows

# 会话给满三档（契约 §1.2：管理员会话隐含 admin）。
SESSION_SCOPES: frozenset[str] = frozenset({"read", "scan", "admin"})

# 这些方法不改变状态，不触发 CSRF 检查。
SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# 跨站 HTML 表单**只能**发出这三种 Content-Type（CORS 安全名单）。
# 契约 §1.2 要求「所有写操作要求 application/json」——
# 这条名单就是那个要求的可执行版本：出现这几种类型的写请求一定是表单，
# 而本 API 几乎没有任何端点收表单。
FORM_CONTENT_TYPES: frozenset[str] = frozenset(
    {"application/x-www-form-urlencoded", "multipart/form-data", "text/plain"}
)

# ★ 显式登记**确实需要** multipart 的写端点（任务 10 P6-d）。
#
# `(方法, 路径后缀)`。附件上传必须走 `multipart/form-data` ——
# 那是浏览器传文件的唯一方式，没有别的选择。
#
# ## 为什么不干脆全局放开 multipart
#
# 因为那道检查的价值恰恰在于"这个 API 不收表单"：
# 跨站表单是 CSRF 最经典的载体，而"本 API 根本不解析表单"
# 让整类攻击无处落脚。全局放开等于把这层纵深防御拆掉，
# 换来的只是少写一个集合。
#
# ## 为什么用"显式登记"这个形状
#
# 新增一个接受文件的端点时，**必须**在这里加一行，否则它的上传
# 会被 400 挡掉 —— 也就是说漏掉这件事会在**开发时立刻暴露**，
# 而不是悄悄削弱防线。
#
# 配套的自检测试在 `tests/test_findings_p6.py`：它遍历全部路由，
# 找出所有声明了 `UploadFile` 的端点，断言它们都在这个集合里。
# 那道检查才是这个机制真正的保险 —— 少了它，这个集合会腐烂成
# 一份"曾经正确"的名单。
MULTIPART_WRITE_ENDPOINTS: frozenset[tuple[str, str]] = frozenset(
    {
        # ⚠️ **不带 `/api/v1` 前缀** —— 前缀是 `include_router(prefix=...)`
        # 在挂载时加的，而 `route.path` 是**子路由自己的模板**。
        # 实测：`POST /api/v1/findings/<id>/attachments` 匹配到的
        # `route.path` 是 `/findings/{findingId}/attachments`。
        #
        # 我第一版按"完整对外路径"写，结果一个都没匹配上 ——
        # 症状是所有上传端点突然都 400，而错误消息只说
        # "写操作必须使用 Content-Type: application/json"。
        # 漏洞的处置证据（任务 10 P6-d）
        ("POST", "/findings/{findingId}/attachments"),
        # 题目附件：按扫描上传（任务 12）
        ("POST", "/scans/{scanId}/attachments"),
        ("POST", "/scans/attachments/staging"),
        # 题目附件：建扫描**之前**的暂存（任务 12）
    }
)


def _matched_route_path(request: Request) -> str | None:
    """这次请求**匹配到的路由模板**（形如 `/api/v1/scans/{scanId}/attachments`）。

    ⚠️ 不能用 `request.url.path`：那是**带真实值**的路径
    （`/api/v1/scans/scan_01J…/attachments`），与登记表里的模板
    永远不相等。

    ⚠️ 也**不是**对外那条含 `/api/v1` 的路径 —— 前缀是
    `include_router(prefix=...)` 挂载时加的，`route.path` 里没有它。
    登记表因此也用**不带前缀**的模板。

    取不到时返回 None（比如 404 之前就走到这里的情形），
    调用方按"未登记"处理 —— 那会拒绝，是更安全的默认。
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return str(path) if path else None


def _allows_multipart(request: Request) -> bool:
    """这个请求打到的端点是否**显式登记**为接受 multipart。

    ## ★ 从"后缀匹配"改成"精确匹配路由模板"（任务 12）

    第一版用的是后缀匹配（`path.endswith("/attachments")`），
    理由是"路径里带变量的端点不好精确比对"。而那个便利有代价 ——
    我当时在注释里写下过：

    > 加一个以 `/attachments` 结尾的端点会**自动获得** multipart 放行，
    > 而不会在开发时被提醒。

    结果它**不是**以那个方式出问题的，而是反过来：
    任务 12 加的暂存端点是 `POST /scans/attachments/staging`，
    **不以 `/attachments` 结尾**，于是它**没有**被放行，
    上传直接被 400 挡掉（"写操作必须使用 Content-Type: application/json"）。
    而那条错误消息与"忘了登记"看起来毫无关系 —— 正是我预判的
    那类难查症状，只是方向相反。

    所以现在改成精确匹配：登记表里写完整的路由模板，逐字比对。
    好处是**双向都安全** —— 漏登记会被自检测试抓住，
    而多登记不可能（没有"刚好同后缀"这种东西了）。

    ⚠️ 只看 `multipart/form-data`，另外两种表单类型**一样被拒**：
    附件端点也该只收 multipart（urlencoded 传不了二进制文件）。
    """
    template = _matched_route_path(request)
    if template is None:
        return False
    return (request.method, template) in MULTIPART_WRITE_ENDPOINTS


@dataclass(frozen=True, slots=True)
class Identity:
    """当前请求的调用方。

    刻意做成**不可变**的：它会在一次请求里被多处读到（鉴权、审计留痕、
    响应里的 `authorized_by`），任何一处改了它都会让别处看到不一致的值。
    """

    kind: Literal["session", "token"]
    # 会话是 `session:<id>`；令牌是它的 label（"CI 流水线"）。用于日志与文案。
    label: str
    scopes: frozenset[str]
    user_id: str | None = None
    session_id: str | None = None
    token_id: str | None = None

    def can(self, scope: str) -> bool:
        return scope_allows(sorted(self.scopes), scope)

    def describe(self) -> str:
        return "管理员会话" if self.kind == "session" else f"令牌「{self.label}」"


def resolve_identity(request: Request, db: Session) -> Identity | None:
    """从请求里解析身份。解析不出就返回 None（**不抛异常**）。

    「可选身份」是必需的：`GET /auth/state` 在登录前就要能调用，
    但它还要回答 `authenticated`。如果这里直接抛 401，那个端点就没法用了。
    """
    header = request.headers.get("authorization") or ""
    if header[:7].lower() == "bearer ":
        raw = header[7:].strip()
        token = auth_service.resolve_token(db, raw)
        if token is None:
            return None
        auth_service.mark_token_used(db, token)
        return Identity(
            kind="token",
            label=token.label,
            scopes=frozenset(token.scopes),
            token_id=token.id,
        )

    cookie_name = get_settings().session_cookie_name
    raw_session = request.cookies.get(cookie_name)
    if not raw_session:
        return None
    session = auth_service.resolve_session(db, raw_session)
    if session is None:
        return None
    auth_service.touch_session(db, session, ttl_hours=get_settings().session_ttl_hours)
    return Identity(
        kind="session",
        label=f"session:{session.id}",
        scopes=SESSION_SCOPES,
        user_id=session.user_id,
        session_id=session.id,
    )


def _allowed_origins(request: Request) -> set[str]:
    """允许的源 = 配置里的 CORS 源 + **本请求自己的源**。

    加上自身的源是必需的：生产环境常把前端与 API 放在同一个域名下，
    此时 `Origin` 就等于 API 自己的地址，而它不会出现在 `SUPEX_CORS_ORIGINS`
    （那是给开发态跨源用的）。同源请求按定义不是 CSRF，必须放行。
    """
    origins = {o.rstrip("/") for o in get_settings().cors_origin_list}
    own = f"{request.url.scheme}://{request.url.netloc}"
    origins.add(own)
    # 反代场景：外部看到的是 https://host，而应用看到的 scheme 可能是 http。
    forwarded = request.headers.get("x-forwarded-host")
    if forwarded:
        proto = request.headers.get("x-forwarded-proto", "https")
        origins.add(f"{proto}://{forwarded}".rstrip("/"))
    return origins


def enforce_csrf(request: Request, identity: Identity) -> None:
    """Cookie 通道的写操作必须来自允许的源，且不能是表单编码。

    三道防线的分工（契约 §1.2 的表）：

    | 防线                          | 挡什么                                     |
    | ----------------------------- | ------------------------------------------ |
    | `SameSite=Strict`（Cookie 属性）| 跨站请求根本带不上 Cookie —— **第一道，也是主要那道** |
    | 本函数                        | 同站点但不同源的场景、以及 `SameSite` 在旧浏览器上被忽略的情况 |
    | `HttpOnly`                    | XSS 偷不走会话（它管的是读，不是发）        |

    为什么还要第二道：`SameSite` 是浏览器行为，而「浏览器行为」有历史包袱 ——
    它对新注册的域名有默认值差异、对 `localhost` 有特殊放宽。
    服务端自己判一次 `Origin` 的成本是几微秒，而这把安全边界从
    「依赖浏览器实现正确」变成了「依赖我们自己的一行判断」。

    ⚠️ `Origin` 与 `Referer` **都没有**的写请求放行，这不是漏洞：
    浏览器在跨源写请求上**必定**带 `Origin`（这是 Fetch 规范强制的）。
    两者都缺只可能是非浏览器客户端（curl、脚本），而它不受 CSRF 影响 ——
    CSRF 的定义就是"借用户的浏览器"。
    """
    if identity.kind != "session" or request.method in SAFE_METHODS:
        return

    allowed = _allowed_origins(request)

    origin = request.headers.get("origin")
    if origin:
        if origin.rstrip("/") not in allowed:
            raise ApiError(
                403,
                ErrorCode.CROSS_ORIGIN_BLOCKED,
                "该请求来自未授权的来源，已被拦截。若这是你自己部署的前端，"
                "请把它的地址加进 SUPEX_CORS_ORIGINS。",
            )
    else:
        referer = request.headers.get("referer")
        if referer:
            parts = urlsplit(referer)
            ref_origin = f"{parts.scheme}://{parts.netloc}"
            if ref_origin and ref_origin not in allowed:
                raise ApiError(
                    403,
                    ErrorCode.CROSS_ORIGIN_BLOCKED,
                    "该请求来自未授权的来源，已被拦截。",
                )

    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type in FORM_CONTENT_TYPES:
        # ★ 例外：显式登记接受 multipart 的端点（附件上传，任务 10 P6-d）。
        #
        # ⚠️ Origin 检查**在这之前**已经执行过了 —— 放行的只是
        # "Content-Type 是不是表单"这一道，跨源那道闸对附件上传照旧生效。
        # 这个顺序很关键：把例外写成"整个 enforce_csrf 跳过"就会
        # 连跨源保护一起丢掉。
        if content_type == "multipart/form-data" and _allows_multipart(request):
            return
        raise ApiError(
            400,
            ErrorCode.VALIDATION_FAILED,
            "写操作必须使用 Content-Type: application/json。",
        )


def require_identity(
    request: Request, db: Annotated[Session, Depends(get_db)]
) -> Identity:
    """必须已登录。解析不出就 401。"""
    identity = resolve_identity(request, db)
    if identity is None:
        raise ApiError.unauthenticated()
    enforce_csrf(request, identity)
    return identity


def _require_scope(identity: Identity, scope: str) -> Identity:
    if not identity.can(scope):
        raise ApiError.insufficient_scope(SCOPE_LABELS.get(scope, scope))
    return identity


def require_read(identity: Annotated[Identity, Depends(require_identity)]) -> Identity:
    """只读：看扫描、漏洞、报告、设置。"""
    return _require_scope(identity, "read")


def require_scan(identity: Annotated[Identity, Depends(require_identity)]) -> Identity:
    """发起/终止扫描 —— **会对真实目标发包**。"""
    return _require_scope(identity, "scan")


def require_admin(identity: Annotated[Identity, Depends(require_identity)]) -> Identity:
    """改设置、管 provider、管令牌、改密码。"""
    return _require_scope(identity, "admin")


def client_ip(request: Request) -> str:
    """调用方 IP。

    ⚠️ **刻意不读 `X-Forwarded-For`**。那个头可以被客户端随意伪造，
    而它在这里有两个用途：登录限流的 key（伪造 = 免费绕过限流）与
    扫描授权留痕（伪造 = 留痕失去意义）。两者都不能建立在可伪造的输入上。

    反代场景的正确做法是在 ASGI 服务器层配置可信代理
    （`uvicorn --proxy-headers --forwarded-allow-ips=<反代地址>`），
    由 uvicorn 判断来源可信后改写 `request.client`。这样信任边界在
    服务器层是显式的，而不是在业务代码里"读一个头"。
    """
    return request.client.host if request.client else "unknown"
