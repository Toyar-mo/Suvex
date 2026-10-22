"""平台的接入与生命周期（v0.25）。

## 一次"自动对接"的完整时序

    ① POST /ctf2/platforms/draft      创建 status='building' 的行，起后台线程，立刻返回
    ② 后台：probe 实测 → 大模型生成 → 试跑 → （失败就带着错误重试）
    ③ GET  /ctf2/platforms/{id}       前端轮询，直到 status 不再是 building
    ④ POST /ctf2/platforms/{id}/activate  验证凭据 → 存进账号表 → 指向这个平台

## ★ 为什么生成一定要放后台线程

一轮生成最多三次"探测 + 大模型 + 试跑"：探测约 20 秒（几十个候选端点、
还得限速），大模型每轮 10~40 秒，试跑几秒。**最坏两分钟。**

挂在一次 HTTP 请求上的后果不是"慢"，而是**前端超时之后后台还在跑** ——
用户看到"失败"，而配置其实马上就好了。那种不一致最难查：重试一次，
第二次反而"成功"，于是看起来像个偶发 bug。

所以状态落在 `ctf2_platforms.status` 上，前端轮询它。

## ★ 凭据在这里只经过内存

生成阶段用到的 key 由请求带进来，后台线程用完即弃，**不落库**
（表上也刻意没有这一列，见 `models.Ctf2Platform` 的 docstring）。
它只在第 ④ 步、用户明确点"启用"之后，才被加密写进账号表。

多一次点击换的是"凭据只有一个家"。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import asdict
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import hostguard
from app.core.db import session_scope
from app.core.errors import ApiError
from app.core.ids import new_id
from app.kernel.adapters.builder import ChatFn, build_spec, verify_spec
from app.kernel.adapters.generic import GenericDriver
from app.kernel.adapters.spec import PlatformSpec, SpecError
from app.kernel.models import Ctf2Account, Ctf2Platform

logger = logging.getLogger("suvex.ctf2")

#: 内置驱动的标识。`platform_id IS NULL` 时用它。
BUILTIN_SLUG = "ctf2"
BUILTIN_DISPLAY = "CTF2（内置驱动）"

#: 同一个平台不允许有两轮生成同时在跑（后一轮会把前一轮的产物覆盖掉，
#: 而用户看到的是"结果随机"）。
_BUILD_GUARD = threading.Lock()
_BUILDING: set[str] = set()

#: 一轮生成的轮数上限。三次之后还不行，多半是文档与真实接口差得太远，
#: 再试只是烧额度。
MAX_BUILD_ATTEMPTS = 3


def _run(coro: Any) -> Any:
    """在同步上下文里跑协程。

    ⚠️ 与 `service._run` 同一个前提：调用方是 FastAPI 的**同步**端点
    （跑在线程池里），没有正在运行的事件循环。这里不 import 它是因为
    那个名字带下划线，而跨模块 import 私有函数会把"谁可以用它"搞乱。
    """
    return asyncio.run(coro)


# ══════════════════════════════════════════════════════════
# 读
# ══════════════════════════════════════════════════════════


def list_platforms(db: Session) -> list[Ctf2Platform]:
    return list(
        db.execute(select(Ctf2Platform).order_by(Ctf2Platform.created_at.desc()))
        .scalars()
        .all()
    )


def get_platform(db: Session, platform_id: str) -> Ctf2Platform | None:
    if not platform_id:
        return None
    return db.get(Ctf2Platform, platform_id)


def get_platform_or_404(db: Session, platform_id: str) -> Ctf2Platform:
    row = get_platform(db, platform_id)
    if row is None:
        raise ApiError(404, "CTF2_PLATFORM_NOT_FOUND", "找不到这个平台配置。")
    return row


def spec_of(platform: Ctf2Platform | None) -> PlatformSpec | None:
    """把库里那列 JSON 还原成配置对象。

    ⚠️ 解析失败**返回 None 而不是抛**：一份坏掉的配置不该让整个平台列表
    接口 500 —— 那种"点开设置页就白屏"的故障，用户没有任何自救手段。
    调用方（同步/提交）看到 None 会退化成"这个平台用不了"，那是可解释的。
    """
    if platform is None or not platform.spec:
        return None
    try:
        return PlatformSpec.from_dict(platform.spec)
    except SpecError as exc:
        logger.warning("平台 %s 的配置解析失败：%s", platform.slug, exc)
        return None


# ══════════════════════════════════════════════════════════
# 生成
# ══════════════════════════════════════════════════════════


#: 明确拒绝的目标：云厂商的**实例元数据服务**。
#:
#: ★ 这是唯一一类我们要主动拒绝的地址。
#:
#: **不**做内网限制，是刻意的：比赛平台很可能就部署在内网
#: （`192.168.x.x` / `10.x.x.x`），而"拒绝内网"会把最可能的使用场景
#: 直接挡在门外 —— 那等于这个功能白做。
#:
#: 元数据服务不一样：一次 GET 就能换到云主机的临时凭据，而"让服务端
#: 替你打一个 URL"正是这个功能的形状。所以那一个必须堵死。
#:
#: ⚠️ 判据**在 `app/core/hostguard.py`** —— 因为 `transport.py` /
#: `probe.py` 也要用它（跟随跳转时逐跳校验），而它们不能反向 import
#: 这个模块（`platforms → builder → generic → transport` 已经成链，
#: 反过来就成环）；`app/providers/llm.py` 与 CTF2 互不认识，也不该
#: 互相 import。这里保留别名是为了不破坏既有 import 点。
BLOCKED_HOSTS = hostguard.BLOCKED_HOSTS


def assert_safe_base_url(raw: str) -> str:
    """校验用户填的平台地址。返回原值（去掉首尾空白）。

    ⚠️ 两层：协议必须是 http(s)（`PlatformSpec` 也会再校验一次），
    主机不能是元数据服务（见 `hostguard.is_blocked_host`）。

    ★ 主机那一层**不是字符串比对**：`169.254.169.254` 有 `2852039166`、
    `0xA9FEA9FE`、`[::ffff:169.254.169.254]` 这些 `getaddrinfo` 全认的
    等价写法，只比字符串会一个都不命中 —— 见 `hostguard` 的说明。

    ★★ R-4：`.host` 的访问**必须在 `try` 里**。`httpx.URL(...)` 的构造是
    惰性的、不抛 —— 畸形 A-label（`http://xn--.invalid/x`）要到第一次访问
    `.host` 时才抛 `idna.IDNAError`（`httpx/_urls.py:191`）。它既不是
    `httpx.InvalidURL` 也不是 `HTTPError` 的子类，会直接穿过本函数 →
    `create_building` → `POST /ctf2/platforms/draft` →
    全局 `@app.exception_handler(Exception)` → **500**。
    实测（Kali）：直调本函数传 5 条 A-label 全部逃逸。
    """
    url = (raw or "").strip()
    try:
        parsed = httpx.URL(url)
        # ★★ 先访问一次 `.host`：IDNA 校验发生在这里，不是构造时。
        #    下面 `parsed.host` 的几处访问都在 try 之外，拦不住。
        host = parsed.host
    except Exception as exc:  # noqa: BLE001 - httpx 对畸形 URL 抛的异常类型不固定
        raise ApiError.validation("平台地址不是一个合法的 URL。", {"baseUrl": str(exc)}) from exc
    if parsed.scheme not in ("http", "https"):
        raise ApiError.validation(
            "平台地址必须以 http:// 或 https:// 开头。", {"baseUrl": f"协议是 {parsed.scheme!r}"}
        )
    if not host:
        raise ApiError.validation("平台地址缺少主机名。", {"baseUrl": "缺少主机名"})
    if hostguard.is_blocked_host(host):
        raise ApiError.validation(
            "这个地址是云厂商的实例元数据服务，不能作为平台地址。",
            {"baseUrl": "该地址被拒绝"},
        )
    return url


def _unique_slug(db: Session, hint: str) -> str:
    base = (hint or "platform")[:48] or "platform"
    candidate = base
    index = 2
    while db.execute(
        select(Ctf2Platform.id).where(Ctf2Platform.slug == candidate)
    ).scalars().first():
        candidate = f"{base}-{index}"
        index += 1
    return candidate


def create_building(db: Session, *, base_url: str) -> Ctf2Platform:
    """建一行"生成中"的记录。

    ★ 先落库再起线程（而不是反过来）：线程会立刻开始写状态，如果行还不
    存在，它就只能丢掉结果 —— 而用户看到的是"生成完了但什么都没有"。
    """
    base_url = assert_safe_base_url(base_url)
    row = Ctf2Platform(
        id=new_id("ctf2plat"),
        slug=_unique_slug(db, _slug_hint(base_url)),
        display_name="正在识别…",
        base_url=base_url,
        driver="generic",
        source="generated",
        status="building",
        attempts=0,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _slug_hint(base_url: str) -> str:
    host = (base_url or "").split("//", 1)[-1].split("/", 1)[0]
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in host).strip("-")
    return cleaned or "platform"


def start_build(
    platform_id: str,
    *,
    base_url: str,
    access_key: str,
    document: str,
) -> bool:
    """起一个后台线程做生成。**立刻返回**，不等结果。

    返回 `False` 表示这个平台已经有一轮在跑了（调用方据此告诉用户
    "正在生成中"，而不是让他以为点了没反应）。
    """
    with _BUILD_GUARD:
        if platform_id in _BUILDING:
            return False
        _BUILDING.add(platform_id)
    thread = threading.Thread(
        target=_run_build,
        args=(platform_id, base_url, access_key, document),
        name=f"suvex-ctf2-platform-{platform_id[:8]}",
        daemon=True,
    )
    thread.start()
    return True


def _run_build(platform_id: str, base_url: str, access_key: str, document: str) -> None:
    """后台体。**绝不抛出去** —— 线程里的未捕获异常会让状态永远停在
    `building`，而用户会一直转圈等一个已经死掉的生成。"""
    try:
        chat = _build_chat()
        result = _run(
            build_spec(
                base_url=base_url,
                access_key=access_key,
                chat=chat,
                document=document,
                max_attempts=MAX_BUILD_ATTEMPTS,
            )
        )
    except Exception as exc:  # noqa: BLE001 - 见 docstring
        logger.exception("平台配置生成失败（platform=%s）", platform_id)
        _finish(platform_id, status="failed", error=f"{type(exc).__name__}: {exc}")
        return
    finally:
        with _BUILD_GUARD:
            _BUILDING.discard(platform_id)

    if result.ok and result.spec is not None:
        logger.info(
            "平台配置生成成功（platform=%s，%d 轮，%d 道题）",
            platform_id,
            len(result.attempts),
            result.challenge_count,
        )
        _finish(
            platform_id,
            status="verified",
            spec=asdict(result.spec),
            raw=result.last_raw,
            attempts=len(result.attempts),
        )
        return

    # ★ 失败时**仍然把最后那份合法配置存下来**：它离成功最近，用户可以
    #   在界面上看到它、手工改一处再试。整份丢掉等于把模型做出来的工作
    #   全扔了，而"差一点"和"完全不行"对用户是两件事。
    logger.info("平台配置生成未通过（platform=%s）：%s", platform_id, result.error)
    _finish(
        platform_id,
        status="failed",
        spec=asdict(result.spec) if result.spec is not None else None,
        raw=result.last_raw,
        attempts=len(result.attempts),
        error=result.error or "生成失败",
    )


def _finish(
    platform_id: str,
    *,
    status: str,
    spec: dict[str, Any] | None = None,
    error: str = "",
    raw: str = "",
    attempts: int = 0,
) -> None:
    """把生成结果写回库。**失败也要把原始输出留下** —— 排查"它为什么
    总也生成不对"时，唯一能看的证据就是那段原文。"""
    with session_scope() as db:
        row = db.get(Ctf2Platform, platform_id)
        if row is None:  # pragma: no cover - 只会在生成期间被删掉时发生
            logger.warning("平台 %s 在生成期间被删除，结果丢弃", platform_id)
            return
        if spec is not None:
            row.spec = spec
            row.base_url = str(spec.get("base_url") or row.base_url)
            row.display_name = str(spec.get("display_name") or row.display_name)
        row.status = status
        row.last_error = error or None
        row.last_raw = raw or None
        row.attempts = attempts


