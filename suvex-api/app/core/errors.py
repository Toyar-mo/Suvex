"""统一错误响应（契约 §1.3）。

形状只有一种，任何非 2xx 都是它：

```json
{ "error": { "code": "...", "message": "...", "details": { "字段": "问题" } } }
```

三条硬要求是从前端代码反推出来的，不是审美：

1. **`message` 必须是可直接展示的中文完整句**。前端 `SaveBar` 会把 error
   拼进「保存失败：<error>」，所以它不能是 `invalid_target` 这种码。
2. **一次返回全部字段级错误**（`details`）。前端表单是逐字段就地报错的
   （`Field.tsx` 的 `error` prop），"改一个报一个"会让用户来回提交五次。
3. **不泄露内部信息**：不返回堆栈、不返回 SQL、不回声用户输入。
   `changePassword` 的文案已经定了调子 —— 只写「当前密码不正确」，
   不透露任何额外信息。

`details` 的 key 用**前端字段名**（camelCase，如 `targetKind`），
不是 pydantic 的 snake_case 路径 —— 前端拿它直接查字段，不做转换。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("suvex.errors")


class ErrorCode:
    """契约 §1.3 的错误码表。字符串常量而不是 Enum ——

    它们要进 JSON，Enum 每次都得 `.value`，而且 `json.dumps` 对 str 子类
    更省事。加新码时必须同步改 `docs/api-contract.md` 的错误码表。
    """

    VALIDATION_FAILED = "VALIDATION_FAILED"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    INSUFFICIENT_SCOPE = "INSUFFICIENT_SCOPE"
    CROSS_ORIGIN_BLOCKED = "CROSS_ORIGIN_BLOCKED"
    TARGET_NOT_AUTHORIZED = "TARGET_NOT_AUTHORIZED"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    SCAN_NOT_ABORTABLE = "SCAN_NOT_ABORTABLE"
    # 续跑不可用（任务 16）。单独一个码，因为**补救动作明确**：
    # 改用「重跑」。混进通用的 409 只会显示一句"当前状态下无法执行"。
    SCAN_NOT_RESUMABLE = "SCAN_NOT_RESUMABLE"
    # 题解取不到（任务 19）。三种成因（不是夺旗任务 / 还没结束 / 报告文件已不在）
    # 走同一个码，因为**补救动作都不是在界面上能做的**：用户要么换一种判据重跑，
    # 要么等它跑完。真正要区分的是"哪一句说明" —— 而那由 message 承担，
    # 它本来就是写给用户看的中文原话。
    WRITEUP_UNAVAILABLE = "WRITEUP_UNAVAILABLE"
    # ★ v0.27：这条扫描是 CTF2 借来挂事件流的壳，不是一次真扫描。
    #
    # 为什么值得单独一个码（而不是复用 `SCAN_NOT_RESUMABLE` 那类）：
    # 用户点了「重跑」之后**必须被告知去点哪里** —— 这个码对应的 message
    # 会写"请回 CTF2 题目卡片上点解题"。混进通用 409 只会显示一句
    # "当前状态下无法执行"，而用户刚刚亲眼看到按钮可用。
    SCAN_IS_CTF2_SHELL = "SCAN_IS_CTF2_SHELL"
    PROVIDER_KEY_CHANGED = "PROVIDER_KEY_CHANGED"
    PROVIDER_UNREACHABLE = "PROVIDER_UNREACHABLE"
    # 删除 provider 时两种**补救动作不同**的拒绝（任务 2 补，契约 §3.2 / §6）：
    # 前者要先去设另一个为默认，后者要先去终止那些扫描。
    # 给同一个码的话前端只能把两句话都显示出来，而其中一句是错的。
    PROVIDER_IS_DEFAULT = "PROVIDER_IS_DEFAULT"
    PROVIDER_IN_USE = "PROVIDER_IN_USE"
    # 密文解不开（SUPEX_SECRET_KEY 换了或丢了）。
    # 单独一个码是为了让前端能给出"重新填写凭据"这个明确指引，
    # 而不是把它混进通用的 500 里。
    PROVIDER_KEY_UNREADABLE = "PROVIDER_KEY_UNREADABLE"
    RATE_LIMITED = "RATE_LIMITED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    SETUP_ALREADY_DONE = "SETUP_ALREADY_DONE"
    # ── 多用户与角色（v0.24）──────────────────────────────
    #
    # ★ 这三个描述的是"**通过界面走进去的死胡同**"，而不是"参数写错了"。
    #   分开给码的理由：前端要据此给出**不同的补救动作**
    #   （"先指定另一个管理员" vs "不能删自己"），而拿中文消息去
    #   `includes('管理员')` 做判断会在文案一改就静默失效。
    LAST_ADMIN = "LAST_ADMIN"
    CANNOT_DELETE_SELF = "CANNOT_DELETE_SELF"
    USERNAME_TAKEN = "USERNAME_TAKEN"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"


# 没有专门错误码的 HTTP 状态 → 兜底码。加了这个映射才不会把
# 「路由打错」报成 INTERNAL_ERROR（那会让用户以为服务坏了）。
_STATUS_FALLBACK: dict[int, tuple[str, str]] = {
    400: (ErrorCode.VALIDATION_FAILED, "请求内容不合法。"),
    401: (ErrorCode.UNAUTHENTICATED, "请先登录。"),
    403: (ErrorCode.INSUFFICIENT_SCOPE, "当前凭据权限不足。"),
    404: (ErrorCode.NOT_FOUND, "请求的资源不存在。"),
    405: (ErrorCode.METHOD_NOT_ALLOWED, "该端点不支持这个 HTTP 方法。"),
    409: (ErrorCode.VALIDATION_FAILED, "当前状态下无法执行该操作。"),
    429: (ErrorCode.RATE_LIMITED, "请求过于频繁，请稍后重试。"),
    501: (ErrorCode.NOT_IMPLEMENTED, "该功能尚未实现。"),
}

# 字段级校验错误的中文说法。
# pydantic 的原生消息是英文（"Field required"），直接透出去就违反了
# 「message 必须是可直接展示的中文完整句」。这里按错误类型映射，
# 不做逐条翻译（那要维护所有类型的文案），认不出的类型退回一句通用的。
_PYDANTIC_MESSAGES: dict[str, str] = {
    "missing": "此项必填",
    "string_too_short": "长度不足",
    "string_too_long": "长度超出上限",
    "string_type": "需要是文本",
    "int_parsing": "需要是整数",
    "int_type": "需要是整数",
    "float_parsing": "需要是数字",
    "float_type": "需要是数字",
    "bool_parsing": "需要是布尔值",
    "bool_type": "需要是布尔值",
    "datetime_parsing": "时间格式不正确，应为 ISO 8601（如 2026-09-23T15:30:42.123Z）",
    "datetime_from_date_parsing": "时间格式不正确，应为 ISO 8601",
    "literal_error": "取值不在允许范围内",
    "enum": "取值不在允许范围内",
    "list_type": "需要是数组",
    "dict_type": "需要是对象",
    "greater_than": "数值小于允许的下限",
    "greater_than_equal": "数值小于允许的下限",
    "less_than": "数值超出允许的上限",
    "less_than_equal": "数值超出允许的上限",
    "value_error": "取值不合法",
    "json_invalid": "不是合法的 JSON",
    "extra_forbidden": "不认识的字段",
}


class ApiError(Exception):
    """业务错误。`raise ApiError.not_found("扫描")` 这样用。"""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        self.headers = headers

    def to_payload(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        # details 只在有内容时出现。始终带一个 null 的 details 会让前端
        # 多一层「有没有字段错误」的判断，而它本来只需要看 key 在不在。
        if self.details:
            error["details"] = self.details
        return {"error": error}

    # ── 构造快捷方式：让业务代码里不出现魔法数字 ──────────

    @classmethod
    def validation(cls, message: str, details: dict[str, Any] | None = None) -> ApiError:
        return cls(400, ErrorCode.VALIDATION_FAILED, message, details)

    @classmethod
    def unauthenticated(cls, message: str = "请先登录。") -> ApiError:
        return cls(401, ErrorCode.UNAUTHENTICATED, message)

    @classmethod
    def insufficient_scope(cls, needed: str, message: str | None = None) -> ApiError:
        return cls(
            403,
            ErrorCode.INSUFFICIENT_SCOPE,
            message or f"当前凭据权限不足：此操作需要「{needed}」权限。",
        )

    @classmethod
    def target_not_authorized(cls, message: str = "该目标尚未确认授权，无法发起扫描。") -> ApiError:
        return cls(403, ErrorCode.TARGET_NOT_AUTHORIZED, message)

    @classmethod
    def not_found(cls, what: str) -> ApiError:
        return cls(404, ErrorCode.NOT_FOUND, f"{what}不存在，可能已被删除。")

    @classmethod
    def conflict(cls, code: str, message: str) -> ApiError:
        return cls(409, code, message)

    @classmethod
    def rate_limited(cls, retry_after_seconds: int, message: str | None = None) -> ApiError:
        return cls(
            429,
            ErrorCode.RATE_LIMITED,
            message or f"尝试过于频繁，请在 {retry_after_seconds} 秒后重试。",
            headers={"Retry-After": str(retry_after_seconds)},
        )

    @classmethod
    def not_implemented(cls, planned_task: str, what: str) -> ApiError:
        """分期交付时未实现的端点。

        用 501 而不是 404：404 的语义是「这个资源不存在」，
        而真实情况是「端点还没做」—— 两者对调用方的含义完全不同
        （前者该换 id，后者该等版本）。
        """
        return cls(
            501,
            ErrorCode.NOT_IMPLEMENTED,
            f"{what}尚未实现（计划在后端任务 {planned_task} 中交付）。",
        )


def _field_name(loc: tuple[Any, ...]) -> str:
    """pydantic 的 `loc` → 前端字段名。

    `("body", "authorization", "statement")` → `authorization.statement`。
    去掉 `body`/`query`/`path`/`header` 这几个位置前缀 ——
    它们对前端没有意义，用户看到的字段就是他自己填的那个框。
    """
    parts = [str(p) for p in loc if p not in ("body", "query", "path", "header", "cookie")]
    return ".".join(parts) if parts else "请求"


def translate_validation_error(exc: RequestValidationError) -> dict[str, str]:
    """把 pydantic 的错误列表翻成「字段 → 中文说明」。

    同一个字段有多个错误时保留**第一条**：前端 `Field` 只渲染一个 error，
    给两条会让它显示其中一条、而另一条永远看不到 —— 不如只留最先触发的。
    """
    details: dict[str, str] = {}
    for err in exc.errors():
        field = _field_name(tuple(err.get("loc", ())))
        if field in details:
            continue
        err_type = str(err.get("type", ""))
        details[field] = _PYDANTIC_MESSAGES.get(err_type, "取值不合法")
    return details


def _envelope(
    status_code: int, code: str, message: str, details: dict[str, Any] | None = None, headers: dict[str, str] | None = None
) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    return JSONResponse(status_code=status_code, content={"error": error}, headers=headers)


def register_exception_handlers(app: FastAPI) -> None:
    """挂上四类处理器。顺序无所谓，FastAPI 按异常类型精确匹配。"""

    @app.exception_handler(ApiError)
    async def _handle_api_error(_request, exc: ApiError) -> JSONResponse:
        return _envelope(exc.status_code, exc.code, exc.message, exc.details, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _handle_request_validation(_request, exc: RequestValidationError) -> JSONResponse:
        # ⚠️ 契约要求 400，而 FastAPI 默认给 422。
        # 前端是按契约写的，返回 422 会让它的校验错误分支整个不触发。
        details = translate_validation_error(exc)
        message = "提交的内容有误，请检查标出的字段。"
        if details:
            message = "提交的内容有误：" + "；".join(f"{k} {v}" for k, v in details.items()) + "。"
        # `details` 必须传下去 —— 前端 `Field.tsx` 的 `error` prop 读的就是它。
        # 只把内容拼进 message 的话，表单上一个字段都不会标红，
        # 而用户看到的是一句笼统的"提交的内容有误"，不知道改哪里。
        return _envelope(400, ErrorCode.VALIDATION_FAILED, message, details)

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(_request, exc: StarletteHTTPException) -> JSONResponse:
        code, default_message = _STATUS_FALLBACK.get(
            exc.status_code, (ErrorCode.INTERNAL_ERROR, "请求处理失败。")
        )
        # `detail` 是我们自己写的场合（比如路由里 raise HTTPException(404, "…")）
        # 就用它；否则用兜底文案。不把 Starlette 的英文 detail 透出去。
        raw = exc.detail
        message = raw if isinstance(raw, str) and _looks_chinese(raw) else default_message
        return _envelope(exc.status_code, code, message, headers=getattr(exc, "headers", None))

    @app.exception_handler(Exception)
    async def _handle_unexpected(request, exc: Exception) -> JSONResponse:
        # 堆栈只进服务端日志。响应里连异常类名都不给 ——
        # 类名本身就是内部结构信息。
        logger.exception("未捕获的异常：%s %s", request.method, request.url.path, exc_info=exc)
        return _envelope(
            500,
            ErrorCode.INTERNAL_ERROR,
            "服务器内部错误，请稍后重试。若持续出现，请查看服务端日志。",
        )


def _looks_chinese(text: str) -> bool:
    """判断串里有没有中日韩字符 —— 用来区分「我们写的中文文案」和
    「框架自带的英文 detail」。这是启发式，但比维护一张状态码→文案表
    更不容易漂移（表会忘记更新，而这个判断永远对自定义文案生效）。
    """
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)