def _build_chat() -> ChatFn:
    """从 provider 配置造一个 `chat` 函数。

    ★ 复用既有的 provider 体系（不新接模型）：选 provider 的规则与
    `runner.build_spec` 一致（默认的优先、其次按创建时间），凭据走
    `providers.service.decrypt_provider_key`（"明文只在一处出现"那条纪律）。

    ⚠️ 会话里把需要的**标量**取出来再关掉它：`provider` 对象在
    `session_scope` 退出后就 detached 了，之后碰它的任何属性都会炸 ——
    而这个函数是后台线程里跑的，那种异常会变成"生成永远停在 building"。
    """
    from app.providers.models import ProviderAccount
    from app.providers.service import decrypt_provider_key

    with session_scope() as session:
        provider = (
            session.execute(
                select(ProviderAccount).order_by(
                    ProviderAccount.is_default.desc(), ProviderAccount.created_at
                )
            )
            .scalars()
            .first()
        )
        if provider is None:
            raise ApiError(
                409,
                "CTF2_NO_PROVIDER",
                "还没有配置 LLM provider，无法自动识别平台。请先在「设置 → 模型服务」里添加一个。",
            )
        model = provider.model
        base_url = provider.base_url or ""
        label = provider.label
        kind = provider.kind
        api_key = decrypt_provider_key(provider)

    from app.kernel.runner import LlmGateway

    gateway = LlmGateway(
        model=model, api_key=api_key, base_url=base_url, label=label, kind=kind
    )

    async def chat(messages: list[dict[str, str]], *, system: str = "") -> str:
        reply = await gateway.chat(messages, system=system)
        return reply.text

    return chat


def delete_platform(db: Session, platform_id: str) -> None:
    """删掉一份配置。**不动账号** —— 见 `models.Ctf2Account.platform_id`
    那段说明（两件事在语义上无关）。

    ⚠️ 但如果账号正指向它，需要把账号退回内置驱动，否则那边会留下一个
    悬空的 `platform_id`，而症状是"同步时找不到平台，报一个看不懂的错"。
    """
    row = get_platform_or_404(db, platform_id)
    account = (
        db.execute(select(Ctf2Account).where(Ctf2Account.platform_id == platform_id))
        .scalars()
        .first()
    )
    if account is not None:
        account.platform_id = None
    db.delete(row)
    db.commit()


# ══════════════════════════════════════════════════════════
# 启用
# ══════════════════════════════════════════════════════════


def activate_platform(
    db: Session, *, platform_id: str, access_key: str, label: str = ""
) -> Ctf2Account:
    """启用一个生成的平台：验证凭据 → 存进账号表 → 指向它。

    ## ★ 为什么要**再验一次**

    生成阶段已经试跑过了，看起来是重复劳动。但那中间隔着一个不确定的
    窗口：用户可能改了配置、平台可能挂了、key 可能过期。而"启用"这个
    动作的语义是"以后就用它了" —— 用它之前不验，等于把问题推到第一次
    同步时再暴露，而那时用户已经以为配好了。
    """
    from app.core.crypto import encrypt_secret, mask_secret

    platform = get_platform_or_404(db, platform_id)
    cleaned = (access_key or "").strip()
    if not cleaned:
        raise ApiError.validation("请填写这个平台的 AccessKey。", {"accessKey": "必填"})

    spec = spec_of(platform)
    if spec is None:
        raise ApiError(
            409,
            "CTF2_PLATFORM_NOT_READY",
            "这个平台的配置还不完整（或已损坏），请重新生成一次。",
        )

    result = _run(verify_spec(spec, cleaned))
    if not result.ok:
        raise ApiError(400, "CTF2_PLATFORM_VERIFY_FAILED", f"这个平台没能通过验证：{result.error}")

    account = (
        db.execute(select(Ctf2Account).where(Ctf2Account.status == "active"))
        .scalars()
        .first()
    )
    if account is None:
        account = Ctf2Account(id=new_id("ctf2acc"), status="active")
        db.add(account)

    account.label = (label or "").strip() or account.label or platform.display_name
    account.token_encrypted = encrypt_secret(cleaned)
    account.token_hint = mask_secret(cleaned)
    account.platform_id = platform.id
    account.status = "active"
    account.last_error = None
    db.commit()
    db.refresh(account)
    logger.info("已启用平台 %s（%d 道题可见）", platform.slug, result.challenge_count)
    return account


def use_builtin_platform(db: Session) -> None:
    """把账号退回内置的 CTF2 驱动。"""
    account = (
        db.execute(select(Ctf2Account).where(Ctf2Account.status == "active"))
        .scalars()
        .first()
    )
    if account is None:
        return
    account.platform_id = None
    db.commit()


def driver_for(db: Session, account: Ctf2Account | None) -> Any:
    """按账号拿到该用的驱动。

    * `platform_id IS NULL` → 内置 CTF2（`drivers/ctf2` 那条老路）
    * 有 `platform_id` → 读配置，造一个 `GenericDriver`

    ⚠️ 这个函数是**下一轮接线**的入口（`service.sync` / `runner` 会改走
    它）。现在先把它放在这里，并由 `platforms` 自己的测试覆盖 ——
    比等到改 `service` 时再发明它要好。
    """
    from app.kernel import service as ctf2_service

    if account is None or not account.platform_id:
        return None  # 内置：调用方继续用 Ctf2Client
    platform = get_platform(db, account.platform_id)
    spec = spec_of(platform)
    if spec is None:
        raise ApiError(
            409,
            "CTF2_PLATFORM_NOT_READY",
            "当前账号指向的平台配置不可用，请在设置里重新生成或切回内置驱动。",
        )
    key = ctf2_service.decrypt_account_token(account)
    return GenericDriver(spec, key)


__all__ = [
    "BUILTIN_DISPLAY",
    "BUILTIN_SLUG",
    "MAX_BUILD_ATTEMPTS",
    "activate_platform",
    "create_building",
    "delete_platform",
    "driver_for",
    "get_platform",
    "get_platform_or_404",
    "list_platforms",
    "spec_of",
    "start_build",
    "use_builtin_platform",
]
