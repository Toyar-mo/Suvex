"""`ctf2` 领域的业务编排。**router 只做薄封装。**

## ★ 本模块最重要的一件事：同步外观 / 异步内核

`KernelClient` 是 **async**（httpx.AsyncClient），而领域里的 SQLAlchemy
Session 是 **sync** 的（`core/db.py` 定过：本项目不用 async ORM）。
两者必须在一个进程里共存，所以这里定下一条**明确的边界**：

```
router (def，FastAPI 线程池)  →  service 的同步函数  →  _run(协程)
                                                      └─ 协程内部只调 client
worker 线程  →  asyncio.run(runner 的协程)  →  service 的 async 函数
```

### 为什么 `_run`（即 `asyncio.run`）在这里是安全的

FastAPI 的**同步** `def` 端点跑在 **anyio 的工作线程**里，那个线程
**没有正在运行的 event loop**，所以 `asyncio.run()` 完全合法。

⚠️ 反过来说：**只要有人把本模块对应的任一端点改成 `async def`，
所有 `_run` 会立刻抛 `RuntimeError: asyncio.run() cannot be called
from a running event loop`** —— 而这会同时打挂每一个 CTF2 端点。
这条约束不是"最好不要"，是硬性的，所以它写在最上面。

### 为什么不用 async 端点 + async ORM

* 全项目的 Session 都是 sync 的，为 CTF2 单独引一套 async Session
  意味着两张连接池、两套事务语义、以及"同一个请求里两种会话"的混淆；
* 更重要的是：**sync Session 会阻塞 event loop**。一个 `POST /ctf2/sync`
  要跑十几秒（平台往返 + 写库），做成 async 端点等于把那十几秒里
  所有其他请求全部卡住 —— 包括 SSE 心跳。

代价如实记：一次同步会占住线程池里的一个线程十几秒。线程池默认
40 个线程，而"同时点同步"的人不会多，这个代价可以接受。

### `async` 函数里的同步会话：唯一的例外，以及它的前提

`start_environment_for` / `release_environment` 是 async 的，而它们
里有短促的同步 DB 操作。这**只在一种前提下成立**：它们跑在
**worker 自己的线程 + 自己的 event loop** 里（`runner.run_worker`
的 `asyncio.run`），那个 loop 里只有它一个任务 —— 阻塞它没有任何人受害。

⛔ **禁止在请求路径上调用这两个函数。** 函数名里的 `_for` / 参数里的
`client` 是提示："这是给 worker 的那个版本"。

## 凭据：明文只在一处

与 `providers/service.py` 同构：整个模块里明文 PAT 只在
`decrypt_account_token()` 一个函数里出现，它只有一个调用方
（真的要去调 CTF2 的时候）。`KernelAccount` 上**没有** `token` 属性
（见 models.py），所以想绕过去读也读不到。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import threading
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, TypeVar

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

#: 仅用于类型注解的导入（运行时**不**导入）。
#:
#: `KernelWriteup` 在 `get_writeup` 的签名里出现，而那个模块与本模块
#: 有循环导入关系 —— 所以运行时只在函数体内 import。
#: 这个块让静态检查（ruff 的 F821）认得那个名字。
if TYPE_CHECKING:
    from app.kernel.models import KernelWriteup

from app.core.crypto import SecretDecryptError, decrypt_secret, encrypt_secret, mask_secret
from app.core.db import session_scope
from app.core.errors import ApiError, ErrorCode
from app.core.ids import new_id
from app.core.text import strip_nul
from app.core.timeutil import now_utc, parse_iso
from app.core.vocab import (
    CTF2_AUTO_PRIORITY_BASE,
    CTF2_AUTO_PRIORITY_MAX,
    CTF2_PRIORITY_MANUAL_MAX,
    DEFAULT_CTF2_AUTO_SYNC_SECONDS,
    DEFAULT_CTF2_MAX_CONCURRENT_SOLVES,
    DEFAULT_CTF2_MAX_TURNS,
    DEFAULT_CTF2_MAX_WRONG_SUBMISSIONS,
    DEFAULT_CTF2_SOLVE_POLL_SECONDS,
    MAX_CTF2_AUTO_SYNC_SECONDS,
    MAX_CTF2_MAX_CONCURRENT_SOLVES,
    MAX_CTF2_MAX_TURNS,
    MAX_CTF2_SOLVE_POLL_SECONDS,
    MIN_CTF2_AUTO_SYNC_SECONDS,
    MIN_CTF2_MAX_CONCURRENT_SOLVES,
    MIN_CTF2_MAX_TURNS,
    MIN_CTF2_SOLVE_POLL_SECONDS,
    RESOLVABLE_CTF2_CHALLENGE_STATUSES,
    IrCategory,
    KernelChallengeStatus,
    KernelCompetitionKind,
    KernelEnvironmentStatus,
    KernelSandboxProfile,
    KernelSubmitMode,
    normalize_sandbox_profile,
)
from app.kernel import events as ctf2_events
from app.kernel.attachments import (
    DownloadResult,
    PendingDownload,
    download_all,
    plan_downloads,
)
from app.kernel.client import (
    EnvironmentAccess,
    KernelAuthExpired,
    KernelClient,
    KernelEnvironmentTimeout,
    KernelError,
    KernelRateLimited,
    KernelResponseError,
    KernelScopeMissing,
)
from app.kernel.models import (
    KernelAccount,
    KernelChallenge,
    KernelCompetition,
    KernelEnvironment,
    KernelSolveAttempt,
)
from app.settings.models import SINGLETON_ID, KernelConfig

logger = logging.getLogger("suvex.ctf2")

T = TypeVar("T")

# ── 上限（都要能说出为什么）──────────────────────────────

# 一次同步最多处理多少个赛事。
#
# ★ 必须有上限：一次同步是一串**外部请求**（赛事 → 阶段 → 题目，
# 每个阶段至少一个请求）。没有上限时"同步全部可见赛事"会变成一个
# 几分钟的请求，而它在反代/浏览器超时后是**部分成功**的 ——
# 那是最难解释的状态（"有些题出来了有些没有，为什么？"）。
MAX_SYNC_COMPETITIONS = 50

# 单个练习集一次最多同步多少道题。理由同上。
MAX_SYNC_CHALLENGES = 500

# 错误提交上限的可配区间。
#
# ⚠️ 上限之所以不是"想设多少就设多少"：这一列是**自动提交的刹车片**
# （见 vocab.DEFAULT_CTF2_MAX_WRONG_SUBMISSIONS）。允许设成 100 等于
# 让用户把一个安全边界自己拆掉，而那个后果（把这道题的机会全烧光）
# 是**不可逆**的。所以给一个天花板，并在 API 层拒绝超出的值。
#
# ⚠️ 这个数与 `settings/models.py` 的 `KernelConfig` 上的 CHECK
# （`max_wrong_submissions <= 10`）是**同一个数**，改一处必须改另一处
# 并补一条迁移 —— 那条 CHECK 是字面量固化的，不会跟着这里变。
MIN_WRONG_SUBMISSIONS = 0
MAX_WRONG_SUBMISSIONS_CEILING = 10

# 没有租期信息的环境，多久之后算陈旧。
#
# ★ 为什么要有这一条：平台给的环境**带租期**（实测默认 1 小时，见
#   `start_environment_for` 里 `_parse_time(access.expires_at)` 那句），
#   但 `expires_at` 可空 —— 早先那几批环境就是 NULL（写回租期是后来才
#   补的）。这些行永远不会"到期"，因为库里没有它们的到期时刻。
#
#   真库里实测有 2 条 `status='ready' and expires_at is null`，其中一条
#   已经停了十几天。它们在前端会渲染成一个标着"就绪"的死链接
#   （`EnvironmentStatus.tsx` 的规则是 `accessUrl` 只在 ready 时渲染）。
#
# ⚠️ 24 小时是"明显陈旧"而不是"过期"：默认租期 1 小时，给 24 倍余量，
#   长租期环境不会被误杀。判据是 `ready_at`（环境**就绪**的时刻），
#   不是 `created_at` —— 行是启动前就建的，创建到就绪之间可能隔很久。
STALE_ENVIRONMENT_WITHOUT_TTL_HOURS = 24


# ══════════════════════════════════════════════════════════
# 同步外观：在同步上下文里跑协程
# ══════════════════════════════════════════════════════════


def _run(coro: Awaitable[T]) -> T:  # noqa: UP047 - 见下
    """在同步上下文里跑一个协程。见模块 docstring 的边界说明。

    ⚠️ 只在**没有正在运行的 event loop** 的线程里调用它。
    在 event loop 里调用会抛 RuntimeError —— 那是好事（早失败），
    而不是一个会被静默吞掉的问题。

    ⚠️ `# noqa: UP047`：ruff 建议改成 PEP 695 的 `def _run[T](...)`。
    刻意不改成那个 —— 项目的部署目标是 Kali 上的 **3.13**，但代码要能被
    3.12 的开发环境读；而且更实际的理由是：`TypeVar` 是**这个仓库里
    一致的写法**（其余模块也是这么写的），单独给这一个函数换个语法
    会让"泛型长什么样"在这里分叉。等全仓库一起换时再换。
    """
    try:
        return asyncio.run(coro)  # type: ignore[arg-type]
    except RuntimeError as exc:
        if "event loop" in str(exc):
            raise RuntimeError(
                "在正在运行的 event loop 里调用了 CTF2 的同步外观（asyncio.run）。"
                "这说明有端点被改成了 `async def` —— 见 app/kernel/service.py "
                "模块 docstring 里那条硬性约束。"
            ) from exc
        raise


def _translate(exc: KernelError) -> ApiError:
    """领域异常 → 可展示的 `ApiError`。

    ★ 这个翻译必须**逐类型**做，不能统一成一句"对接 CTF2 失败"：

    | 领域异常 | HTTP | 补救动作 |
    | --- | --- | --- |
    | `KernelAuthExpired` | 401 | 重新绑定账号（只有人能做的动作） |
    | `KernelScopeMissing` | 403 | 去平台加那个 scope（异常里带着名字） |
    | `KernelRateLimited` | 429 | 等一会儿再试（带 `Retry-After` 头） |
    | `KernelEnvironmentTimeout` | 504 | 过一会儿重新启动环境 |

    统一成一句的后果是用户一直点重试一个永远不会好的东西 ——
    而四种情况里有两种是**重试无用**的。
    """
    if isinstance(exc, KernelAuthExpired):
        return ApiError(
            401,
            # 用专门的码而不是 UNAUTHENTICATED：那个码的含义是
            # "**你**没登录"，而这里是我们持有的 PAT 失效了 ——
            # 前端据此显示的是"重新绑定"而不是"跳登录页"。
            "CTF2_PAT_EXPIRED",
            exc.message,
        )
    if isinstance(exc, KernelScopeMissing):
        return ApiError(
            403,
            ErrorCode.INSUFFICIENT_SCOPE,
            exc.describe(),
            {"missingScope": exc.missing_scope} if exc.missing_scope else None,
        )
    if isinstance(exc, KernelEnvironmentTimeout):
        return ApiError(504, "CTF2_ENVIRONMENT_TIMEOUT", exc.message)
    if isinstance(exc, KernelRateLimited):
        seconds = int(exc.retry_after or 30)
        return ApiError.rate_limited(max(1, seconds), exc.message)
    return ApiError(502, "CTF2_UPSTREAM_ERROR", exc.message)


# ══════════════════════════════════════════════════════════
# 账号
# ══════════════════════════════════════════════════════════


def get_account(db: Session, *, active_only: bool = True) -> KernelAccount | None:
    """当前绑定的账号。没有返回 None（**不建行、不抛**）。

    与 `settings.service.read_sandbox_config` 同一个模式：
    读操作不该有副作用 —— "只是打开了一次 CTF2 页面"不应该凭空建出一行。
    """
    stmt = select(KernelAccount).order_by(KernelAccount.created_at.desc()).limit(1)
    if active_only:
        stmt = stmt.where(KernelAccount.status == "active")
    return db.execute(stmt).scalars().first()


def list_accounts(db: Session) -> list[KernelAccount]:
    return list(
        db.execute(select(KernelAccount).order_by(KernelAccount.created_at.desc())).scalars()
    )


def get_account_or_404(db: Session, account_id: str) -> KernelAccount:
    account = db.get(KernelAccount, account_id)
    if account is None:
        raise ApiError.not_found("这个 CTF2 账号绑定")
    return account


def decrypt_account_token(account: KernelAccount) -> str:
    """取出明文 PAT。**全模块唯一的解密入口。**

    ⚠️ 失败时抛的是 `ApiError` 而不是把 `SecretDecryptError` 透出去：
    那个异常的消息里提到 `SUPEX_SECRET_KEY`，而它是给运维看的。
    用户需要知道的是"凭据解不开，请重新绑定"。
    """
    if not account.token_encrypted:
        raise ApiError(
            409,
            "CTF2_NOT_BOUND",
            "尚未绑定 CTF2 账号，或该绑定已被解绑。请先绑定一个 PAT。",
        )
    try:
        return decrypt_secret(account.token_encrypted)
    except SecretDecryptError as exc:
        raise ApiError(
            500,
            ErrorCode.PROVIDER_KEY_UNREADABLE,
            f"无法解密 CTF2 账号「{account.label or account.id}」的凭据：{exc}"
            "（最可能的原因是 SUPEX_SECRET_KEY 被更换或丢失；"
            "换回原密钥即可恢复，否则只能重新绑定。）",
        ) from exc


def build_client_sync() -> KernelClient:
    """构造一个客户端（取当前活跃绑定的 PAT）。

    ★ 名字里的 `sync` 指的是"**在同步上下文里调用**"，不是"同步的
    HTTP 客户端" —— 返回的仍然是 `async` 的 `KernelClient`，
    由调用方在自己的协程里用它。

    为什么是函数而不是一个可复用的单例：PAT 会在重新绑定时变化，
    而缓存一个客户端意味着**换绑之后还在用旧凭据** —— 那个症状是
    "我明明重新绑了，它还说 401"。每次现构造的代价只是一次解密。
    """
    with session_scope() as db:
        account = get_account(db)
        if account is None:
            raise ApiError(
                409, "CTF2_NOT_BOUND", "尚未绑定 CTF2 账号。请先在「CTF2」页绑定一个 PAT。"
            )
        token = decrypt_account_token(account)
    return KernelClient(token=token)


def bind_account(db: Session, *, token: str, label: str = "") -> KernelAccount:
    """绑定（或**换绑**）一个账号。

    ## 换绑不新建行，而是**就地替换凭据**

    同一个实例只会绑一个 CTF2 账号（数据库上的部分唯一索引
    `uq_ctf2_accounts_active` 强制了这一点）。所以"再绑一次"的语义是
    **换一把钥匙**，而不是"多一个账号"。

    为什么不做成多账号：`KernelChallenge` 通过 `competition.account_id`
    间接归属账号，而多账号会让"这道题是哪个号的"变成一个每次都要查的
    问题 —— 而提交 flag 必须用**当初读到这道题的那个号**（额度是按号的）。

    ## 绑定要真的验一次凭据

    不验的话，用户会得到一个"绑定成功"的提示，然后在第一次点解题时
    才吃到 401 —— 而那时他已经以为绑定没问题了。所以这里**真的发一次
    请求**（`GET practice/`，最轻的那个端点）。

    验失败时**不落库**：把一条无效凭据存下来，会让界面上出现一个
    "已绑定"的账号，而它什么都做不了。
    """
    cleaned = (token or "").strip()
    if not cleaned:
        raise ApiError.validation("请填写 CTF2 的 PAT。", {"token": "必填"})

    async def _probe() -> None:
        async with KernelClient(token=cleaned) as client:
            # `practice/` 是最轻的端点：它只列练习集，不涉及任何状态。
            # 用它做探测而不是"随便调一个能 200 的端点"——
            # 探测的用途是验证**凭据有效且这个 scope 有**。
            await client.list_practice()

    try:
        _run(_probe())
    except KernelError as exc:
        raise _translate(exc) from exc

    account = get_account(db)
    if account is None:
        account = KernelAccount(id=new_id("knacc"), status="active")
        db.add(account)

    account.label = (label or "").strip() or account.label or "CTF2 账号"
    # 密文与掩码**必须一起更新**（读取路径靠掩码，不做解密）——
    # 与 provider 的同名两列是同一条规矩。
    account.token_encrypted = encrypt_secret(cleaned)
    account.token_hint = mask_secret(cleaned)
    account.status = "active"
    account.last_error = None
    account.last_synced_at = now_utc()
    db.commit()
    db.refresh(account)
    return account


def unbind_account(db: Session, account_id: str) -> str:
    """解绑：**把密文真删掉**，保留行。返回被解绑账号的**标签**。

    ⚠️ 不清空密文而只是改 `status` 的话，那把 PAT 还躺在库里 ——
    而用户的操作语义是"我不想让它留在这里"。所以密文与掩码一起置 NULL。

    行保留是因为它下面挂着赛事与提交记录（那些是 CTF2 的资产，
    而 `ondelete=CASCADE` 会把它们一起带走）。见 models 的说明。

    ★ 为什么返回标签而不是 `None`：审计要写"解绑了哪个号"，而解绑之后
    这一行就只剩 id 了 —— 事后看一条只有 id 的审计记录等于没写。
    让调用方自己再查一次也能拿到，但那意味着**两次查询之间有窗口**，
    而且是把"记得先取标签"这件事交给每个调用方去记。
    """
    account = get_account_or_404(db, account_id)
    label = account.label
    account.token_encrypted = None
    account.token_hint = None
    account.status = "revoked"
    account.scopes = None
    account.last_error = None
    db.commit()
    return label


# ══════════════════════════════════════════════════════════
# 同步
# ══════════════════════════════════════════════════════════


@dataclass(slots=True)
class SyncResult:
    """一次同步的账。

    ★ 三个计数**必须分开**：`created` / `updated` / `errors`。
    只给一个"同步了 N 个"会让用户无法判断"我改了题面它有没有同步过来"，
    而那正是他会重复点同步的原因（每次都得等一遍完整的往返）。
    """

    competitions: int = 0
    challenges_created: int = 0
    challenges_updated: int = 0
    errors: list[str] = field(default_factory=list)
    # ★ 附件单独一笔账，**不并进上面三个计数**：题面同步成功但附件
    #   全军覆没（官方域名换了、磁盘满了）是一种真实且需要被看见的
    #   状态，混进"同步了 N 道题"里就没人能发现。
    attachments: DownloadResult = field(default_factory=DownloadResult)
    # ★ 一句"为什么什么都没有"的说明（可能为空）。
    #
    # 为什么需要它：`赛事 5 · 新增题目 0` 这个组合会让人一遍遍点同步 ——
    # 数字都"正常"，但界面上就是没有题，而**原因不在这个数字里**。
    # 不解释的话，用户唯一能做的就是再点一次。
    note: str = ""

    # ★ 这次**新入库**的题目 id（v0.24）。
    #
    # 为什么要把它带出来：自动作业在同步之后要回答"哪几道是刚发现的"，
    # 而那个答案**只有同步自己知道** —— `_upsert_challenge` 返回的
    # `created` 是唯一权威。让调用方回头去猜（按 `created_at` 排序取
    # 最新一批）会在边界上出错：同一次同步里既有新题又有更新题，
    # 而它们的时间戳落在同一秒里。
    #
    # ⚠️ 它**不参与** `summary()`：那是给用户看的一句话，而这是给自动
    #    作业用的机器输入。混在一起会让"新增题目 3"后面拖一串 uuid。
    new_challenge_ids: list[str] = field(default_factory=list)

    def message(self) -> str:
        """给用户看的一句话（`summary()` + 必要的解释）。"""
        return f"{self.summary()}{self.note}"

    def summary(self) -> str:
        parts = [f"赛事 {self.competitions}", f"新增题目 {self.challenges_created}"]
        if self.challenges_updated:
            parts.append(f"更新 {self.challenges_updated}")
        if self.attachments.downloaded or self.attachments.skipped or self.attachments.failed:
            parts.append(self.attachments.summary())
        if self.errors:
            parts.append(f"失败 {len(self.errors)} 项")
        return " · ".join(parts)


# 同一时刻只允许**一次**同步在跑。
#
# ★ 为什么需要它：v0.24 之后有两类调用方 —— 用户手点（router）与自动
#   作业（`ctf2/autosync.py`）。它们并发时做的是同一件事（把平台读一遍），
#   后果是**请求量翻倍**。比赛里的请求预算和解题**共享同一个账号**，
#   多打一遍就是多推高一次撞 429 的概率，而 429 的代价落在提交上。
#
# ⚠️ 必须是 `RLock`：`try_sync` 拿到锁之后要调用 `sync`，而 `sync`
#   自己也要拿同一把锁 —— 不可重入的锁会在那里**自己等自己**，
#   表现为"点一次同步，界面永远转圈"。
_SYNC_LOCK = threading.RLock()


def try_sync(
    db: Session, *, competition_ids: list[str] | None = None
) -> SyncResult | None:
    """能拿到同步锁就同步，拿不到返回 `None`。**自动作业专用。**

    与 `sync` 的区别只有一条：它**不排队**。自动同步遇到"用户正在手动
    同步"时应该跳过这一轮 —— 排队会让一次手点之后积压一串重复同步，
    而那正好是最浪费请求预算的行为。
    """
    if not _SYNC_LOCK.acquire(blocking=False):
        return None
    try:
        return sync(db, competition_ids=competition_ids)
    finally:
        _SYNC_LOCK.release()


def sync(db: Session, *, competition_ids: list[str] | None = None) -> SyncResult:
    """同步一次（**全进程串行**）。

    实现与全部理由见 `_sync_locked`，这里只负责那把锁。
    """
    with _SYNC_LOCK:
        return _sync_locked(db, competition_ids=competition_ids)


def _sync_locked(db: Session, *, competition_ids: list[str] | None = None) -> SyncResult:
    """把平台上的赛事与题目拉进本地库。

    ## 为什么是"拉取覆盖"而不是"增量对账"

    平台的题目**会被改**（题面补充提示、附件替换、分值调整），
    而我们没有任何"它变了"的信号（CTF2 没有 webhook，也没有版本号）。
    所以每次同步都是**重读一遍并覆盖**。

    代价是同步是 O(题目数) 次请求，且每次都全量。对"几场赛事、
    几十道题"完全可接受；对"一次同步五百道题"就慢得明显 ——
    这也正是 `MAX_SYNC_CHALLENGES` 存在的理由。

    ## 一条失败不中断整次同步

    某一道题的详情拉取失败（平台抖动、题目被删）时**记进 errors
    继续走**。中断的代价是"前面成功的那几十道也没落库"，
    而用户看到的是一个失败提示加一个空列表。

    ## 附件：落库之后单独一段，不在事务里

    附件清单存进 JSONB 的同时，真实文件由 `attachments.download_all`
    拉到 `_ctf2_attachments/<challengeId>/`（域名白名单、逐文件与
    单次总量上限、文件名净化都在那个模块里）。

    ★ 它**必须在 `db.commit()` 之后**，而且必须有自己的账：
    附件下载失败（出题人放了站外链接、磁盘满、平台抖）不该回滚
    "赛事和题面已经同步成功"这件事 —— 用户至少还能看到题面、
    手动点重试；反过来，把下载放进事务里，一次附件失败就会让
    整场赛事的题目全部消失。
    """
    account = get_account(db)
    if account is None:
        raise ApiError(409, "CTF2_NOT_BOUND", "尚未绑定 CTF2 账号。请先绑定一个 PAT。")
    token = decrypt_account_token(account)
    result = SyncResult()

    # ★ 已经在本地的 challenge remote_id —— 侧门只补**新的**。
    #
    # ## 为什么必须这样做
    #
    # 一次全量侧门要 **98+ 个请求、约 45 秒**（客户端限速 5 req/s，那是
    # 平台的礼貌线，调高会撞 429）。而"重复点同步"恰恰是最常见的操作 ——
    # 用户看到没更新就会再点一次。
    #
    # 实测（2026-10-01，用户截图）：全量侧门让同步稳定跑到 45 秒，
    # 而前端 `DEFAULT_TIMEOUT_MS = 20_000` 到点就 abort，于是界面上弹出
    # **"同步失败：请求超时或被取消"** —— 而后端其实每次都跑完了
    # （`last_synced_at` 正常推进、98 道题也在库里）。
    # 那是一条**假失败**：用户以为没同步，实际同步好了。
    #
    # 补了这道过滤之后：首次同步仍要 45 秒（一次性的），但重复同步只发
    # `writeups` 一个请求 —— 秒级返回。
    #
    # ## 代价说清楚
    #
    # 已在库的题目**详情不会刷新**（平台改了题面/附件我们不会跟进）。
    # 这个取舍是划算的：CTF 题目是静态的，而"每次同步都要 45 秒、
    # 且前端必然误报失败"是**每次**都在付的代价。
    # 真要强制刷新一道题，重新拉一次单题（「拉取单题」）即可。
    #
    # ⚠️ 在 `_fetch` **之前**读：`_fetch` 是 async 且按设计不碰 DB
    #    （见它自己的 docstring），所以这里先查好、以闭包形式传进去。
    #
    # ★★ v0.30：收窄到**当前账号**作用域（2026-10-12 修）。
    #
    # ## 原来取全表，会误跳过
    #
    # 原来的写法是 `select(KernelChallenge.remote_id)` —— **不带任何
    # 作用域**。而"已在库"这个判断的含义应当是"**本账号**已经有这道题了"，
    # 不是"这个 remote_id 在这个数据库里出现过"。
    #
    # 后果：不同赛事/练习集里**恰好同名**的 remote_id 会让新题被跳过。
    # 而平台侧的 id 是**按赛事分配**的（UUID 碰撞概率极低，但
    # "练习集 A 的题目 1" 与 "练习集 B 的题目 1" 用同一个短 id 是
    # 完全可能的）。症状是"同步说完成了，但题库里少了几道题"——
    # 而它看起来像"平台没有这些题"。
    #
    # ## 为什么按账号而不是按赛事收窄
    #
    # 题目归属是 `challenge → competition → account`。按账号收窄既
    # 足够（同一个账号下才有"这道题我有没有"的意义），又不会漏掉
    # "同一道题出现在两个练习集里"这种情况 —— 那时它本来就该被认成
    # 同一个 remote_id。
    #
    # ⚠️ 用 join 而不是先查赛事 id 再 `in_()`：后者在赛事很多时会生成
    #    一个很长的 IN 列表，而 join 让数据库自己选执行计划。
    known_remote_ids = {
        str(x)
        for x in db.execute(
            select(KernelChallenge.remote_id)
            .join(
                KernelCompetition,
                KernelCompetition.id == KernelChallenge.competition_id,
            )
            .where(KernelCompetition.account_id == account.id)
        )
        .scalars()
        .all()
    }

    async def _fetch() -> dict[str, Any]:
        """把需要的东西**一次性**拉回来，再落库。

        ★ 这个分工是刻意的：网络往返在一段里做完，落库在同步上下文里做完。
        混着做的话，我们要么在协程里开数据库会话（阻塞 event loop），
        要么把客户端对象传进同步函数（那会让 session 生命周期难以推理）。
        """
        collected: dict[str, Any] = {
            "practice": [],
            "practice_details": [],
            "competitions": [],
            # ★ 拉取阶段的错误也要带回来 —— 见下面赛事那段：
            #   一场拉不动（没加战队、被删了）不该让**整次**同步失败。
            "errors": [],
        }
        async with KernelClient(token=token) as client:
            practices = await client.list_practice()
            collected["practice"] = practices
            for item in practices[:MAX_SYNC_COMPETITIONS]:
                remote_id = _pick(item, "id", "practice_id")
                if remote_id is None:
                    continue
                # ⚠️ 平台**没有"列出练习集题目"的端点**（见 client 的说明），
                #    所以"内嵌数组"这条在 CTF2 上**恒为空**。
                #    保留它只是为了兼容"别的平台把题目塞进练习集对象"的形状
                #    （这份代码的目标平台会换 —— 见模块 docstring）。
                #
                #    ★ 真正能从 CTF2 捞到题的是下面那段**解法侧门**。
                challenges = item.get("challenges")
                if not isinstance(challenges, list):
                    continue
                for challenge in challenges[:MAX_SYNC_CHALLENGES]:
                    challenge_id = _pick(challenge, "id", "challenge_id")
                    if challenge_id is None:
                        continue
                    detail = await client.get_practice_challenge(
                        str(remote_id), str(challenge_id)
                    )
                    collected["practice_details"].append(
                        {
                            "practice_id": str(remote_id),
                            "challenge": {**challenge, **(detail or {})},
                        }
                    )

            # ── ★ 题目来源：解法侧门 ────────────────────────────
            #
            # 这是 CTF2 上**唯一**能批量拿到真实题目的路。
            #
            # 链条：`writeups`（公开解法，每条带 `challenge_id`）
            #   → 逐个练习集试 `practice/{id}/challenges/{cid}/`
            #   → 命中的那道就是可提交的练习题。
            #
            # 实测（2026-09-30）：98 个 id **98/98 全中**
            # （BUUCTF 96、N1BOOK 1、BUUCTF-Real 1）。
            #
            # ⚠️ 试练习集的**顺序**决定请求量：按题目数降序能让绝大多数
            #    id 一次命中（实测 98 次请求，而不是最坏 98×5=490 次）。
            #    96/98 落在题目最多的那个练习集下面，这不是巧合 ——
            #    镜像题、搬运题的解法自然集中在大库里。
            #
            # ⚠️ 逐 id 逐练习集容错：某道题不属于任何练习集是**正常**的
            #    （daily 的 25 道就是 0 命中），不该中断整次同步。
            harvested = 0
            try:
                writeups = await client.list_writeups()
            except KernelError as exc:
                # 侧门拉不动不该毁掉整次同步 —— 练习集与赛事那两部分
                # 已经拉好了，用户至少还能看到它们。
                logger.warning("CTF2 解法列表拉取失败（不影响其余同步）：%s", exc)
                writeups = []

            if writeups:
                collected["writeups_total"] = len(writeups)
                already = {
                    str(entry["challenge"].get("id") or "")
                    for entry in collected["practice_details"]
                }
                candidate_ids = [
                    str(cid)
                    for cid in dict.fromkeys(
                        w.get("challenge_id") for w in writeups if w.get("challenge_id")
                    )
                    # ★ 已在库的**直接跳过** —— 这是把重复同步从 45 秒压到
                    #   秒级的关键。理由与代价见 `known_remote_ids` 的说明。
                    if str(cid) not in already and str(cid) not in known_remote_ids
                ][:MAX_SYNC_CHALLENGES]
                # 题目多的练习集排前面 —— 命中率高，请求少。
                ordered = sorted(
                    practices[:MAX_SYNC_COMPETITIONS],
                    key=lambda p: -int(p.get("challenge_count") or 0),
                )
                for cid in candidate_ids:
                    for practice in ordered:
                        practice_id = _pick(practice, "id", "practice_id")
                        if practice_id is None:
                            continue
                        try:
                            detail = await client.get_practice_challenge(
                                str(practice_id), cid
                            )
                        except KernelError:
                            # 不属于这个练习集是**正常**的（详情端点回 404），
                            # 继续试下一个。
                            continue
                        if not detail:
                            continue
                        collected["practice_details"].append(
                            {"practice_id": str(practice_id), "challenge": detail}
                        )
                        harvested += 1
                        break
                collected["harvested"] = harvested
                logger.info(
                    "CTF2 解法侧门：候选 %d 个 challenge_id，捞到 %d 道题",
                    len(candidate_ids),
                    harvested,
                )
            for competition_id in (competition_ids or [])[:MAX_SYNC_COMPETITIONS]:
                # ★ **逐场容错**：一场拉不动就记一笔、继续下一场。
                #
                # 这一条是被真实事故逼出来的：用户勾了一场自己**还没加入
                # 战队**的赛事时，平台回 403 `TEAM_MEMBERSHIP_REQUIRED`，
                # 而那个异常会让 `_fetch` 整体失败 —— 于是**连练习集**
                # （前面已经拉好的那部分）也一起落不了库。
                # 用户看到的是"整次同步失败"，而他其实只勾错了一场。
                #
                # 与模块 docstring 承诺的"一条失败不中断整次同步"是同一条规矩，
                # 只是那句话说在题目层面，而这里要落到赛事层面。
                try:
                    entry = await _fetch_competition(client, competition_id)
                except KernelError as exc:
                    logger.warning("CTF2 赛事 %s 拉取失败：%s", competition_id, exc)
                    collected["errors"].append(f"赛事 {competition_id}：{exc}")
                    continue
                collected["competitions"].append(entry)
        return collected

    try:
        collected = _run(_fetch())
    except KernelError as exc:
        raise _translate(exc) from exc

    # ── 落库（同步上下文）──────────────────────────────────
    #
    # ⚠️ 附件清单在这里**只收集、不下载**：`download_all` 是协程，
    #    而这一段跑在请求线程的同步上下文里（见模块 docstring 的边界）。
    pending_downloads: list[PendingDownload] = []

    def _account(challenge: KernelChallenge, *, created: bool) -> None:
        """记一道题的账，并把**新入库**的 id 单独收好。

        抽成闭包而不是在两个调用点各写两行：那两行原来是
        `created += 1 if created else 0` / `updated += 0 if created else 1`，
        一个 `if/else` 的真值表被拆成两个方向相反的表达式 —— 加第三个
        计数（新题 id）时，两次调用都必须记得改，而漏掉一处不会报错，
        只会让"新题列表"少一半。这里只有一个地方需要维护。
        """
        if created:
            result.challenges_created += 1
            result.new_challenge_ids.append(challenge.id)
        else:
            result.challenges_updated += 1

    # ★ 拉取阶段记下的错误（比如"某场赛事没加战队"）要进同一本账 ——
    #   否则用户只会看到"赛事 1 · 新增题目 0"，而不知道那 1 场其实没进来。
    result.errors.extend(collected.get("errors") or [])

    for item in collected["practice"]:
        remote_id = _pick(item, "id", "practice_id")
        if remote_id is None:
            continue
        _upsert_competition(
            db, account=account, remote_id=str(remote_id), kind="practice", payload=item
        )
        result.competitions += 1

    for entry in collected["practice_details"]:
        competition = _find_competition(
            db, account.id, entry["practice_id"], "practice"
        )
        if competition is None:
            continue
        challenge, created = _upsert_challenge(
            db, competition=competition, stage_id=None, payload=entry["challenge"]
        )
        _account(challenge, created=created)
        pending_downloads.extend(plan_downloads(challenge.id, challenge.attachments))

    for entry in collected["competitions"]:
        competition = _upsert_competition(
            db,
            account=account,
            remote_id=str(entry["remote_id"]),
            kind="competition",
            payload=entry["status"],
        )
        result.competitions += 1
        for stage_id, payload in entry["challenges"]:
            challenge, created = _upsert_challenge(
                db, competition=competition, stage_id=stage_id, payload=payload
            )
            result.challenges_created += 1 if created else 0
            result.challenges_updated += 0 if created else 1
            pending_downloads.extend(plan_downloads(challenge.id, challenge.attachments))

    # 回写题目计数：列表页读它，避免 N+1 次 count。
    for competition in (
        db.execute(
            select(KernelCompetition).where(KernelCompetition.account_id == account.id)
        )
        .scalars()
        .all()
    ):
        competition.challenge_count = len(
            db.execute(
                select(KernelChallenge.id).where(
                    KernelChallenge.competition_id == competition.id
                )
            ).all()
        )

    account.last_synced_at = now_utc()
    account.last_error = None
    db.commit()

    # ── 附件下载（第二段：网络，且**不碰 Session**）────────────
    if pending_downloads:
        try:
            result.attachments = _run(download_all(token, pending_downloads))
        except KernelError as exc:
            # 只吞**领域内**的错误：真 bug（比如 file 路径算错）必须冒出来。
            result.attachments.failed.append(str(exc))
        if result.attachments.failed:
            # 记一条到同步账上，让前端能提示"附件没下全"，
            # 而不是让用户以为解题失败是模型的问题。
            result.errors.extend(f"附件：{item}" for item in result.attachments.failed[:5])

    # ── ★ 一句"为什么一道题都没有" ──────────────────────────
    #
    # 这是**实测**出来的能力边界，不是我们的 bug，但它长得像 bug：
    #
    #   · `GET /practice/` 能拉到练习集，但练习集对象里**没有题目清单字段**
    #     （只有 `challenge_count` 这个数字）；
    #   · 而"列出练习集题目"的接口**不存在** —— 带着真 PAT 试过 11 条候选
    #     路径，全部返回 `404 {"params": {"resource": "route"}}`（那是
    #     "路由不存在"，不是"资源不存在"）；
    #   · 唯一**存在**的是"按已知 id 取单题详情"
    #     （`GET /practice/{id}/challenges/{cid}/`，编造 id 时返回的是
    #     `NOT_FOUND` 而不是 route 不存在，说明端点是活的）。
    #
    # ★★ 但"没有列表端点"**不等于**"拉不到题"。
    #
    # 题目 id 可以从**解法侧门**批量拿到：`GET /writeups/` 每条公开解法
    # 都带着它讲的是哪道题（`challenge_id`），拿到 id 再逐个练习集去配
    # 详情端点即可。实测 98/98 全中，一次同步能拉进 97 道题 —— 见 `_fetch`
    # 里"解法侧门"那一段。
    #
    # ⚠️ 这个 note 原来写的是"要批量拉题目请用「同步赛事题目」"。那句话
    #    在当时是对的（侧门还没发现），但它把用户指向了一条**需要先加入
    #    战队、且对已结束的赛事根本无效**的路 —— 用户会照做，然后再撞一次墙。
    #
    # 所以它现在只在**连侧门都没取到内容**时才出现（解法端点抖动，或
    # PAT 缺 `writeup:read`）。那时该回答的是"下一步怎么办"，而不是给一句
    # 让人反复点同步的结论。
    # ⚠️ 判据要**三分**，不能只看"新增 0"。
    #
    # 这一条被改了三次，每次都是被一个真实的误导逼回来的：
    #
    #   ① 只看 `created == 0` → 幂等第二次同步（`新增 0 · 更新 98`）
    #      也会弹"侧门没取到内容"，自相矛盾；
    #   ② 改成 `created == 0 and updated == 0` → 侧门补了"跳过已在本地的"
    #      之后，重复同步正好是 `新增 0 · 更新 0`（**这是最好的结果**），
    #      于是又弹同一句"侧门没取到内容"。
    #
    # 根因是：`created/updated` 描述的是"**这次改了几行**"，而 note 要回答
    # 的是"**为什么没题**"。两者不是一回事 —— 得直接看侧门到底有没有货。
    if not competition_ids and result.challenges_created == 0:
        writeups_total = int(collected.get("writeups_total") or 0)
        if writeups_total == 0:
            # 侧门是空的：这是**唯一**真正需要解释的情况。
            result.note = (
                "。练习集本身只提供元信息（平台没有「列出题目」的端点），"
                "而平时用来补题的「公开解法」侧门这次没取到内容。"
                "可以稍后重试；若持续为空，请确认这条 PAT 带有 writeup:read 权限"
            )
        elif result.challenges_updated == 0:
            # 侧门有货、但一道新的都没有 —— 说明本地已经是最新。
            # ★ 这不是失败，要把话说清楚，否则用户会反复点同步。
            total = db.execute(
                select(func.count()).select_from(KernelChallenge)
            ).scalar_one()
            result.note = f"。本地已是最新（{total} 道题都在库里，本次无需更新）"

    logger.info("CTF2 同步完成：%s", result.message())
    return result


async def _fetch_competition(client: KernelClient, competition_id: str) -> dict[str, Any]:
    """把**一场**赛事拉成"可落库的形状"（状态 + 各阶段的题目）。

    ★ 单独一个函数是为了让调用方能**逐场** try/except（见 `_fetch` 里那段
    "逐场容错"的说明）。放在循环体里的话，异常会直接冒到 `_fetch` 外面，
    而那意味着"一场勾错了，全都没同步"。

    ⚠️ 这里会用三个端点，其中 `/competitions/{id}/status/` 与
    `/stages/` 在一些场次上会 **403 `TEAM_MEMBERSHIP_REQUIRED`**
    （团队赛且这个账号没加入战队）。那不是我们能绕的 —— 平台的规矩。
    它会被上面的 try/except 记成一笔错误，并在错误文案里翻成人话
    （见 `client._ERROR_KEY_HINTS`）。
    """
    status = await client.competition_status(competition_id)
    stages = await client.list_stages(competition_id)
    entry: dict[str, Any] = {
        "remote_id": competition_id,
        "status": status,
        "challenges": [],
    }
    for stage in stages:
        stage_id = _pick(stage, "id", "stage_id")
        if stage_id is None:
            continue
        for challenge in await client.list_stage_challenges(str(stage_id)):
            # 与练习集那条路径**同一道筛子**：没有 id 的题目落不了库
            # （`_upsert_challenge` 会拒绝，而那是整次同步级别的）。
            if _pick(challenge, "id", "challenge_id") is None:
                continue
            entry["challenges"].append((str(stage_id), challenge))
    return entry


def list_remote_competitions() -> list[dict[str, Any]]:
    """**直接问平台**有哪些赛事（不落库）。

    ## 为什么需要它：先有鸡还是先有蛋

    同步赛事题目要用户先选赛事，而"可选赛事"不能来自本地库 ——
    本地库里的赛事正是**上一次同步的结果**，第一次用的时候它是空的。
    所以这个函数绕过本地，直接问平台要列表。

    ## 返回的形状是**精简后**的，不是平台原样

    只留界面要用的几项（id / 名称 / 起止 / 状态原文）。理由与
    `client` 的"不做 Pydantic 建模"相反：那一条说的是**别人家的契约**
    不要建模；而这里是我们**自己**下发给前端的形状，它必须稳定 ——
    前端拿它渲染列表并回传 id，多一个字段就多一处会漂移的东西。

    ⚠️ 平台返回的字段名（`name` / `start_time` / `end_time` / `is_public` …）
    是用真 PAT 实测出来的，见 `_pick` 的多候选键写法。
    """
    async def _fetch() -> list[dict[str, Any]]:
        async with build_client_sync() as client:
            return await client.list_competitions()

    try:
        rows = _run(_fetch())
    except KernelError as exc:
        raise _translate(exc) from exc

    out: list[dict[str, Any]] = []
    for item in rows:
        remote_id = _pick(item, "id", "competition_id")
        if remote_id is None:
            continue
        out.append(
            {
                "remoteId": str(remote_id),
                "name": _pick(item, "name", "title") or f"#{remote_id}",
                "startAt": _pick(item, "start_time", "startAt", "start_at"),
                "endAt": _pick(item, "end_time", "endAt", "end_at"),
                "status": _pick(item, "status", "state") or "",
            }
        )
    return out


_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def extract_challenge_id(raw: str) -> str | None:
    """从用户粘进来的**任意文本**里抠出题目 id。

    ## 为什么是"任意文本"而不是"只接受纯 id"

    用户手上有的东西是**平台页面的地址栏**（或者从别处复制来的一段），
    让他自己把 id 从中间裁出来是在给他找活干 —— 而这事机器做更准。

    顺带一提：这个函数也顺手解决了"链接里带一堆 query 参数"的情况，
    因为正则只认 UUID 的形状。
    """
    match = _UUID_RE.search(raw or "")
    return match.group(0) if match else None


def pull_challenge(db: Session, *, raw_input: str) -> tuple[KernelChallenge, bool]:
    """按 id / 链接拉**一道**练习集题目。返回 `(题目, 是不是新建的)`。

    ## 为什么需要它（而不是继续等"列表接口"）

    实测过：CTF2 的开放 API **没有**"列出练习集题目"的接口
    （11 条候选路径全部 `404 resource=route`），而"按 id 取单题详情"
    **是活的**（编造 id 时返回 `resource=practice_challenge`，区别明确）。

    所以拿到题目的唯一可行路径是：**用户从平台页面上把题目链接复制过来**，
    我们按 id 精确拉取。这条路径一次一道，但它确定能work。

    ## 为什么要遍历练习集

    题目详情要 `(practice_id, challenge_id)` 两个 id，而用户给的链接里
    通常只有后者。练习集一共就几个（`MAX_SYNC_COMPETITIONS` 之内），
    逐个试比让用户再去复制一个 practice id 省事得多。
    """
    challenge_id = extract_challenge_id(raw_input)
    if not challenge_id:
        raise ApiError.validation(
            "没有从这段文本里认出题目 id。请把平台上的题目链接整条粘过来"
            "（形如 …/challenges/xxxxxxxx-xxxx-…），或者直接粘那段 UUID。",
            {"input": "未找到形如 UUID 的题目 id"},
        )

    account = get_account(db)
    if account is None:
        raise ApiError(409, "CTF2_NOT_BOUND", "尚未绑定 CTF2 账号。请先绑定一个 PAT。")
    token = decrypt_account_token(account)

    # 本地已知的练习集（同步过才有）——它们提供了 practice_id
    practices = [
        item
        for item in (
            db.execute(
                select(KernelCompetition)
                .where(KernelCompetition.kind == "practice")
                .order_by(KernelCompetition.synced_at.desc())
            )
            .scalars()
            .all()
        )
    ]
    if not practices:
        raise ApiError(
            409,
            "CTF2_NO_PRACTICE",
            "本地还没有任何练习集，无法判断这道题属于哪个练习集。请先点一次「同步题目」。",
        )

    async def _fetch() -> tuple[KernelCompetition | None, dict[str, Any] | None]:
        async with KernelClient(token=token) as client:
            for practice in practices[:MAX_SYNC_COMPETITIONS]:
                try:
                    detail = await client.get_practice_challenge(
                        practice.remote_id, challenge_id
                    )
                except KernelError as exc:
                    # ★ 逐集容错：某个练习集下没有这道题是**正常**的
                    #   （那就是"不属于它"），不该中断整次查找。
                    logger.debug(
                        "练习集 %s 下没有题目 %s：%s", practice.remote_id, challenge_id, exc
                    )
                    continue
                if detail:
                    return practice, detail
        return None, None

    try:
        competition, detail = _run(_fetch())
    except KernelError as exc:
        raise _translate(exc) from exc

    if competition is None or not detail:
        raise ApiError.not_found(
            f"在已知的 {len(practices)} 个练习集里都没有找到这道题"
            f"（{challenge_id}）。它可能属于一个还没同步过的练习集，"
            "或者链接里的 id 不是题目 id。"
        )

    challenge, created = _upsert_challenge(
        db, competition=competition, stage_id=None, payload=detail
    )
    db.commit()

    # ── 附件：光把清单存进 JSONB 是不够的 ──────────────────────
    #
    # `sync` 那条路径有这一步（`plan_downloads` + `download_all`，
    # 见本模块 580 行附近），而单题拉取这里**漏了**。
    #
    # 后果不是"少个文件"那么轻：解题沙箱是按
    # `_ctf2_attachments/<challenge_id>/` 挂载给容器的，目录不存在
    # 就等于模型**一个文件都看不到**。实测症状（2026-09-29，hashcat 题）：
    # 环境正常、沙箱起来了、模型跑满 8 轮、`candidates=0` —— 而平台侧
    # 一切正常，看起来像"模型太笨"，实际是它手上没有题。
    #
    # ⚠️ 与 `sync` 同一条边界：下载是**协程**，所以放在 `commit()` 之后，
    #    而且 `download_all` 只做网络与落盘、不碰 Session。
    pending = plan_downloads(challenge.id, challenge.attachments)
    if pending:
        try:
            _run(download_all(token, pending))
        except KernelError as exc:
            # 附件没下全**不该**把"题目已经进库"这件事回滚 ——
            # 重新拉一次即可（已存在的文件会跳过）。
            logger.warning("CTF2 单题拉取的附件下载失败：%s", exc)

    logger.info(
        "CTF2 单题拉取：%s（%s）→ %s",
        challenge.title,
        challenge_id,
        "新建" if created else "更新",
    )
    return challenge, created


def _upsert_competition(
    db: Session, *, account: KernelAccount, remote_id: str, kind: str, payload: dict[str, Any]
) -> KernelCompetition:
    """赛事/练习集的 upsert。

    ⚠️ 这里不显式加锁，但**唯一约束**必须在（`(account_id, remote_id)`）：
    两个并发同步会同时"查不到、然后各插一条"，而唯一约束让第二个失败 ——
    失败会被翻译成一次可重试的错误，比留下两条重复记录好。
    """
    existing = _find_competition(db, account.id, remote_id, kind)
    competition = existing or KernelCompetition(
        id=new_id("kncomp"),
        account_id=account.id,
        remote_id=remote_id,
        kind=kind,
        name=f"#{remote_id}",
    )
    if existing is None:
        db.add(competition)

    competition.name = _pick(payload, "name", "title") or competition.name or f"#{remote_id}"
    competition.remote_status = _pick(payload, "status", "state") or ""
    competition.start_at = _parse_time(_pick(payload, "start_at", "startAt", "start_time"))
    competition.end_at = _parse_time(_pick(payload, "end_at", "endAt", "end_time"))
    # 原始响应留一份：CTF2 有我们没建模的字段，排查时能看到"当时它说了什么"。
    competition.raw = payload if isinstance(payload, dict) else None
    competition.synced_at = now_utc()
    db.flush()
    return competition


def _find_competition(
    db: Session, account_id: str, remote_id: str, kind: str
) -> KernelCompetition | None:
    """按 `(account_id, remote_id)` 找赛事/练习集。

    ## ⚠️ 查询条件**必须与唯一键一致**（2026-10-11 修）

    唯一键是 `(account_id, remote_id)`（见 `models.KernelCompetition`
    的 `uq_ctf2_competitions_account_remote`），而这里原来还多带了一个
    `kind` 条件。两者的差别不是风格问题，它会**撞唯一约束**：

      同账号下若 practice 与 competition 的 `remote_id` 相同，
      带 `kind` 的查询会认为"没有记录"→ 走插入分支 → 数据库按
      唯一键拒绝 → 整次同步失败（而报错是 IntegrityError，
      看起来像"数据坏了"，不像"查询条件写窄了"）。

    而 `kind` 是**我们这边的分类**（`competition` / `practice`），
    不是平台的标识 —— 用分类去查一个按标识唯一的行，本身就说不通。

    ## `kind` 参数保留的用途：**更新**而不是**查找**

    找到之后由 `_upsert_competition` 用平台给的 kind 覆盖它：
    平台把一个东西从 practice 改成 competition 时我们要跟上，
    而不是因为"查不到"就去插一条新的（那会撞唯一键）。
    """
    return (
        db.execute(
            select(KernelCompetition).where(
                KernelCompetition.account_id == account_id,
                KernelCompetition.remote_id == remote_id,
            )
        )
        .scalars()
        .first()
    )


def _upsert_challenge(
    db: Session,
    *,
    competition: KernelCompetition,
    stage_id: str | None,
    payload: dict[str, Any],
) -> tuple[KernelChallenge, bool]:
    """题目的 upsert。返回 `(题目, 是不是新建的)`。

    ★ 为什么要把对象本身也返回：同步的第二段（附件下载）需要
    `challenge.id` 和净化后的附件清单，而它们是**这里**才算出来的。
    让调用方拿 remote_id 再查一次库能把签名保持干净，但那是
    "为了签名好看多一次查询" —— 而这里恰好已经有对象在手。

    ## ★ 同步**不动**解题相关的字段

    `status` / `flag` / `wrong_submission_count` / `sandbox_profile` /
    `scan_id` / `max_wrong_submissions` 一律不碰 —— 它们是**我们这边的
    状态**，平台不知道它们，也没有任何理由因为一次同步而被重置。

    这条很容易做错：用 `payload` 整体构造一个对象再覆盖，会把
    "我已经解出这道题"抹成"未解出"，而症状是用户第二天打开发现
    昨天解出的题又变回没解 —— 那会让人完全不敢相信这个系统。
    """
    remote_id = _pick(payload, "id", "challenge_id")
    if remote_id is None:
        # ⚠️ 返回 `None` 会让调用方每一处都要判空，而"少了一道题"是
        #    最难被发现的失败。这里选择**出声**而不是跳过：
        #    平台的响应形状变了意味着**所有**题都会没 id，继续走只会
        #    得到一句"同步成功 · 新增题目 0"，那比直接报错更难排查。
        #    没有 id 的题目在 `_fetch` 阶段就已经被挡掉了，走到这里
        #    说明形状真的变了。
        raise KernelError("平台返回的题目没有 id，同步中止（响应形状可能已变化）")

    existing = (
        db.execute(
            select(KernelChallenge).where(
                KernelChallenge.competition_id == competition.id,
                KernelChallenge.remote_id == str(remote_id),
            )
        )
        .scalars()
        .first()
    )

    challenge = existing or KernelChallenge(
        id=new_id("knchal"),
        competition_id=competition.id,
        remote_id=str(remote_id),
        status=KernelChallengeStatus.UNSOLVED.value,
        sandbox_profile=KernelSandboxProfile.BASE.value,
    )
    if existing is None:
        db.add(challenge)

    challenge.stage_id = stage_id or challenge.stage_id
    challenge.title = _pick(payload, "title", "name") or challenge.title or f"#{remote_id}"
    # 分类**照抄平台给的字符串**（大小写与措辞都不动）——
    # 镜像的选择在同步这一刻决定并落库，见 `_profile_from_payload`。
    challenge.category = _pick(payload, "category", "type") or challenge.category or ""
    challenge.description = (
        _pick(payload, "description", "content", "detail") or challenge.description
    )
    attachments = payload.get("attachments") or payload.get("files")
    challenge.attachments = (
        attachments if isinstance(attachments, list) else challenge.attachments
    )
    score = _pick(payload, "score", "points", "value")
    if score is not None:
        # 分值取不到就**留着原值**，不写 0。
        # 写 0 会让列表上出现"0 分"这种看起来像事实的东西，而它其实是
        # "平台给的值我们读不懂" —— 两件事混在一起就没法排查了。
        with contextlib.suppress(TypeError, ValueError):
            challenge.score = int(score)
    # ★ 套题位置：只在**我们还没记过**时写入（见 models 里那段说明 ——
    #   现取会让平台的改动转嫁成一次错误提交）。
    if not challenge.sub_flag_id:
        challenge.sub_flag_id = _pick(payload, "sub_flag_id", "subFlagId")

    if existing is None:
        challenge.sandbox_profile = _profile_for(challenge.category)
        challenge.max_wrong_submissions = DEFAULT_CTF2_MAX_WRONG_SUBMISSIONS

    db.flush()
    return challenge, existing is None


def _profile_for(category: str) -> str:
    """选沙箱镜像。**在同步时决定一次，不每次启动重算。**

    调用 `runner.profile_for_category` —— 那是**唯一**的分类→镜像规则
    （精确匹配，不是子串匹配；`forensics` 含 `re` 这个坑写在它的
    docstring 里）。这里再判一次等于第二份实现，而它们迟早会分叉。

    ⚠️ 延迟 import：`runner` 依赖本模块（在函数内 import），
    顶层互相 import 会得到一次循环导入。
    """
    from app.kernel import runner

    return runner.profile_for_category(category)


def _pick(payload: Any, *keys: str) -> str | None:
    """从平台返回的 dict 里取第一个非空的标量值。

    ⚠️ 与 `client._first_str`（那是个私有名）**刻意不共用**：那是
    "CTF2 的响应到底长什么样"这条**领域知识**的封装，复制它会有漂移
    风险；而这个函数只是一个取键的小工具，三行，没有会过期的知识。
    两种情况的取舍不同，所以这里的结论也不同。

    数字也要能取：平台的 id 可能是整数（`{"id": 1234}`），
    而我们的列是字符串。统一 `str()` 化省掉每次调用的转换。
    """
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if value is None or isinstance(value, (dict, list)):
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _parse_time(value: str | None) -> datetime | None:
    """平台给的时间 → tz-aware UTC。认不出就返回 None（**不猜**）。

    认不出时返回 None 而不是"当前时刻"：一个编造的开始时间会让
    "这场赛事结束了吗"这个判断给出错误答案，而 None 至少是诚实的。
    """
    if not value:
        return None
    try:
        return parse_iso(value)
    except ValueError:
        logger.warning("无法解析 CTF2 返回的时间：%r", value)
        return None


# ══════════════════════════════════════════════════════════
# 查询（给 router 用）
# ══════════════════════════════════════════════════════════


def list_competitions(db: Session, *, account_id: str | None = None) -> list[KernelCompetition]:
    stmt = select(KernelCompetition).order_by(KernelCompetition.synced_at.desc())
    if account_id:
        stmt = stmt.where(KernelCompetition.account_id == account_id)
    return list(db.execute(stmt).scalars())


def get_competition_or_404(db: Session, competition_id: str) -> KernelCompetition:
    competition = db.get(KernelCompetition, competition_id)
    if competition is None:
        raise ApiError.not_found("这个赛事")
    return competition


def list_challenges(
    db: Session, *, competition_id: str | None = None, status: str | None = None
) -> list[KernelChallenge]:
    stmt = select(KernelChallenge).order_by(
        KernelChallenge.created_at.desc(), KernelChallenge.id
    )
    if competition_id:
        stmt = stmt.where(KernelChallenge.competition_id == competition_id)
    if status:
        stmt = stmt.where(KernelChallenge.status == status)
    return list(db.execute(stmt).scalars())


def get_challenge_or_404(db: Session, challenge_id: str) -> KernelChallenge:
    challenge = db.get(KernelChallenge, challenge_id)
    if challenge is None:
        raise ApiError.not_found("这道题")
    return challenge


def latest_environment(db: Session, challenge_id: str) -> KernelEnvironment | None:
    """这道题最近一次的环境尝试。

    ⚠️ 取**最近一次**而不是"当前活着的那个"：起环境失败的那次记录
    才是排查的依据（见 models 的说明），而"活着的"通常是同一行。
    """
    return (
        db.execute(
            select(KernelEnvironment)
            .where(KernelEnvironment.challenge_id == challenge_id)
            .order_by(KernelEnvironment.created_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )


def list_environments(db: Session, *, challenge_ids: list[str]) -> list[KernelEnvironment]:
    """批量取环境（列表页用）。**一次查询**，不是每道题查一次。"""
    if not challenge_ids:
        return []
    return list(
        db.execute(
            select(KernelEnvironment)
            .where(KernelEnvironment.challenge_id.in_(challenge_ids))
            .order_by(KernelEnvironment.created_at.desc())
        )
        .scalars()
    )


# ══════════════════════════════════════════════════════════
# 环境（runner 依赖这三个函数）
# ══════════════════════════════════════════════════════════


@dataclass(slots=True)
class StartedEnvironment:
    """一次环境启动的结果。**字段名是 runner 依赖的接口。**"""

    environment_id: str
    access_url: str | None
    access_urls: list[str]
    polls: int = 0
    # ★ 平台给的接入类型（`http` / `nc` …）。runner 用它**纠正**按题型
    #   猜出来的网络隔离 —— 见 `client.EnvironmentAccess.access_type`。
    access_type: str = ""


def _remote_ids(db: Session, challenge_id: str) -> dict[str, Any]:
    """取远端 id：`(practice_id, remote_challenge_id, sub_flag_id)`。

    ⚠️ 用 `KernelCompetition.remote_id` 作为 `practice_id`：CTF2 的练习集
    与赛事是两套端点，但**提交与环境端点只挂在 practice 上**
    （见 client 的端点表）。所以赛事类的题目在这里取到的是赛事的
    remote_id，调用会 404，而错误信息会如实说"CTF2 拒绝了这次请求（404）"。

    这是**已知的功能缺口**，写在交付说明里而不是猜一个 id。
    """
    challenge = db.get(KernelChallenge, challenge_id)
    if challenge is None:
        return {}
    competition = db.get(KernelCompetition, challenge.competition_id)
    return {
        "practice_id": competition.remote_id if competition else "",
        "challenge_id": challenge.remote_id,
        "sub_flag_id": challenge.sub_flag_id,
    }


def _remote_ids_fresh(challenge_id: str) -> dict[str, Any]:
    """同上，但自己开会话（异步路径用）。"""
    with session_scope() as db:
        return _remote_ids(db, challenge_id)


async def platform_reports_solved(challenge_id: str) -> bool:
    """问平台一句：**这道题本队伍是不是已经解出了？**

    ## ★ 为什么需要这个探测（它挡的是一类会花钱的错）

    平台侧的 `is_solved` 与我们的 `status` 回答的是**两个不同的问题**
    （见 `vocab.KernelChallengeStatus` 的说明）：「平台做没做出来」对
    「我们的流水线走到哪一步」。我们**刻意**没有把两者合成一个字段 ——
    代价是：一道队友在网页端解出的题，在我们库里仍然是 `unsolved`，
    于是它**是可解的**。

    而「可解」在这里意味着真的会去解：烧掉一整轮 LLM 调用（几十次
    工具调用），然后提交时撞上平台的

        HTTP 400 {"error": {"code": "ALREADY_SOLVED"}}

    —— 实测（2026-10-01，hashcat）确认平台就是这么答的。

    `vocab.RESOLVABLE_CTF2_CHALLENGE_STATUSES` 把 `solved` 排除在外，
    理由写的正是"重复提交要计入错误次数，是在拿用户账号冒险"。本条
    探测就是把**同一个理由**用在"平台说它已解出"这件事上。

    ## 为什么是**现问**而不是同步时落库

    同步是周期性的，而"队友刚刚在网页端交了"可以发生在两次同步之间。
    落库的布尔值会**过期**，而过期的安全判断等于没有。起点这一次查询
    的成本是一个请求，换来的是"永远不会白烧一轮解题"。

    ## 失败时**不阻断**（fail-open）

    探测本身出错（网络抖、平台 5xx）时抛出去，由调用方决定继续解题 ——
    它是安全网，不是必经之路。真正的兜底在提交那一步：识别
    `ALREADY_SOLVED` 并**不把题目打成失败**（见 `runner.submit_candidate`）。
    """
    remote = _remote_ids_fresh(challenge_id)
    if not remote.get("practice_id") or not remote.get("challenge_id"):
        # 没有远端标识说明同步没完成，这**不是**"已解出"——不能拿它当
        # 阻断理由（那会让一道根本没同步好的题永远解不了）。
        return False

    client = build_client_sync()
    async with client:
        try:
            detail = await client.get_practice_challenge(
                practice_id=str(remote["practice_id"]),
                challenge_id=str(remote["challenge_id"]),
            )
        except KernelResponseError as exc:
            # 404 = 这条路走不通（例如这题其实属于某个赛事 stage，而不是
            # 练习中心）→ **问不出来**，不是"已解出"。返回 False 让它
            # 照常解题：把"问不出来"当成"已解出"会让一批题永远解不了。
            if exc.status_code == 404:
                logger.info(
                    "平台状态探测不适用（404），按未解出继续：%s", challenge_id
                )
                return False
            raise
    return detail.get("is_solved") is True


async def start_environment_for(client: KernelClient, challenge_id: str) -> StartedEnvironment:
    """起题目环境并落库。**worker 专用**（见模块 docstring 的例外说明）。"""
    remote = _remote_ids_fresh(challenge_id)
    if not remote.get("practice_id") or not remote.get("challenge_id"):
        raise KernelError("这道题缺少远端标识，无法启动环境（同步可能没完成）。")

    # ① 先落一行 `starting`：起环境可能要轮询几分钟，而这几分钟里
    #    用户必须能在界面上看到"正在起环境"—— 没有这行的话，
    #    他点了解题之后看到的是一片空白。
    environment_id = new_id("knenv")
    with session_scope() as db:
        challenge = db.get(KernelChallenge, challenge_id)
        if challenge is not None:
            db.add(
                KernelEnvironment(
                    id=environment_id,
                    challenge_id=challenge_id,
                    status=KernelEnvironmentStatus.STARTING.value,
                    sandbox_profile=challenge.sandbox_profile,
                    started_at=now_utc(),
                )
            )
            challenge.status = KernelChallengeStatus.SOLVING.value
            challenge.last_error = None
        db.commit()

    # ② 幂等轮询（幂等 + 三个上限都在 client 那一侧）
    try:
        access: EnvironmentAccess = await client.start_environment_until_ready(
            practice_id=str(remote["practice_id"]),
            challenge_id=str(remote["challenge_id"]),
        )
    except KernelEnvironmentTimeout as exc:
        _fail_environment(environment_id, challenge_id, exc.message, polls=exc.polls)
        raise
    except KernelError as exc:
        _fail_environment(environment_id, challenge_id, exc.message, polls=0)
        raise

    # ③ 就绪 → 回写地址。
    #
    # ⚠️ 数据库上有一条 `access_url_only_when_ready` 约束：
    #    非 ready 状态不许有 access_url。所以"写地址"与"改状态"必须
    #    **在同一个事务里**发生，不能先写 url 再改状态 ——
    #    中间那一下会撞约束，而报错看起来像"字段写错了"。
    urls = list(access.access_urls) or ([access.access_url] if access.access_url else [])
    with session_scope() as db:
        environment = db.get(KernelEnvironment, environment_id)
        polls = 0
        if environment is not None:
            environment.status = KernelEnvironmentStatus.READY.value
            environment.access_url = access.access_url
            environment.access_urls = urls
            environment.ready_at = now_utc()
            # ★ 写**真实**的轮询次数。
            #
            # 原来是 `int(environment.poll_count or 0) + 1` —— 建行时是 0，
            # 这里加一，所以**永远显示"轮询 1 次"**，哪怕实际轮询了 2 次、
            # 20 次。界面上那句话是假的，而它恰恰是用户判断
            # "环境起得快不快"的唯一依据。
            #
            # 真实的次数一直算在 `client.start_environment_until_ready`
            # 里（它的 `polls` 局部变量），只是从来没被返回出来。
            environment.poll_count = int(access.polls or 0)
            polls = environment.poll_count
            # ★ 租期。平台给的是 ISO 串（实测默认 1 小时）。
            #   列一直存在但**从来没被填过** —— 于是"环境什么时候到期"
            #   在界面上和库里的答案都是"不知道"。
            environment.expires_at = _parse_time(access.expires_at)
        challenge = db.get(KernelChallenge, challenge_id)
        if challenge is not None:
            challenge.status = KernelChallengeStatus.ENVIRONMENT_READY.value
            ctf2_events.emit_environment_ready(
                db, challenge, access_urls=urls, polls=polls
            )
        db.commit()

    return StartedEnvironment(
        environment_id=environment_id,
        access_url=access.access_url,
        access_urls=urls,
        polls=polls,
        access_type=access.access_type,
    )


async def release_environment(
    client: KernelClient, challenge_id: str, *, mark_failed: bool = True
) -> None:
    """释放平台环境 + 把本地环境行标成 stopped。**不抛异常。**

    ★ 它在 `runner.run_worker` 的 `finally` 里被调用，所以**绝不能抛**：
    一个抛出会盖住真正的异常（用户看到"释放失败"，而真实原因是解题崩了）。

    ## ★ `mark_failed`：状态该由谁决定

    本函数有两条调用路径，它们的语义**不一样**：

    | 路径 | `mark_failed` | 为什么 |
    | --- | --- | --- |
    | 用户点「释放环境」（`release_environment_sync`） | `True` | 用户主动中断 → 这题确实没跑完 |
    | worker 的 `finally` 收尾 | `False` | 状态由 `_record_outcome` 按**解题结果**决定 |

    实测（2026-10-01，BUUCTF「Url编码」）踩到的坑：worker 那条路径也走了
    默认的 `True`，而它跑在 `_record_outcome` **之前**，于是：

    ① 这里先把状态写成 `failed`、`last_error = "环境已释放。"`；
    ② 随后的 `_record_outcome` 发现 `status` 已经不是 `solving` 了，
       于是两个分支都不进 —— **真正的原因从未被记录到任何地方**。

    界面上因此只显示一句什么都没解释的「环境已释放。」，而真相是
    「模型给出的候选 flag 被本地格式校验丢弃了」。用户拿着那句话
    完全无从下手。

    ## ★ 捕获面必须是 `Exception`，不能只捕 `KernelError`

    实测（2026-09-30）：一次真实的端到端里，题目**已经解出、flag 已提交
    并被平台判定正确**（`status=correct`、`confirmed_flag` 已落库），
    然后整个 worker 崩在这一步：

        release_environment → stop_environment → _request
        RuntimeError: Cannot send a request, as the client has been closed.

    `RuntimeError` 不是 `KernelError`（它是 httpx 在客户端已关闭后抛的），
    于是穿透了下面那个 `except KernelError` —— 收尾变成了崩溃，而用户
    看到的是"解题失败"，实际上那一刻**结果早就落库了**。

    这正是本 docstring 第一段警告的情形，只是漏掉的异常类型不在预期里。
    既然承诺是"绝不能抛"，捕获面就得与它一致：`Exception`。
    （`KeyboardInterrupt` / `SystemExit` / `asyncio.CancelledError` 都**不是**
    `Exception` 的子类，所以放宽到 `Exception` 不会把它们吞掉。）
    """
    # ★★ 整个函数体都必须在捕获面之内（2026-10-10 修）。
    #
    # ## 原来漏在哪
    #
    # 第一行 `_remote_ids_fresh(challenge_id)` 在 `try` **之外**，而它内部
    # 开了一个 `session_scope()` —— 数据库抖动 / 连接池耗尽时它抛的是
    # SQLAlchemy 异常，**穿透**了这个承诺"绝不抛"的函数。
    #
    # ## 后果有两层，第二层更贵
    #
    # 1. 它在 `finally` 里被调用，所以这个异常会**盖住 worker 的原始异常**
    #    —— 正是本 docstring 第一段要防的形态（"一个抛出会盖住真正的异常"）；
    # 2. `_run_worker` 的收尾段（`_record_outcome` + `finalize_solve_scan`）
    #    **永远不会执行** → 题目状态与解题 scan 都停在运行态。
    #
    # 也就是说：一次数据库抖动会让一次已经跑完的解题**丢掉结果**。
    #
    # ## 为什么用一个大 try 包住整段，而不是只把首行挪进去
    #
    # 函数里有两处会话访问（`_remote_ids_fresh` 与下面那个 `session_scope`）
    # 与一次网络调用。它们每一个都可能抛，而**承诺是整段不抛** ——
    # 逐个去包等于每加一行就要重新论证一次捕获面，迟早漏。
    # 包整段的代价是缩进变深，换来的是"这条承诺不再依赖读者逐行检查"。
    try:
        remote = _remote_ids_fresh(challenge_id)
        if remote.get("practice_id") and remote.get("challenge_id"):
            try:
                await client.stop_environment(
                    practice_id=str(remote["practice_id"]),
                    challenge_id=str(remote["challenge_id"]),
                )
            except KernelError as exc:
                # 平台侧失败只记日志：本地状态照旧推进 —— 否则那道题会永远
                # 停在"环境就绪"，而用户点"释放环境"永远没有反应。
                logger.warning("停 CTF2 题目环境失败（本地状态照旧推进）：%s", exc.message)
            except Exception as exc:  # noqa: BLE001 - 见 docstring：收尾绝不抛
                logger.warning(
                    "停 CTF2 题目环境时出现非领域异常（本地状态照旧推进）：%s: %s",
                    type(exc).__name__,
                    exc,
                )

        with session_scope() as db:
            environment = (
                db.execute(
                    select(KernelEnvironment)
                    .where(
                        KernelEnvironment.challenge_id == challenge_id,
                        KernelEnvironment.status == KernelEnvironmentStatus.READY.value,
                    )
                    .order_by(KernelEnvironment.created_at.desc())
                    .limit(1)
                )
                .scalars()
                .first()
            )
            if environment is not None:
                environment.status = KernelEnvironmentStatus.STOPPED.value
                environment.stopped_at = now_utc()
                # ★ 必须清 access_url：`access_url_only_when_ready` 约束要求
                #   非 ready 状态没有地址。而且从语义上说，停掉的环境地址
                #   就是死的 —— 留着它会让界面上出现一个点不开的链接。
                environment.access_url = None
            challenge = db.get(KernelChallenge, challenge_id)
            # ⚠️ 只有 `mark_failed=True`（用户主动释放）才在这里判 failed ——
            #    worker 收尾那条路径**必须**把状态留给 `_record_outcome`，
            #    否则真实原因会被"环境已释放。"盖掉。见本函数的 docstring。
            if (
                mark_failed
                and challenge is not None
                and challenge.status
                in (
                    KernelChallengeStatus.ENVIRONMENT_READY.value,
                    KernelChallengeStatus.SOLVING.value,
                )
            ):
                challenge.status = KernelChallengeStatus.FAILED.value
                challenge.last_error = "环境已释放。"
            db.commit()
    except Exception as exc:  # noqa: BLE001 - 见上面那段：整段承诺不抛
        # 走到这里说明连"本地状态推进"都没做成（数据库层故障）。
        # **仍然不抛** —— 本函数的承诺是收尾绝不抛，而调用点在 `finally` 里：
        # 那里抛出的异常会盖住 worker 的原始异常，让真正的失败原因消失。
        logger.warning(
            "释放 CTF2 环境时出错（本地状态可能未推进）：%s: %s",
            type(exc).__name__,
            exc,
        )


def release_environment_sync(
    db: Session, challenge_id: str, *, mark_failed: bool = True
) -> None:
    """给 router 用的同步外观。

    ★ 存在的理由：router **不该 import `_run`**（那是个私有名，
    而且它把"asyncio.run 的用法约束"泄漏到了调用层）。这里把它
    收进 service —— `_run` 的使用面越小，那条"不能改成 async def"
    的约束就越难被无意破坏。

    ⚠️ 默认 `mark_failed=True`：router 那条路径是**用户主动点「释放环境」**，
    那时把题判成 failed 是对的（他确实中断了）。而 worker 的收尾走
    `release_environment(..., mark_failed=False)` —— 见那边的说明。
    """
    del db  # 会话由 release_environment 自己开（它在 worker/请求两条路径上都被用）
    client = build_client_sync()
    try:
        _run(release_environment(client, challenge_id, mark_failed=mark_failed))
    except KernelError as exc:
        raise _translate(exc) from exc


def expire_stale_environments(db: Session) -> int:
    """把**已经过期或明显陈旧**的 ready 环境标成 `expired`，返回改了几条。

    ## 为什么必须有（v0.40）

    `KernelEnvironmentStatus.EXPIRED` 从定义那天起就**没有任何一处设置它**
    —— 全仓 `app/` 零处。于是 `expires_at` 过去之后，行还停在 `ready`，
    而 `access_url` 也还留着。

    前端 `EnvironmentStatus.tsx` 的规则是"`accessUrl` 只在 `status ===
    'ready'` 时渲染"，于是界面上出现一个标着「就绪」的链接，点进去是
    连接断开 —— 用户以为是我们把环境弄坏了，实际是它按租期正常到期了。

    真库里实测有 2 条 `status='ready' and expires_at < now()`，其中
    一条已过期 15 天。

    ## 两条判据，与它们各自能证明的东西

    | 判据 | 情况 | 能证明 |
    | --- | --- | --- |
    | ① `expires_at` 已过 | 平台明确给了租期，且租期到了 | **确实过期** |
    | ② `expires_at` 为空、`ready_at` 早于 24 小时 | 我们不知道租期，但它明显陈旧 | **只是陈旧**，不是过期 |

    ② 是必要的：`expires_at` 可空（写回租期是后来才补的），那批早先的
    环境**永远不会**满足判据 ①。真库里这种行有 2 条，`container_id`
    全 NULL。阈值 24 小时的取法与理由见
    `STALE_ENVIRONMENT_WITHOUT_TTL_HOURS`。

    ## ★ 状态与本函数**不**碰的东西

    - `status` 改成 `expired` 与 `access_url`（连同 `access_urls`）清空
      **必须在同一个事务里**：`access_url_only_when_ready` 约束要求
      非 ready 不许有 url，中间那一下会直接撞约束。
    - ⚠️ **不动 `challenge.status`**：这道题解没解出、是不是还在排队，
      与环境到期是两件事。真库里卡在 `environment_ready`/`solving`
      的题是 0 条，没有"顺手修一下"的理由 —— 而顺手改一个状态机字段
      是没有测试守得住的改动。
    - ⚠️ 也**不动 `container_id`**：那是排查残留容器的线索。要清容器
      归 `sweep_orphan_sandboxes` 管，两件事别搅在一起。

    ## 边界（说清楚它证不了什么）

    能证：库里的过期行会转成 `expired`，界面上那个死链接会消失。
    **不能证**平台真的会遵守它给的 `expires_at` —— 我们只是按**自己的
    记录**判断，平台提前回收或延长租期，这里都不知道。
    """
    moment = now_utc()
    stale_before = moment - timedelta(hours=STALE_ENVIRONMENT_WITHOUT_TTL_HOURS)

    targets = (
        db.execute(
            select(KernelEnvironment).where(
                KernelEnvironment.status == KernelEnvironmentStatus.READY.value,
                # ① 明确了租期且已过
                (KernelEnvironment.expires_at < moment)
                # ② 没有租期信息，但就绪时刻已明显陈旧
                | (
                    KernelEnvironment.expires_at.is_(None)
                    & KernelEnvironment.ready_at.is_not(None)
                    & (KernelEnvironment.ready_at < stale_before)
                )
            )
        )
        .scalars()
        .all()
    )
    for environment in targets:
        environment.status = KernelEnvironmentStatus.EXPIRED.value
        # ★ 与改状态同一个事务、同一个循环体：约束要求非 ready 不得有地址。
        environment.access_url = None
        # ⚠️ `access_urls` 也要清。`release_environment` 只清了单数那个，
        #    那是历史遗留；这里两处都清，否则"全部入口"里还留着死地址，
        #    而列表接口读的正是复数那一列。
        environment.access_urls = None
    if targets:
        db.commit()
        logger.info("把 %d 个过期/陈旧的 CTF2 环境标成了 expired", len(targets))
    return len(targets)


def _fail_environment(
    environment_id: str, challenge_id: str, reason: str, *, polls: int
) -> None:
    with session_scope() as db:
        environment = db.get(KernelEnvironment, environment_id)
        if environment is not None:
            environment.status = KernelEnvironmentStatus.ERROR.value
            environment.last_error = strip_nul(reason)[:500]
            environment.poll_count = max(int(environment.poll_count or 0), polls)
            # 非 ready 状态不该有地址（约束 + 语义双重要求）
            environment.access_url = None
        challenge = db.get(KernelChallenge, challenge_id)
        if challenge is not None:
            challenge.status = KernelChallengeStatus.FAILED.value
            challenge.last_error = strip_nul(reason)[:500]
            ctf2_events.emit_abandoned(db, challenge, reason=reason)
        db.commit()


# ══════════════════════════════════════════════════════════
# 解题受理 / 取消
# ══════════════════════════════════════════════════════════


def start_solve(
    db: Session,
    challenge: KernelChallenge,
    *,
    mode: str | None = None,
    sandbox_profile: str | None = None,
    max_wrong_submissions: int | None = None,
    brief: str | None = None,
    resume: bool = False,
) -> KernelChallenge:
    """受理一次解题：校验 → **落策略快照** → 入队。

    ## ★ 校验三件事，每一件都挡掉一类真实的浪费

    1. **状态可解**（`RESOLVABLE_CTF2_CHALLENGE_STATUSES`）。
       已经解出的题再解一次没有意义，而允许它会让"重复提交同一个 flag"
       变成一条可走通的路径 —— CTF 平台的重复提交是计入错误次数的。
    2. **有账号**。没有凭据就起不了环境，而"受理之后才发现"意味着
       用户白等一轮。
    3. **额度参数合法**。它是自动提交的刹车片，配错等于拆掉边界。

    ## 为什么档位由 service 兜住而不是只靠 schema

    schema 挡的是 HTTP 入参。而这个函数也会被内部调用（将来的 CLI、
    或从"重解一次"按钮进来的另一条路径）。真正要紧的是**两条路都不
    静默截断** —— 把用户填的 99 悄悄夹成 10 并返回成功，
    他下次会看到一个自己没设过的值。

    ## ★★ 为什么策略必须在这里**快照**（2026-10-11 修）

    这个函数原来是 `del mode` —— 收下策略参数后直接丢弃，理由是
    "策略是一次解题的输入，不是题目的属性，worker 跑的时候现读设置"。
    **前半个判断是对的，后半个是错的**：worker 现读的是**那时候**的
    全局设置，而受理与真正开跑之间隔着一段排队时间（并发上限 3）。

    后果不是"参数没生效"这么轻，它是一条**时序漏洞**：

      · 用户点解题时选 `safe`（明确表示"我要人工确认"）；
      · 题目排队等待；
      · 这期间任何人把全局改成 `auto`；
      · worker 启动时读到 `auto` —— **系统替用户做了一个他明确
        拒绝过的对外提交**，消耗的是**不可再生**的错误次数额度。

    同一个坑还吃掉了 `brief`（人工线索）与 `max_turns`（回合预算）。

    所以现在把"这一次尝试的输入"落成一行 `KernelSolveAttempt`，
    由 worker 读取。写进去就不再改 —— 要换策略就再受理一次。
    """
    settings_row = effective_ctf2_settings(db)
    _assert_resolvable(challenge)
    # ★ 平台能力断言排在状态断言之后：先告诉用户"这道题现在能不能解"，
    #   再说"平台根本不给这条路" —— 顺序反过来时，一道已解出的赛事题
    #   会得到"平台不支持"的提示，而那不是他该关心的问题。
    _assert_submission_supported(db, challenge)

    if _requires_platform(db, challenge) and get_account(db) is None:
        raise ApiError(409, "CTF2_NOT_BOUND", "尚未绑定 CTF2 账号。请先绑定一个 PAT。")
    _assert_llm_ready(db)

    if max_wrong_submissions is not None:
        _assert_wrong_ceiling(max_wrong_submissions)
        challenge.max_wrong_submissions = max_wrong_submissions

    if sandbox_profile:
        if sandbox_profile not in {p.value for p in KernelSandboxProfile}:
            raise ApiError.validation(
                "未知的沙箱镜像。", {"sandboxProfile": "取值不在允许范围内"}
            )
        # ★ v0.65：把历史镜像名（`ctf2-sandbox*`）规范成新名后再存 —— 写入侧
        #   只写新名，旧名只作为读取侧的兼容。校验放行旧名（否则一批老客户端
        #   传旧值会被拒），但落库的是新名。
        resolved_profile = normalize_sandbox_profile(sandbox_profile)
    else:
        resolved_profile = challenge.sandbox_profile or settings_row.default_sandbox_profile
    challenge.sandbox_profile = resolved_profile

    # ── 解析出本次尝试的策略（**受理时定下，之后不再变**）──────
    #
    # 三个值都遵循同一条规则：**显式给了就用给的，没给才取当下的全局值**。
    # 关键在于"取全局值"这件事**只发生在这里**（受理时刻），
    # 而不是在 worker 里 —— 那正是这条修复的全部意义。
    resolved_mode = mode or settings_row.submit_mode
    resolved_turns = int(settings_row.max_turns or DEFAULT_CTF2_MAX_TURNS)
    # brief 与 `scans.brief` 同一条口径：上限 8000，且不进命令行。
    # 空串归一成 None：`""` 与"没填"在语义上是同一件事，
    # 而存一个空串会让界面显示一个空白的"人工提示"区块。
    cleaned_brief = (brief or "").strip() or None

    challenge.status = KernelChallengeStatus.SOLVING.value
    # ★ 重新解题时**旧候选作废**。`candidate_ready` 是可以再解的（见
    #   `RESOLVABLE_CTF2_CHALLENGE_STATUSES`），而留着上一轮的候选会让
    #   界面在"求解中"的同时显示一个 flag —— 用户无法判断它还算不算数。
    #   判定历史不会因此丢失：它在 `ctf2_submissions` 里。
    challenge.candidate_flag = None
    challenge.last_error = None

    attempt = KernelSolveAttempt(
        id=new_id("kntry"),
        challenge_id=challenge.id,
        mode=resolved_mode,
        max_turns=resolved_turns,
        sandbox_profile=resolved_profile,
        brief=cleaned_brief,
        # 模型也一起快照：provider 配置会被改（换模型、删掉再建），
        # 而"这次尝试用的到底是哪个"必须与当时一致 —— 否则复盘时
        # 看到的模型可能已经不是当时那个了。
        provider_id=_snapshot_provider_id(db),
        llm_model=_snapshot_provider_model(db),
        resume=resume,
    )
    db.add(attempt)
    db.commit()
    db.refresh(attempt)

    ctf2_events.emit_challenge_discovered(db, challenge)
    db.commit()

    _enqueue(challenge.id, attempt_id=attempt.id)
    return challenge


def _snapshot_provider_id(db: Session) -> str | None:
    """受理时记下这次会用哪个 provider。没有就返回 `None`。

    ⚠️ 与 `runner.build_spec` 取 provider 的规则必须**一致**
    （`is_default DESC, created_at ASC` 的第一条）—— 否则快照里记的
    与实际用的会是两个。这条一致性由 `test_solve_attempt.py` 里那条
    "快照的模型与 build_spec 选中的一致"钉住。
    """
    from app.providers.models import ProviderAccount

    row = (
        db.execute(
            select(ProviderAccount).order_by(
                ProviderAccount.is_default.desc(), ProviderAccount.created_at
            )
        )
        .scalars()
        .first()
    )
    return row.id if row is not None else None


def _snapshot_provider_model(db: Session) -> str | None:
    """同 `_snapshot_provider_id`，取的是模型名。"""
    from app.providers.models import ProviderAccount

    row = (
        db.execute(
            select(ProviderAccount).order_by(
                ProviderAccount.is_default.desc(), ProviderAccount.created_at
            )
        )
        .scalars()
        .first()
    )
    return row.model if row is not None else None


#: 哪些 competition kind 的题目**允许**走"解题/提交"这条路。
#:
#: ## ★ 这个集合与 `_NO_PLATFORM_KINDS` 是**两个问题**，不能合并
#:
#: | 问题 | 判据 | 集合 |
#: | --- | --- | --- |
#: | 这题要不要绑 CTF2 账号？ | 会不会拿凭据去打平台 | `_NO_PLATFORM_KINDS` |
#: | 平台给不给这题提交/起环境的接口？ | 平台的端点表长什么样 | 本集合 |
#:
#: `practice` 是**两者都为"是"**：它有平台账号（拉题用），也有提交端点。
#: `local` 是**两者都为"否"**：没账号、也没端点。
#: `competition`（赛事）是"要账号、**没有**端点"—— 它卡在本集合上。
#:
#: ★ 把本集合写成 `_NO_PLATFORM_KINDS` 是一个**真实发生过的回归**：
#:   那次改动让 `practice` 从"放行"变成"拒绝"，于是**所有练习集题目
#:   都无法解题**，报错说"平台没开放赛事题目的提交接口"。而它被
#:   `test_platform_challenge_still_needs_account` 抓住 —— 那条测试
#:   本来要守的是"平台题照旧要求绑账号"，结果撞在一个更严重的问题上。
#:   两个概念共用一个名字时，"改了 A 也改了 B"这件事不会报错。
_ALLOWED_SOLVE_KINDS: Final[frozenset[str]] = frozenset(
    {KernelCompetitionKind.PRACTICE, KernelCompetitionKind.LOCAL}
)

#: 哪些 competition kind **不需要** CTF2 平台账号。
#:
#: 目前只有 `local`：本地复盘案例（v0.43）的题目与附件都在本机，没有远端、
#: 不用凭据、也不从平台拉取。`practice` 与 `competition` 都是平台镜像，
#: 必须有账号 —— 那是拉取时用的凭据，不是可选的装饰。
#:
#: ⚠️ 认不出的 kind **不在**这个集合里，于是它仍然走"必须有账号"那条路，
#:    并在 `_assert_submission_supported` 那里被 fail-closed 拒绝。也就是说
#:    新加一个平台类 kind 却忘了改这里时，症状是"要求绑账号"（烦但安全），
#:    而不是"绕过账号检查"。
_NO_PLATFORM_KINDS: Final[frozenset[str]] = frozenset({KernelCompetitionKind.LOCAL})


def _requires_platform(db: Session, challenge: KernelChallenge) -> bool:
    """解这道题是否必须先绑一个 CTF2 账号。

    ## 为什么要有这个函数

    `start_solve` 里原来是一句无条件的 `get_account(db) is None → 409`。
    对平台镜像的题那是对的（要拉题、要起环境、要提交 flag），但对
    **本地复盘案例**（`kind='local'`）它是纯粹的阻碍：那批证据包是本机
    上传的，整个流程一次都不会碰到 CTF2 的域名。

    用户此时看到的是「尚未绑定 CTF2 账号。请先绑定一个 PAT。」——
    而他要做的是传一批日志让智能体复盘，跟 CTF2 账号毫无关系。
    这类错误信息最坏的地方不是拦住了操作，是**指错了方向**。

    ## 查不到赛事行时返回 True

    外键（`ondelete=CASCADE`）保证赛事行平时一定在，真查不到说明数据坏了。
    这时按"需要平台"处理是安全方向：多要一个账号不会造成错的后果，
    而放过一个本该有账号的题会让 worker 在后面某个位置拼出一个空凭据
    去打平台，报错与真实原因相距很远。

    ⚠️ 与 `_assert_submission_supported` 的"查不到就放行"方向**相反**，
       两处的理由也不同：那边的放行是为了不让一道练习集的题突然解不了
       （它的失败会在真正调用平台时如实报出来），这边的收紧是因为
       接下来要拿凭据去拼 client，而拼不出来时报错会更难懂。
    """
    competition = db.get(KernelCompetition, challenge.competition_id)
    if competition is None:
        return True
    return competition.kind not in _NO_PLATFORM_KINDS


def _assert_resolvable(challenge: KernelChallenge) -> None:
    """这道题现在能不能解。不能就抛**说清原因**的 409。

    ⚠️ 用 409 而不是 400：这不是"请求写错了"，是"当前状态下不能做"。
    前端对两者的处理不同 —— 400 提示改输入，409 提示等状态变化
    （或者去把上一次的解题取消掉）。
    """
    if challenge.status == KernelChallengeStatus.SOLVED.value:
        raise ApiError.conflict(
            "CTF2_ALREADY_SOLVED", "这道题已经解出并确认过了，不需要再解一次。"
        )
    if challenge.status in (
        KernelChallengeStatus.SOLVING.value,
        KernelChallengeStatus.SUBMITTING.value,
    ):
        raise ApiError.conflict(
            "CTF2_ALREADY_RUNNING", "这道题正在处理中。请等它结束，或先取消这一次。"
        )
    if challenge.status not in RESOLVABLE_CTF2_CHALLENGE_STATUSES:
        raise ApiError.conflict(
            "CTF2_NOT_RESOLVABLE", f"当前状态（{challenge.status}）下无法开始解题。"
        )


def _assert_submission_supported(db: Session, challenge: KernelChallenge) -> None:
    """这道题的提交/环境接口在平台的开放 API 里**到底存不存在**。

    ## ★ 这条断言是实测出来的，不是防御性编程

    对 `https://ctf2.dasctf.com/api/open/v1` 逐条探测过（未鉴权请求：
    401/400 说明路由存在，`404 + "resource":"route"` 说明路由不存在）：

    * **存在**：`GET practice/`、`GET practice/{id}/challenges/{cid}/`、
      `POST practice/{id}/challenges/{cid}/submit/`、
      `POST practice/{id}/challenges/{cid}/environment/start/`、
      `GET competitions/`、`GET competitions/{id}/status/`、
      `GET competitions/{id}/stages/`、`GET stages/{id}/challenges/`、
      `GET stages/{id}/submissions/`；
    * **不存在**（全部 404）：`competitions/{id}/challenges/{cid}/submit/`、
      `competitions/{id}/submit/`、`stages/{sid}/challenges/{cid}/submit/`、
      `stages/{id}/submit/`、`competitions/{id}/challenges/`、
      `competitions/{id}/challenges/{cid}/environment/start/`、
      `stages/{sid}/challenges/{cid}/environment/start/`、
      `competitions/{id}/environment/start/`、`practice/{id}/`、
      `practice/{id}/challenges/`。

    结论：**赛事（`kind='competition'`）下的题目可以同步、可以展示，
    但开放接口没有给出提交 flag 与起环境的路子。** 所以在这里明确
    拒绝，而不是发一个注定 404 的请求。

    ⛔ 为什么不猜一条对称的路径（照着 `practice/...` 的形状写
    `stages/{sid}/challenges/{cid}/submit/`）：猜中的概率低，而
    **猜错的代价不对称** —— 最坏的情况是我们把一次没送达的提交
    记成"已提交"，用户据此以为这题交过了。给一句"平台没开放"
    是难听但真的话。

    ⚠️ 平台将来补齐了这些接口，本函数与它的两个调用点一起删掉即可。
    它是一条**有出处的断言**（探测日期、探测方法、结论），不是
    一个永久的架构假设。
    """
    competition = db.get(KernelCompetition, challenge.competition_id)
    if competition is None or competition.kind in _ALLOWED_SOLVE_KINDS:
        # ⚠️ 查不到赛事行时**放行**：外键保证它平时一定在
        #    （`ondelete=CASCADE` 会连带删题），所以这个分支真出现时
        #    说明数据坏了 —— 而"因为数据坏了就拒绝"会让一道练习集的题
        #    突然解不了。真正的失败会在下面调用平台时如实报出来。
        #
        # ★ `local` 也在这里（v0.43）：本地复盘案例**根本没有远端**，
        #    "平台有没有开放提交接口"这个问题对它不成立。放行它不是
        #    把检查放松了 —— 下面那条 fail-closed 分支的判据一字未变，
        #    它仍然拒绝所有认不出的 kind。
        #
        # ★ 反过来，**认不出的 kind 一律拒绝**（fail-closed）：
        #    未知 kind 意味着我们不知道它有没有提交端点，而猜一条路径
        #    去提交，最坏的结果是把一次没送达的提交记成"已提交"。
        #    两个分支方向相反，因为一个是数据坏了、一个是契约变了。
        return
    raise ApiError(
        409,
        "CTF2_COMPETITION_SUBMIT_UNSUPPORTED",
        "CTF2 的开放接口没有提供赛事题目的提交与起环境接口"
        "（实测这些路径全部返回 404），目前只支持练习集下的题目解题与提交。"
        "赛事题目仍可同步与查看。",
    )


def learn_from_solve(db: Session, challenge: KernelChallenge, outcome) -> None:  # noqa: ANN001
    """把一次解题提炼成**方法卡**（v0.32）。**失败只记日志，不抛。**

    ## ★ 唯一硬约束：卡片里绝不能有 flag

    卡片会被注入到**另一道题**的 prompt。带上旧 flag 就变成
    "把 A 题的答案泄漏到 B 题的上下文里"，而 B 题的模型完全可能
    把它当成 B 题的答案交上去。

    所以提炼走 `notes.build_note_fields`，它对每个字段都做
    **白名单式脱敏**（剔除 flag 形状、URL、长随机串）。

    ## 为什么用 upsert 而不是 append

    一道题一张卡（`uq_ctf2_solve_notes_challenge`）：重解时**更新**。
    与 `ctf2_solve_attempts`（每次一行）相反 —— 它们回答的问题不同：
      · attempt 回答"这一次按什么策略跑的"（历史，只增）；
      · note    回答"这道题怎么解"（知识，取最新）。

    ## 为什么失败不抛

    它在解题**收尾**时被调用，而那时结果是既成事实。让"提炼知识"
    的失败把一次成功的解题变成报错是本末倒置 —— 调用方（`_record_outcome`）
    也再包了一层 try。
    """
    from app.core.ids import new_id
    from app.kernel import notes as notes_mod
    from app.kernel.models import KernelSolveNote

    # ★ v0.40：写入前过一遍分类校验。
    #
    # 为什么写入侧也要校验（不只是"同步时校验一次"）：`category` 是
    # `String(64)` 且**没有数据库约束**，任何一条写路径都可能塞进
    # 一个不该存在的 `agentsec-*` 取值。而那样一张卡会在开关关着的
    # 部署里被正常召回、正常注入，只是它的 verdict 语义没人认 ——
    # 属于"看不出来的错"。
    #
    # ⚠️ 校验失败**不拦解题**（本函数失败只记日志）：把 category
    #    退回空串写卡，卡片仍可用（只是召回时不再靠分类匹配）。
    #    这比"因为分类不合法就把整次解题的知识丢掉"更划算。
    #
    # ⚠️ 配置**只读一次**：下面 `agentsec` / `ir` 与这里共用同一个值。
    #    读两次不仅多一次查询，更糟的是万一两次之间配置被改了，
    #    会出现"分类校验按开着过、证据格式按关着写"这种自相矛盾的卡。
    #
    # ★ v0.41：两个开关在**同一次**配置读取里取出。不是为了省一次查询
    #   （`effective_ctf2_settings` 本身有缓存），而是为了让"本次写卡
    #   用的是哪一份配置"只有一个答案 —— 分两次读，两次之间配置被改
    #   就会出现"按 ir 开着过了校验、按 agentsec 关着写了格式"。
    try:
        ctf2_settings = effective_ctf2_settings(db)
        agentsec_on = bool(ctf2_settings.enable_agentsec_scenarios)
        ir_on = bool(ctf2_settings.enable_ir_scenarios)
    except Exception:  # noqa: BLE001 - 读配置失败不该拦住写卡
        # ⚠️ 只在会话真的坏了才 rollback（判据 `db.is_active`）。
        #   不回滚 → 会话停在 DEACTIVE，之后任何 ORM 访问都抛
        #   PendingRollbackError，报错点在别处。
        #   无条件回滚 → 吞掉调用方未提交的改动（实测
        #   `_tmp/er/probe_except_rollback.py`），而写卡这条路的调用方
        #   通常正带着未提交的 note 改动。
        if not db.is_active:
            db.rollback()
        logger.warning("读 CTF2 配置失败，本次按未启用处理", exc_info=True)
        agentsec_on = False
        ir_on = False

    try:
        category = notes_mod.validate_note_category(
            challenge.category or "",
            agentsec_enabled=agentsec_on,
            ir_enabled=ir_on,
        )
    except ValueError:
        logger.warning(
            "解题方法卡的分类不合法，本次按未分类写卡：%s（%s）",
            challenge.id,
            challenge.category,
        )
        category = ""

    # `solved` 取自**最终状态**（调用方刚定完）。
    solved = challenge.status == KernelChallengeStatus.SOLVED.value

    fields = notes_mod.build_note_fields(
        transcript=outcome.transcript or "",
        error=outcome.error,
        candidates=[c.flag for c in (outcome.candidates or [])],
        solved=solved,
        # 只有场景包分类才按四项证据格式写 `dead_ends` ——
        # 五个现有分类的卡片内容必须一个字不变（见该函数的说明）。
        #
        # ★ v0.41：`ir` 与 `agentsec` 走同一条格式分支（理由见
        # `build_note_fields`：应急响应里"查了但没找到"同样是
        # 要被复用的结论，四项判据结构与对抗评测逐条对应）。
        # 两者**分别传各自的开关**，不合并成一个布尔值 —— 合并会
        # 让"只开了 IR 没开 agentsec"的部署把普通题也套上四项格式。
        agentsec=agentsec_on,
        ir=ir_on,
        # ★ v0.40：模型在 FINAL 里写的**真实**排除清单。
        #
        # 为什么不只靠自动生成的那四项前缀：那四项里只有"观察到什么"
        # 是真拿得到的（错误原话），另三项是如实写的"未记录"。而模型
        # 在 FINAL 的 `ruled_out` 里逐条写了「排除了什么、凭什么」——
        # 那是**唯一**能让证据链真正齐全的信息源。
        #
        # ⚠️ 它仍然要过 `sanitize_for_reuse`（在 `build_note_fields`
        #    内部做），因为模型的自由文本完全可能把目标地址、甚至
        #    密钥原文写进去，而卡片会进另一道题的 prompt。
        ruled_out=list(outcome.ruled_out or []),
        # ★ v0.56：从分类推导"这张卡属于哪一侧"（offensive/defensive/None）。
        # 只标侧，不猜 generic —— 跨场景通用是人工判断，前缀推不出来。
        #
        # ⚠️ 用 `challenge.category`（题目**原始**分类）而不是上面
        # 校验后的 `category`：场景包开关关着时 `validate_note_category`
        # 会把 `ir-*` / `agentsec-*` 退回空串（那是"不许这类卡参与召回"
        # 的合规处理），但**这张卡属于哪一侧**是事实，与开关无关。
        # 用空串去推会丢掉侧标，让同一道题在开关开/关两种部署里写出
        # 侧标不同的卡 —— 而 side 只是排序/可读性辅助，不该随开关漂移。
        domain=notes_mod.domain_for_category(challenge.category or ""),
    )

    # ★ v0.40：判定这张卡是"命中"还是"排除"还是"待跟进"。
    #
    # ## 为什么 `solved` 要在 `fields` 之后**重新算一次**
    #
    # 原来 `solved` 直接等于 `challenge.status == SOLVED`，也就是只认
    # "解出来了"。而一次"跑完全套测试手法、确认这个考察点不存在"的
    # 解题同样花了全部成本，却会被记成未解决 —— 下次同类任务排在后面、
    # 甚至不被召回，于是同一套排除流程要再跑一遍。
    #
    # 现在：`ruled_out`（且证据链齐全）也算 solved。
    # 判据住在 `notes` 里（`resolve_verdict` / `counts_as_solved`），
    # **不在这里再写一遍** —— 两处各写一份必然漂移。
    verdict = notes_mod.resolve_verdict(solved=solved, dead_ends=fields["dead_ends"])
    solved_effective = notes_mod.counts_as_solved(verdict)

    existing = (
        db.execute(
            select(KernelSolveNote).where(KernelSolveNote.challenge_id == challenge.id)
        )
        .scalars()
        .first()
    )
    if existing is None:
        db.add(
            KernelSolveNote(
                id=new_id("knnote"),
                challenge_id=challenge.id,
                category=category,
                solved=solved_effective,
                verdict=verdict,
                domain=fields.get("domain"),
                **{k: v for k, v in fields.items() if k != "domain"},
            )
        )
    else:
        existing.category = category
        existing.solved = solved_effective
        existing.verdict = verdict
        existing.domain = fields.get("domain")
        existing.approach = fields["approach"]
        existing.tools = fields["tools"]
        existing.dead_ends = fields["dead_ends"]
    db.commit()
    logger.info(
        "已更新解题方法卡：%s（solved=%s verdict=%s tools=%s）",
        challenge.id,
        solved_effective,
        verdict,
        fields["tools"] or "无",
    )


def record_solve_usages(
    db: Session, *, attempt_id: str | None, challenge_id: str, outcome
) -> int:  # noqa: ANN001
    """把一次解题的逐轮 token 用量落库（v0.36）。**失败只记日志，不抛。**

    ## 为什么失败不抛

    它在解题**收尾**时被调用，而那时结果是既成事实。让"记统计"的失败
    把一次成功的解题变成报错是本末倒置 —— 与 `learn_from_solve`
    同一条理由。

    ## ★ `attempt_id` 为 None 时**整批跳过**

    `ctf2_solve_usages.attempt_id` 是 NOT NULL 外键。拿不到 attempt
    （历史任务、测试直接调 `run_worker` 而没有快照）时无法落库。

    这时**跳过而不是抛**：统计缺失是可以接受的，而"因为没快照就
    让解题失败"不行。但要**出声**记 warning —— 静默跳过会让
    "为什么这道题没有 token 记录"变成一个查不出来的谜。

    ## 为什么按行 INSERT 而不是批量

    轮数通常 20–80，一次 `add_all` 就够。真正的量级问题不在这里
    （一次解题只写一次），所以不做分批。
    """
    from app.core.ids import new_id
    from app.kernel.models import KernelSolveUsage

    usages = list(getattr(outcome, "usages", None) or [])
    if not usages:
        # 没有调用记录是**正常**的（比如沙箱起不来，一轮都没跑）。
        # 不记 warning —— 那会把正常情况刷成噪音。
        return 0

    if not attempt_id:
        logger.warning(
            "这次解题没有策略快照，%d 条 token 用量无处可挂（统计会缺这一段）：%s",
            len(usages),
            challenge_id,
        )
        return 0

    rows = [
        KernelSolveUsage(
            id=new_id("knuse"),
            attempt_id=attempt_id,
            challenge_id=challenge_id,
            turn=turn,
            # ⚠️ 列宽 160，超长截断而不是让写库失败（模型名可能很长）。
            model=(model or "")[:160],
            provider_label=(label or "")[:120],
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
            reasoning_tokens=usage.reasoning_tokens,
            usage_reported=usage.reported,
        )
        for turn, model, label, usage in usages
    ]
    try:
        # ⚠️ SAVEPOINT，**不是** `db.commit()` + `db.rollback()`。
        #
        # 调用点 `_run_worker` 的 ③ 同步段里，`_record_outcome` 已经改过
        # `challenge.status` / `confirmed_flag` 却**没提交**（全段只有末尾
        # 一次 `db.commit()`，见 `record_solve_telemetry` docstring 里那段
        # 实测）。旧写法在这里 `db.rollback()` 会把那笔解题结果一起吞掉，
        # 于是"记 token 失败"变成"一次成功的解题被记成未解出"。
        #
        # 与 `record_solve_telemetry` 同一处、同一模式、同一个修法。
        with db.begin_nested():
            db.add_all(rows)
            db.flush()
    except Exception:  # noqa: BLE001 - 统计失败不该毁掉解题结果
        # ⚠️ SAVEPOINT 已回滚，**绝不能再 `db.rollback()`**（会把调用方
        #    未提交的业务改动一并回滚）。把刚 add 的行摘掉即可。
        for row in rows:
            with contextlib.suppress(Exception):
                db.expunge(row)
        logger.warning(
            "token 用量落库失败（不影响解题结果）：%s", challenge_id, exc_info=True
        )
        return 0

    reported = sum(1 for r in rows if r.usage_reported)
    logger.info(
        "已记录 token 用量：%s（%d 次调用，其中 %d 次接入点返回了用量，共 %d token）",
        challenge_id,
        len(rows),
        reported,
        sum(r.total_tokens or (r.prompt_tokens + r.completion_tokens) for r in rows),
    )
    return len(rows)


def record_solve_telemetry(
    db: Session, *, attempt_id: str | None, challenge: KernelChallenge, outcome
) -> bool:  # noqa: ANN001
    """把一次解题的横向指标落库（v0.45 生产遥测）。**失败只记日志，不抛。**

    写的是三个数：`turns` / `tool_calls` / `notes_count`。为什么是这三个、
    为什么独立成表而不并进 `ctf2_writeups`，见
    `models.KernelSolveTelemetry` 的类 docstring（那一节是这张表的全部理由）。

    ## 为什么失败不抛

    与 `record_solve_usages` / `learn_from_solve` 同一条纪律：它在解题
    **收尾**时被调用，而那时结果是既成事实。让"记观测"的失败把一次
    成功的解题变成报错是本末倒置。

    ## ⚠️ `attempt_id` 为空时**照写**，这是与 `record_solve_usages` 的区别

    `ctf2_solve_usages.attempt_id` 是 NOT NULL，所以拿不到快照时那边
    **整批跳过**。本表的 `attempt_id` 是 `nullable` + `SET NULL` ——
    观测本身不依赖快照存在，所以这里照写，只把链接留空。

    这不是宽容，是**样本完整性**：历史任务、脚本直调 `run_worker`
    都没有快照，而它们的回合数/工具数一样是有效样本。为了外键好看
    把它们丢掉，等于让遥测只覆盖"走产品路径的那部分解题" ——
    而那恰好是最不需要被诊断的部分。

    ## 为什么每次都**新增一行**，而不是 upsert

    要的就是**样本**。同一道题解 5 次就该有 5 行，那 5 行之间的差异
    才是"这次为什么更快"的证据。upsert 会把样本数永久压成 1
    —— 那正是 `ctf2_writeups`（一题一份）不能兼任本表职责的原因。

    ## ★ 为什么必须用 SAVEPOINT，而不是直接 `db.add` + `db.commit`

    这里踩过一次实测：调用点 `_run_worker` 的 ③ 同步段是

        _record_outcome(db, challenge, outcome)      # 只改内存，不 commit
        record_solve_telemetry(...)                  # ← 本函数
        record_solve_usages(...)
        finalize_solve_scan(...)
        save_writeup(...)
        db.commit()                                  # ← 全段只有这一次

    `_record_outcome` **不提交**，它只把 `challenge.status` /
    `confirmed_flag` 改在内存里。所以本函数里一句朴素 `db.rollback()`
    会把**那些还没提交的解题结果一起回滚掉**：

        改内存后 status = solved
        record_solve_telemetry -> False
        失败后（同一对象）status = unsolved     ← 内存里的改动没了
        ★★ 重新从库里读 status = unsolved | flag = None
        遥测行数 = 0

    也就是"遥测写失败"会把一次**成功的解题**变成未解出 —— 而这正是
    本函数的全部承诺（"失败不影响解题结果"）的反面。它还是安静的：
    解题照常返回、日志只有一条 warning，只有事后看题目状态才会发现。

    `db.begin_nested()` 发一条 SAVEPOINT，`rollback` 只退到那里，
    调用方的未提交改动原样保留。

    ⚠️ 回滚后还要把 `row` 从 session 里摘掉：它已被 flush 进
       SAVEPOINT、又被回滚，留在 session 里的实例是"已持久化过"的
       幽灵状态，下一次 flush 可能把它当脏数据再插一次。
    """
    from app.core.ids import new_id
    from app.kernel.models import KernelSolveTelemetry

    turns = int(getattr(outcome, "turns", 0) or 0)
    tool_calls = int(getattr(outcome, "tool_calls", 0) or 0)
    notes_count = int(getattr(outcome, "notes_count", 0) or 0)

    # ★ v0.47：这次解题有没有带越权与注入手法块。
    #
    # 这是回答"那个开关到底有没有用"唯一能按条件切分的依据 —— 见
    # `models.KernelSolveTelemetry.offensive` 的注释。
    #
    # ⚠️ `outcome.offensive` 报上来的是"**开关当时开着**"（`SolveSpec.offensive`
    #    的语义，见它的注释），而这一列要记的是"**块真的进了提示词**" ——
    #    两者在七个场景分类上不同：`agentsec-*` / `ir-*` 即使开关开着也
    #    按前缀排除（`runner.build_system_prompt`）。不在这里再排除一次，
    #    那几个分类就会被错分进实验组，而它们与对照组的提示词**完全一样**
    #    —— 那正好是这一列要防的污染。
    #
    # 用 `getattr` + 默认 `False` 而不是直接 `outcome.offensive`：这个函数在
    # 测试里常拿替身 outcome 调用，硬取属性会让"只想测 turns 落库"的用例
    # 因为一个无关的新字段而挂 —— 与上面三行同一个理由。
    offensive = bool(getattr(outcome, "offensive", False))
    if offensive:
        # 排除判据复用 `notes` 的两个前缀判定，**不在这里另写一份**。
        from app.kernel import notes as notes_mod_for_category

        _category = challenge.category or ""
        if notes_mod_for_category.is_agentsec_category(
            _category
        ) or notes_mod_for_category.is_ir_category(_category):
            offensive = False

    # ★ v0.46：从 transcript 提取**真正用到的工具**，再从工具推出细分题型。
    #
    # ⚠️ 不能拿 token 用量或回合数去推 —— 它们不含"做了什么"。
    # 也不能拿 `TOOL_NAMES` 去数（只有几个包装层名字，零信息量）。
    # 判据走生产的 `notes.tools_from_transcript`（按词边界从命令正文里认）。
    #
    # 失败不能让遥测整体失败：transcript 是上下文里的字符串，
    # 而这两列是**附加信息** —— 拿不到它们不应该连 turns/tool_calls 一起丢。
    tool_kinds = ""
    subcategory = ""
    try:
        from app.kernel import notes as notes_mod

        tool_kinds = notes_mod.tools_from_transcript(
            getattr(outcome, "transcript", "") or ""
        )[:200]
        subcategory = notes_mod.derive_subcategory(tool_kinds)[:32]
    except Exception:  # noqa: BLE001 - 附加信息，算不出来就留空
        logger.warning("从 transcript 推工具种类失败：%s", challenge.id, exc_info=True)

    row = KernelSolveTelemetry(
        id=new_id("kntel"),
        challenge_id=challenge.id,
        attempt_id=attempt_id or None,
        # 冗余存当时的分类：见类 docstring（防止同步改分类让统计漂移）。
        category=(challenge.category or "")[:32],
        solved=challenge.status == KernelChallengeStatus.SOLVED.value,
        turns=turns,
        tool_calls=tool_calls,
        notes_count=notes_count,
        offensive=offensive,
        tool_kinds=tool_kinds,
        subcategory=subcategory,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except Exception:  # noqa: BLE001 - 遥测失败不该毁掉解题结果
        # ⚠️ SAVEPOINT 已回滚，**这里绝不能再 `db.rollback()`** ——
        #    那会连带回滚调用方还没提交的解题结果（见 docstring 的实测）。
        with contextlib.suppress(Exception):
            db.expunge(row)
        logger.warning(
            "解题遥测落库失败（不影响解题结果）：%s", challenge.id, exc_info=True
        )
        return False

    logger.info(
        "已记录解题遥测：%s solved=%s turns=%d tools=%d notes=%d sub=%s offensive=%s",
        challenge.id,
        row.solved,
        turns,
        tool_calls,
        notes_count,
        subcategory or "-",
        offensive,
    )
    return True


def save_writeup(
    db: Session,
    *,
    challenge: KernelChallenge,
    outcome,  # noqa: ANN001
    attempt_id: str | None,
    elapsed_sec: int | None,
) -> None:
    """生成并保存这道题的题解（v0.36）。**失败只记日志，不抛。**

    ## 为什么在这里生成而不是让端点现拼

    题解要用 `outcome.transcript`，而那份内容**只在收尾那一刻在内存里**
    （之后只留在磁盘上的 transcript 文件里）。端点现拼就得去读文件、
    再解析一遍 —— 而"读文件失败"会让一个已经成功的解题显示不出题解。

    所以收尾时生成一次、落库，端点只读库。

    ## ★ 一题一份：重解时**更新**（与 `learn_from_solve` 同理）

    题解是给赛后提交用的，一份就够。多次尝试产多份会让"提交哪一份"
    变成新问题。

    ## ⚠️ 题解里**保留 flag 原文**（与 `learn_from_solve` 相反）

    见 `models.KernelWriteup` 的类 docstring：判据是这份内容的**去向**
    （赛后提交 vs 注入另一道题的 prompt），不是"flag 是不是秘密"。
    """
    from app.core.ids import new_id
    from app.kernel import workorder as workorder_mod
    from app.kernel import writeup as writeup_mod
    from app.kernel.models import KernelWriteup

    solved = challenge.status == KernelChallengeStatus.SOLVED.value

    try:
        markdown = writeup_mod.build_writeup(
            challenge=challenge,
            outcome=outcome,
            solved=solved,
            elapsed_sec=elapsed_sec,
        )
    except Exception:  # noqa: BLE001 - 题解生成失败不该毁掉解题结果
        logger.warning("生成题解失败（不影响解题结果）：%s", challenge.id, exc_info=True)
        return

    # ★ v0.41（会话 C）：处置建议单独落一份工单文件。
    #
    # ## 为什么在生成题解这一处顺手做
    #
    # 两个理由：① 这是唯一一处"`outcome` 还在内存里"的地方（工单要用
    # `outcome.workorders`，与题解要用 `outcome.transcript` 同理）；
    # ② 工单的内容与题解第六节**必须同源**，分成两处生成迟早会一边
    # 有、一边没有。
    #
    # ## ⚠️ 失败不影响题解，也不影响解题结果
    #
    # `write_order_file` 自己吞异常并返回 `None`（见它的 docstring）。
    # **非 IR 任务这里什么都不做** —— `parse_workorders` 拿到 `None`
    # 返回空列表，于是连目录都不会建。
    orders = workorder_mod.parse_workorders(
        {"workorders": getattr(outcome, "workorders", None)}
    )
    if orders:
        workorder_mod.write_order_file(
            challenge.id,
            workorder_mod.render_workorders(
                orders,
                challenge_title=getattr(challenge, "title", "") or "",
                # ⚠️ 这里**不能**传 `challenge.category`（曾经的写法）。
                #    分类是「ir-root-cause」这种场景名，不是资产标识；
                #    传进去之后工单头部会render出「资产标识：`ir-root-cause`」，
                #    而读工单的人会以为那台机器就叫这个名字。
                #    `KernelChallenge` 上没有资产列（资产来自证据包本身），
                #    所以这里留空 —— 留空时 `render_workorders` 不渲染该行，
                #    这比渲染一个错的标识诚实。
                asset="",
            ),
        )

    # ★ 显式初始化：`db.execute()` 自己也可能抛（那时连 `row` 都还没赋值），
    #   而下面的 `expunge` 要能安全跳过。靠 `contextlib.suppress` 吞
    #   `UnboundLocalError` 也能跑，但那是拿异常控制流程，读的人看不出来。
    row = None
    try:
        # ⚠️ SAVEPOINT，**不是** `db.commit()` + `db.rollback()` ——
        #    与 `record_solve_telemetry` / `record_solve_usages` 同一个理由：
        #    调用方 `_run_worker` 的 ③ 同步段里 `_record_outcome` 的改动
        #    还没提交，一句 `db.rollback()` 会把一次成功的解题一起吞掉。
        with db.begin_nested():
            existing = (
                db.execute(
                    select(KernelWriteup).where(KernelWriteup.challenge_id == challenge.id)
                )
                .scalars()
                .first()
            )
            if existing is None:
                row = KernelWriteup(
                    id=new_id("knwp"),
                    challenge_id=challenge.id,
                    attempt_id=attempt_id,
                    markdown=markdown,
                    solved=solved,
                    turns=int(getattr(outcome, "turns", 0) or 0),
                    elapsed_sec=elapsed_sec,
                )
                db.add(row)
            else:
                row = existing
                existing.attempt_id = attempt_id
                existing.markdown = markdown
                existing.solved = solved
                existing.turns = int(getattr(outcome, "turns", 0) or 0)
                existing.elapsed_sec = elapsed_sec
            db.flush()
    except Exception:  # noqa: BLE001 - 同上
        # ⚠️ SAVEPOINT 已回滚，**绝不能再 `db.rollback()`**。
        #    新实例要摘掉（它已被 flush 过、又被回滚，留在 session 里是
        #    "已持久化过"的幽灵状态）；已存在的实例只需 expunge，
        #    下次 flush 会重新从库里读。
        if row is not None:
            with contextlib.suppress(Exception):
                db.expunge(row)
        logger.warning("题解落库失败（不影响解题结果）：%s", challenge.id, exc_info=True)
        return

    logger.info("已生成题解：%s（%d 字符，solved=%s）", challenge.id, len(markdown), solved)


def get_writeup(db: Session, challenge_id: str) -> KernelWriteup | None:
    """读一道题的题解。没有就返回 `None`（由路由决定报什么错）。

    ⚠️ `KernelWriteup` 只在函数体内 import（避免模块级循环），
    而注解这里能直接写名字是因为文件头有
    `from __future__ import annotations`（注解不求值）。
    另有一个 `TYPE_CHECKING` 块让静态检查认得它。
    """
    from app.kernel.models import KernelWriteup

    return (
        db.execute(select(KernelWriteup).where(KernelWriteup.challenge_id == challenge_id))
        .scalars()
        .first()
    )


def was_solve_started(db: Session, challenge_id: str) -> bool:
    """这道题**发起过**解题吗（v0.36）。

    ⚠️ 读作"解题有没有开始过"—— **不是**"有没有解出"。
       它与 `status == solved` 无关：未解出的题同样算"发起过"
       （题解对未解出的题也产出，见 `writeup.build_writeup`）。

    ## ★ 判据是两条证据任一成立

    1. `ctf2_solve_attempts` 里有没有这道题的行（v0.28 起的**受理快照**）；
    2. `KernelChallenge.scan_id` 是不是非空（**壳 scan**，从第一版就有）。

    ## ★★ 为什么必须两条 —— 少一条就会对一批老题说假话

    `ctf2_solve_attempts` 是迁移 `d5e8f1a37c92`（v0.28，10-11）才建的表。
    **在那之前解的题没有 attempt 行，但都有壳 scan。**

    实测（2026-10-15，全库 135 道逐题核）：**22 道**题有 `scan_id`、
    无 attempt 行、无题解 —— 其中 **20 道是 `solved` 且 `confirmed_flag`
    非空**（`hashcat`、`Url编码`、`流量！鲨鱼！`…），`solved_at` 落在
    09-30 ~ 10-08，而有 attempt 的题最早是 10-12，**时间上完全分界**。

    只看 attempt 的话，这 20 道**平台判过对的题**会得到
    「这道题还没有解过，点「解题」跑一次」—— 一句假话。
    这与它服务的那个修正（见 `router.read_writeup` 的说明：
    别让错误信息误导用户）是同一个毛病，只是换了个边。

    两条判据的覆盖（实测）：只查 attempt 误判 **22** 道；
    两条任一 误判 **0** 道。反向同样安全：真没跑过的 **104** 道
    （两者皆空）仍判 False，不会多给入口。

    ## 为什么不用 `status` 判

    状态会被后续流转改掉 —— `environment_ready` 是解题过程的**主状态**
    （几十分钟里都不是 `unsolved`），按状态二分会把正在跑的题归错档。
    判"有没有发生过"要用**落下来就不变的事实**，不能用当前状态。
    """
    from app.kernel.models import KernelChallenge, KernelSolveAttempt

    # 证据 1：受理快照（v0.28+）。比状态可靠 —— 状态会被改写，快照不会。
    if (
        db.execute(
            select(KernelSolveAttempt.id)
            .where(KernelSolveAttempt.challenge_id == challenge_id)
            .limit(1)
        ).first()
        is not None
    ):
        return True

    # 证据 2：壳 scan（v0.28 之前唯一的痕迹）。
    #
    # ⚠️ 分开两次查询而不是合成一个 OR：两条证据的**成本差很大**
    #    （attempt 走 (challenge_id, created_at) 索引，scan_id 是主键列），
    #    而 v0.28 之后的题命中第一条就返回了。合成一条 JOIN 会让每题都多读一次。
    return (
        db.execute(
            select(KernelChallenge.scan_id)
            .where(KernelChallenge.id == challenge_id)
            .limit(1)
        ).scalar()
        is not None
    )


def token_usage_summary(
    db: Session, *, challenge_id: str | None = None, competition_id: str | None = None
) -> dict:
    """聚合 token 消耗（v0.36）。给总览页与题目详情用。

    ## ★★ 返回里的 `calls` 与 `recordedCalls` 必须分开

    · `calls` = 调用次数（**永远准确**，从行数数得出来）；
    · `recordedCalls` = 其中**接入点真的返回了用量**的次数。

    两者不等时，`totalTokens` 是**下限**（有几次没算进去）。
    界面上必须让用户看得出这一点 —— 把"没记录"显示成"消耗 0"
    与"没跑过"混淆，那正是这一对字段要消除的歧义。

    ## 为什么不做 token → 金额换算

    单价随模型、供应商、时段变化，写死在代码里会误导
    （契约 §3.19.6 记了这条取舍）。只给 token 数。

    ## 分组维度

    · `byModel` —— 换模（v0.35）之后同一道题会用多个模型，
      "换模前后各花多少"是这里最有价值的读数；
    · `byChallenge` —— 总览页要看"哪道题最烧钱"。

    ⚠️ 两个维度都**只在有数据时**才查，避免无谓的 JOIN。
    """
    from sqlalchemy import func

    from app.kernel.models import KernelSolveAttempt, KernelSolveUsage

    def _q(*entities, where=()):  # noqa: ANN001, ANN202
        stmt = select(*entities)
        for cond in where:
            stmt = stmt.where(cond)
        return stmt

    conds = []
    if challenge_id:
        conds.append(KernelSolveUsage.challenge_id == challenge_id)
    if competition_id:
        # 走 challenge 关联（usage 表没有 competition_id 列）
        conds.append(
            KernelSolveUsage.challenge_id.in_(
                select(KernelChallenge.id).where(KernelChallenge.competition_id == competition_id)
            )
        )

    # ① 总量。`func.coalesce` 让"一行都没有"时得到 0 而不是 None。
    #
    # ⚠️ `total_tokens` 可能为 0 而分项有值（接入点只给分项）。
    #    所以求和用 `GREATEST(total, prompt+completion)` 的口径 ——
    #    与 `TokenUsage.billable_total` 保持一致，否则界面上的
    #    "总计"与逐题相加对不上。
    billable = func.greatest(
        KernelSolveUsage.total_tokens,
        KernelSolveUsage.prompt_tokens + KernelSolveUsage.completion_tokens,
    )
    row = db.execute(
        _q(
            func.count(KernelSolveUsage.id),
            func.coalesce(func.sum(KernelSolveUsage.prompt_tokens), 0),
            func.coalesce(func.sum(KernelSolveUsage.completion_tokens), 0),
            func.coalesce(func.sum(billable), 0),
            func.coalesce(
                func.sum(case((KernelSolveUsage.usage_reported.is_(True), 1), else_=0)), 0
            ),
            where=conds,
        )
    ).one()
    calls, prompt, completion, total, recorded = (int(x) for x in row)

    # ② 按模型
    by_model = []
    for model, label, n, t in db.execute(
        _q(
            KernelSolveUsage.model,
            KernelSolveUsage.provider_label,
            func.count(KernelSolveUsage.id),
            func.coalesce(func.sum(billable), 0),
            where=conds,
        )
        .group_by(KernelSolveUsage.model, KernelSolveUsage.provider_label)
        .order_by(func.coalesce(func.sum(billable), 0).desc())
    ).all():
        by_model.append(
            {"model": model or "", "providerLabel": label or "", "calls": int(n), "totalTokens": int(t)}
        )

    # ③ 按题（总览页"哪道题最烧钱"）
    by_challenge = []
    for cid, title, n, t in db.execute(
        _q(
            KernelSolveUsage.challenge_id,
            KernelChallenge.title,
            func.count(KernelSolveUsage.id),
            func.coalesce(func.sum(billable), 0),
            where=conds,
        )
        .join(KernelChallenge, KernelChallenge.id == KernelSolveUsage.challenge_id)
        .group_by(KernelSolveUsage.challenge_id, KernelChallenge.title)
        .order_by(func.coalesce(func.sum(billable), 0).desc())
        .limit(50)
    ).all():
        by_challenge.append(
            {"challengeId": cid, "title": title or "", "calls": int(n), "totalTokens": int(t)}
        )

    # ④ ★ v0.37：按**每次解题**分组 —— 需求要的"细分到每个任务"。
    #
    # ## 为什么必须 JOIN attempt 表
    #
    # usage 行只有 `attempt_id`（一个 id），而用户要看的"这次任务"
    # 得带上 **mode / model / 受理时刻** —— 那三样都在
    # `ctf2_solve_attempts` 上（v0.28 起的策略快照）。
    # 只给一个 id 的列表是没法读的：用户看不出哪次是 safe、哪次是 auto。
    #
    # ⚠️ `attempt_id` 是 NOT NULL 外键，所以这里用 **INNER JOIN** 是安全的
    #    （不会因为 NULL 丢行）。若哪天它变成可空，这个 JOIN 会静默丢数据 ——
    #    所以下面有一条测试盯着"逐次之和 == 总量"。
    #
    # ## 为什么加 limit
    #
    # 与 `byChallenge` 同一条理由：这是总览页的读数，不是导出。
    # 一次比赛可能跑几百次解题，全量返回会把响应撑大而没有更多信息。
    by_attempt = []
    for aid, cid, title, mode, model, started, n, t in db.execute(
        _q(
            KernelSolveUsage.attempt_id,
            KernelSolveUsage.challenge_id,
            KernelChallenge.title,
            KernelSolveAttempt.mode,
            KernelSolveAttempt.llm_model,
            KernelSolveAttempt.created_at,
            func.count(KernelSolveUsage.id),
            func.coalesce(func.sum(billable), 0),
            where=conds,
        )
        .join(KernelChallenge, KernelChallenge.id == KernelSolveUsage.challenge_id)
        .join(KernelSolveAttempt, KernelSolveAttempt.id == KernelSolveUsage.attempt_id)
        .group_by(
            KernelSolveUsage.attempt_id,
            KernelSolveUsage.challenge_id,
            KernelChallenge.title,
            KernelSolveAttempt.mode,
            KernelSolveAttempt.llm_model,
            KernelSolveAttempt.created_at,
        )
        .order_by(func.coalesce(func.sum(billable), 0).desc())
        .limit(100)
    ).all():
        by_attempt.append(
            {
                "attemptId": aid,
                "challengeId": cid,
                "title": title or "",
                "mode": mode or "",
                "model": model or "",
                "calls": int(n),
                "totalTokens": int(t),
                "startedAt": started,
            }
        )

    # ⑤ 最早一条记录的时间 —— 界面上要说明"统计从哪一刻开始"。
    #
    # ★ 为什么必须给这个：v0.36 之前的解题没有 usage 记录。不给这个
    #   字段的话，用户会以为"总共就花了这么点"，而实际是**没记**。
    since = db.execute(_q(func.min(KernelSolveUsage.created_at), where=conds)).scalar()

    return {
        "calls": calls,
        "recordedCalls": recorded,
        "promptTokens": prompt,
        "completionTokens": completion,
        "totalTokens": total,
        "byModel": by_model,
        "byChallenge": by_challenge,
        "byAttempt": by_attempt,
        "since": since,
    }


def challenge_usage_entries(db: Session, challenge_id: str) -> list:
    """一道题的**逐次**消耗明细（按轮次升序）。

    这是"细分到每一次任务"最直接的那个视图 —— 每一轮花了多少。
    """
    from app.kernel.models import KernelSolveUsage

    return list(
        db.execute(
            select(KernelSolveUsage)
            .where(KernelSolveUsage.challenge_id == challenge_id)
            .order_by(KernelSolveUsage.created_at, KernelSolveUsage.turn)
        )
        .scalars()
        .all()
    )


def set_challenge_priority(db: Session, challenge_id: str, *, priority: int) -> KernelChallenge:
    """设置一道题的调度优先级。**越大越先做。**

    ## 为什么这是一个独立动作，而不是"解题时带一个参数"

    比赛现场的真实用法是：
        一次点几十道题的「解题」→ 看排队情况 → 手工挑三五道插队。

    插队发生在**受理之后**，而 `start_solve` 是受理那一刻的入口。
    把它做成解题参数等于要求用户"先想清楚顺序再点"，而那与
    "开赛后题目分批开放、边看边调"的实际节奏不符（规则里也明确
    写了"可以持续与 Agent 交互、调整解题优先级"）。

    ## 改了优先级**不影响**已在跑的题

    只有还在排队（`solving` 但尚未被 worker 认领）的题会被重新排序。
    已经在跑的那一轮不会被打断 —— 中断一道跑到一半的题会浪费掉
    已经消耗的平台环境与 LLM 调用，而"让它跑完"的代价只是排队的题
    晚一会儿开始。

    ⚠️ 这个函数**不校验 status**：给一道已解出的题设优先级无害
        （它不会被认领），而为此抛错反而会让"批量设优先级"这种
        用法必须先去筛状态。
    """
    challenge = get_challenge_or_404(db, challenge_id)
    challenge.priority = int(priority)
    db.commit()
    db.refresh(challenge)
    logger.info("CTF2 题目优先级：%s → %d", challenge_id, challenge.priority)
    return challenge


def set_priorities_by_score(
    db: Session, *, competition_id: str | None = None, limit: int = 50
) -> int:
    """把排队中的题**按分值从高到低**批量设优先级。返回改了几道。

    ## ★ 为什么是"由用户主动触发的便利入口"，而不是默认策略

    "先做高分题"听起来显然，但它**不总是对的**：

    * 高分题往往更难，开赛初期把并发全压在几道硬题上，可能一道都拿不下；
    * 而"前三名额外加分"意味着**先解出一道容易题**可能比死磕难题更划算；
    * 真正的取舍取决于当前比分、剩余时间与队伍能力 —— 那是战术判断，
      系统替用户猜只会猜错。

    所以这条路径必须**由用户显式触发**（接口上的一个按钮），
    而调度的默认行为仍然是 FIFO（`priority` 全 0 时）。

    ## 分值相同怎么办

    按 `score DESC, updated_at ASC` 排，同分内保持 FIFO —— 与
    `scheduler._claim` 的排序规则**方向一致**，避免"批量设完之后
    顺序又和预期不一样"。

    ⚠️ `score` 可能是 NULL（练习集常常不给分）。NULL 排在**最后**：
        PostgreSQL 默认 `DESC` 时 NULL 在前，那会让所有没分值的题
        插到队首 —— 而它们恰恰是最不值得优先的。
    """
    query = select(KernelChallenge).where(
        KernelChallenge.status == KernelChallengeStatus.SOLVING.value
    )
    if competition_id:
        query = query.where(KernelChallenge.competition_id == competition_id)
    # ⚠️ `nullslast()` 不能省：PostgreSQL 的 `DESC` 默认把 NULL 排最前。
    rows = list(
        db.execute(
            query.order_by(
                KernelChallenge.score.desc().nullslast(),
                KernelChallenge.updated_at.asc(),
            ).limit(limit)
        )
        .scalars()
        .all()
    )
    # 从高到低赋递减的优先级：最大值给分最高的那道。
    # 用 `len(rows) - i` 而不是 `score` 本身，是因为分值可能是 0 或 NULL，
    # 而优先级要保证"被调过的排在没调过的（默认 0）之前"。
    total = len(rows)
    for index, challenge in enumerate(rows):
        challenge.priority = total - index
    db.commit()
    logger.info("CTF2 按分值批量设优先级：%d 道", total)
    return total


def _assert_wrong_ceiling(value: int) -> None:
    if not (MIN_WRONG_SUBMISSIONS <= value <= MAX_WRONG_SUBMISSIONS_CEILING):
        raise ApiError.validation(
            f"错误提交次数上限应在 {MIN_WRONG_SUBMISSIONS}-"
            f"{MAX_WRONG_SUBMISSIONS_CEILING} 之间。",
            {"maxWrongSubmissions": "超出允许范围"},
        )


def _assert_llm_ready(db: Session) -> None:
    """受理之前就检查"有没有可用的 LLM provider"。

    ★ 为什么在**受理时**检查，而不是等 worker 跑到一半再说：

    没有 provider 时，`runner.build_spec` 会失败，而那时用户已经在界面上
    看到"解题中"了 —— 他会盯着一个注定失败的进度等，最后拿到一句
    "还没有配置 LLM provider"。在受理时拦住，用户得到的是**立刻**的
    明确提示（而且他知道该去哪一页修）。

    与 `runner.build_spec` 的检查是同一件事的两处表达，这是刻意的：
    那条路径也可能被内部调用（不经过这个函数），所以两边都要挡。
    """
    from app.providers.models import ProviderAccount

    provider = (
        db.execute(select(ProviderAccount).limit(1)).scalars().first()
    )
    if provider is None:
        raise ApiError(
            409,
            "CTF2_NO_PROVIDER",
            "还没有配置 LLM provider，无法解题。请先在「设置 → 模型服务」里添加一个。",
        )


# ══════════════════════════════════════════════════════════
# 本地复盘任务（v0.43，应急响应）
# ══════════════════════════════════════════════════════════
#
# 这一节的函数让"从界面发起一次应急响应复盘"成为可能。在此之前，
# 本地复盘案例只能由 `scripts/run_ir_closed_loop.py` 直接写库创建 ——
# 也就是说侧栏那个「应急响应」入口后面**没有任何可点的功能**。

#: 隐藏的本地批次名。所有本地复盘任务都挂在这个批次下 ——
#: 它出现在界面的赛事列表里，但用户不需要理解"批次"这个概念。
LOCAL_REVIEW_BATCH_NAME: Final[str] = "本地复盘"

#: 建任务时固定的分类。见 `schemas.IrTaskIn` 的说明：
#: 让用户在还不知道结论时先选"根因 / 时间线 / 范围"等于让他在猜，
#: 而选错会让提示词从第一轮就偏。
_LOCAL_REVIEW_CATEGORY: Final[str] = IrCategory.ROOT_CAUSE


def ensure_local_batch(db: Session) -> KernelCompetition:
    """拿到（或创建）那个装本地复盘任务的隐藏批次。

    ## 为什么不复用用户的某个 practice 批次

    `practice` 的语义是"平台上某个练习集"，`_remote_ids()` 会拿它的
    `remote_id` 当 `practice_id` 去打平台。把本地任务塞进去，worker
    就会拿一个自造 id 去请求不存在的路径，404 → 题失败，而错误信息
    说的是"同步可能没完成"。

    ## `remote_id` 为什么要现造

    `uq_ctf2_competitions_account_remote` 是 `(account_id, remote_id)`
    上的唯一约束，而本地批次的 `account_id` 是 NULL —— PostgreSQL 里
    NULL 不等于 NULL，所以这条约束对它形同虚设。也就是说唯一性得靠
    `remote_id` 自己：这里用固定字面量 `local-reviews`，而查询也是按
    `(kind, remote_id)` 找的，所以重复调用只会返回同一行。

    ⚠️ 不用 `new_id()` 现造：那会让每次调用都建出一个新批次，
       界面上的"本地复盘"会越堆越多，而每个里面只有一道题。
    """
    existing = (
        db.execute(
            select(KernelCompetition).where(
                KernelCompetition.kind == KernelCompetitionKind.LOCAL,
                KernelCompetition.remote_id == "local-reviews",
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return existing

    batch = KernelCompetition(
        id=new_id("kncomp"),
        # ★ NULL：本地批次不属于任何平台账号。这正是 v0.43 把这一列
        #   改成可空的原因 —— 见 `models.KernelCompetition.account_id`。
        account_id=None,
        remote_id="local-reviews",
        kind=KernelCompetitionKind.LOCAL,
        name=LOCAL_REVIEW_BATCH_NAME,
        remote_status="",
    )
    db.add(batch)
    db.flush()
    return batch


def create_local_review(
    db: Session,
    *,
    title: str,
    brief: str = "",
    attachments: list[dict[str, Any]] | None = None,
) -> KernelChallenge:
    """建一个本地复盘任务（一道 `kind='local'` 批次下的 `ir-*` 题）。

    ## 做三件事，顺序不能变

    1. 拿到本地批次（`ensure_local_batch`）；
    2. 建题，`category` 固定 `ir-root-cause`、`sandbox_profile` 由
       `runner.profile_for_category()` 推出来；
    3. **不在这里入队** —— 入队由调用方（router）在附件认领完之后做。

    第 3 条是刻意的：附件要先落到 `attachments.attachment_root(challenge.id)`
    才能被 `_attachment_paths` 读到，而那个路径需要 challenge.id。先入队
    再铺附件会有一个"worker 已经开始读附件目录、而文件还没到位"的窗口，
    表现是**模型说没有证据可看**，而文件明明在磁盘上。

    ## `sandbox_profile` 为什么调 `profile_for_category`

    `KernelChallenge.sandbox_profile` 的列默认值是 `suvex-sandbox`（基础镜像），
    而 IR 分类需要 `suvex-sandbox-ir`（带日志/取证工具的镜像）。不显式写
    就落默认值，于是容器里没有 `tshark`/`fls` 一类命令 —— 而模型的反应
    是"我没有工具"，不是"镜像选错了"。

    这条教训来自 2026-10-21 的真跑：`run_ir_closed_loop.py` 最初也没写，
    是那次跑失败后补上的（现在它里面有同样一行）。
    """
    from app.kernel import runner as runner_mod

    batch = ensure_local_batch(db)
    category = _LOCAL_REVIEW_CATEGORY
    challenge = KernelChallenge(
        id=new_id("knchal"),
        competition_id=batch.id,
        # 本地任务没有远端 id。这里现造一个，是为了满足列上的 NOT NULL
        # 与"同一批次内唯一"（`uq_ctf2_challenges_competition_remote`）。
        # 它**不会被**发给任何平台 —— `_requires_platform` 对 local 返回 False。
        remote_id=new_id("local"),
        title=title.strip(),
        category=category,
        description=(brief or "").strip(),
        attachments=[
            {"name": str(item.get("name") or ""), "url": None}
            for item in (attachments or [])
        ]
        or None,
        status=KernelChallengeStatus.UNSOLVED.value,
        # ★ 见 docstring：不显式写会落列默认值 `suvex-sandbox`。
        sandbox_profile=runner_mod.profile_for_category(category),
    )
    db.add(challenge)
    batch.challenge_count = int(batch.challenge_count or 0) + 1
    db.flush()
    return challenge


def _enqueue(challenge_id: str, *, attempt_id: str | None = None) -> None:
    """把题丢进调度器，带上**这次尝试的快照 id**。
    ⚠️ 延迟 import：`scheduler` 依赖 `runner`，而 `runner` 依赖本模块 ——
    在模块顶层互相 import 会得到一次循环导入，而它的表现是
    "某个端点在第一次被调用时报 ImportError"，排查方向完全跑偏。

    ★ 为什么传 `attempt_id` 而不是把策略值一个个传下去（2026-10-11）：
    调度器只是个"排队与启动"的中转，它不需要理解策略语义。传一个 id
    让 worker 自己去读快照，好处是**将来加字段不用改这条链路** ——
    而这条链路跨了 service → scheduler → runner 三层，
    每加一个字段就要动三处是它最容易出错的地方。
    """
    from app.kernel.scheduler import get_scheduler

    get_scheduler().enqueue(challenge_id, attempt_id=attempt_id)


def cancel_challenge(db: Session, challenge: KernelChallenge) -> KernelChallenge:
    """取消：把状态推回"未解出"，并让调度器不再认领它。

    ## ★ 它**不能**杀死已经在跑的 worker（技术限制，如实记）

    worker 是一个线程，而 Python 没有安全的杀线程手段（见 scheduler 的
    docstring）。所以"取消"的语义是：

    * **队列里没开始的** → 真正取消（状态回 `unsolved`，调度器不再认领）；
    * **已经在跑的** → 只是标记，那一轮会跑完，然后**不落候选**
      （它结束时看到状态已经不是 `solving`，于是丢弃候选 ——
      见 `runner._handle_candidates` 里那个判断）。

    这个区别必须让用户知道 —— 界面上"取消"之后还看到进度在动，
    而没有解释的话，用户会以为按钮坏了。
    """
    if challenge.status in (
        KernelChallengeStatus.SOLVED.value,
        KernelChallengeStatus.CANDIDATE_READY.value,
    ):
        # 已经解出、或已经有候选在等确认：取消它等于丢掉结果。
        # 这两种情况要用户**明确**处理（前者不必取消，后者直接忽略候选即可）。
        raise ApiError.conflict(
            "CTF2_NOTHING_TO_CANCEL",
            "这道题已经解出或已有候选 flag。若要放弃候选，请直接忽略它。",
        )

    challenge.status = KernelChallengeStatus.UNSOLVED.value
    challenge.last_error = "已取消。"
    db.commit()
    return challenge


# ══════════════════════════════════════════════════════════
# 提交（人工确认路径）
# ══════════════════════════════════════════════════════════


def confirm_submit(
    db: Session, challenge: KernelChallenge, *, flag: str, mode: str | None = None
) -> tuple[str, str]:
    """**人工确认后**提交一个 flag。返回 `(状态, 说明)`。

    ## ★ 两条提交路径共用同一段逻辑

    `runner._handle_candidates` 的 auto 路径调的是
    `runner.submit_candidate`，而这个函数**也调它** —— 不是"再写一遍"。

    为什么必须共用：提交涉及三个容易做错且互相耦合的判断（去重、
    额度、pending 的先落库），而它们有**顺序**要求（见 `submit_candidate`
    的三段式说明）。抄一份的后果是两条路径在任何一次修改后行为不同 ——
    而"自动提交"与"手动提交"行为不同，正是最不该发生的事。

    ## 先做本地校验，再决定发不发

    `validate_flag` 在**本地**拦形状不对的候选（见 runner 的说明：
    平台额度不可再生，而形状不对这件事我们在本地就能回答）。
    `decide_submission` 去重 + 查额度 + 判模式。
    """
    from app.kernel import runner

    # ★ 平台能力断言放在**最前面**：这条路在赛事题目上根本没有终点，
    #   先做格式校验再去拒绝会让用户以为"改一下 flag 就能交"。
    _assert_submission_supported(db, challenge)

    # ★★ 状态断言（2026-10-10 补）。
    #
    # ## 这里原来漏了，而 `runner.submit_candidate` 的注释早就指出来了
    #
    # 那段注释原话：「产品路径本该拦住它（`_assert_resolvable` 对已解出抛
    # 409），但 `submit_challenge` 只调了 `_assert_submission_supported`，
    # **没有**调 `_assert_resolvable` —— 所以那条防线是漏的」。
    #
    # ## 漏了会怎样：扣掉一次**不可再生**的机会
    #
    # `runner` 那边的兜底只是"不写坏状态"（避免撞 CHECK 约束得到 500），
    # 它**没有阻止请求发出去**。于是对一道**已解出**的题再提交一个错误
    # 的 flag，平台会回 `WRONG`，而 `_advance_challenge` 会实打实地
    # `wrong_submission_count += 1` —— 每道题的错误提交次数是有限的。
    #
    # 顺序上放在 `_assert_submission_supported` **之后**：先回答
    # "这条路存不存在"（那是能力问题，与状态无关），再回答"现在能不能做"。
    # 反过来的话，一道赛事题会先被报"状态不对"，而它真正的问题是
    # "这个接口在平台上根本不存在" —— 两句里只有后一句可行动。
    _assert_resolvable(challenge)

    try:
        cleaned = runner.validate_flag(flag)
    except runner.FlagFormatError as exc:
        raise ApiError.validation(exc.message, {"flag": "格式不正确"}) from exc

    resolved_mode = mode or effective_ctf2_settings(db).submit_mode
    if resolved_mode not in {m.value for m in KernelSubmitMode}:
        raise ApiError.validation("未知的提交模式。", {"mode": "取值不在允许范围内"})

    already = runner.already_submitted(db, challenge_id=challenge.id, flag=cleaned)
    decision = runner.decide_submission(
        mode=resolved_mode,
        already=already,
        remaining_wrong=challenge.remaining_wrong_submissions,
    )
    if decision.action == "reject":
        # ⚠️ 409 而不是 400：这不是"参数写错了"，是"当前不允许"。
        raise ApiError.conflict("CTF2_SUBMIT_REFUSED", decision.reason)

    remote = _remote_ids(db, challenge.id)

    def _client_factory() -> tuple[KernelClient, dict[str, Any]]:
        return (
            build_client_sync(),
            {
                "competition_remote_id": remote.get("practice_id", ""),
                "remote_id": remote.get("challenge_id", ""),
                "sub_flag_id": remote.get("sub_flag_id"),
            },
        )

    return _run(
        runner.submit_candidate(
            challenge_id=challenge.id,
            flag=cleaned,
            mode=resolved_mode,
            client_factory=_client_factory,
        )
    )


# ══════════════════════════════════════════════════════════
# CTF2 配置（提交策略）
# ══════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class KernelSettings:
    """当前**实际生效**的 CTF2 配置。"""

    submit_mode: str
    max_wrong_submissions: int
    default_sandbox_profile: str
    max_concurrent_solves: int
    # ── 自动作业（v0.24：抢首杀）──────────────────────────
    solve_poll_seconds: float
    auto_sync_seconds: int
    auto_priority_new: bool
    auto_start_new: bool
    #: 每题最多跑多少回合（v0.26）。见 `vocab.DEFAULT_CTF2_MAX_TURNS` ——
    #: 这个数没有全局最优（简单题 6 轮、硬题 200 轮），所以它是配置项。
    max_turns: int
    #: 换模 fallback（v0.35）。默认 provider 调不动时改用哪个；
    #: `None` = 不换（默认），行为与引入前一致。见 `settings/models.py`
    #: 的 `KernelConfig.fallback_provider_id`。
    fallback_provider_id: str | None = None
    #: 是否启用铸网-2026 智能体安全场景包（v0.40）。默认 **False**。
    #:
    #: ⚠️ 它不是一个"多给几种题型"的开关，它**改变 agent 的工作纪律**：
    #: 打开后 `agentsec-*` 那四类任务不再是"攻破系统拿 flag"，而是
    #: "判定某个安全考察点是否成立"（证据分三级、排除必须附反证、
    #: 多智能体结论只能标疑似）。理由见 `settings/models.py` 的
    #: `KernelConfig.enable_agentsec_scenarios`。
    enable_agentsec_scenarios: bool = False
    #: 是否启用应急响应场景包（v0.41）。默认 **False**。
    #:
    #: ⚠️ 与 `enable_agentsec_scenarios` **各自独立**，理由见
    #: `settings/models.py` 的 `KernelConfig.enable_ir_scenarios`：
    #: 共用开关会让一族的分类在另一族开关打开时通过校验，
    #: 而对应的 prompt 块不注入 —— 分类合法、纪律没注入。
    enable_ir_scenarios: bool = False
    #: 是否启用多智能体编排（v0.58）。默认 **False**。
    #:
    #: 打开时 `SolveSpec.multi_agent=True`，解题走 root 调度 + 子 agent
    #: 并行执行；关着时单循环逐字节不变。它改变**执行拓扑**、
    #: 不改变行为纪律（与两个场景包开关不同维），但会把一次任务的
    #: LLM 调用量放大约 N 倍 —— 所以默认关、由人显式打开。
    enable_multi_agent: bool = False
    #: ★ v0.72：可用的 MCP server 配置（学 strix 的 list_mcps 三件套）。
    #:
    #: 形状是"原样的 JSON 列表"（`None`/`[]` = 没配），解析成
    #: `McpServerConfig` 元组发生在 `build_spec` —— 让"读配置"与"解析协议
    #: 配置"分开，配置层不必知道 MCP 的形状。
    mcp_servers: list | None = None


def effective_ctf2_settings(db: Session) -> KernelSettings:
    """读生效配置。**没有就返回默认，不建行。**

    ★ 与 `settings.service.effective_execution_settings` 同一个模式，
    理由也一样：调度器与 worker 会**频繁**读它，而为一次读操作写库
    既没必要、也会让 `updated_at`（用来回答"用户什么时候改过配置"）
    被后台线程反复刷新。

    ⚠️ 而且这里的默认值来自**代码常量**，不是环境变量 —— 见
    `settings.models.KernelConfig` 的说明（提交策略不该由一个部署参数
    静默决定）。没有第二个来源，就没有"来源"可标。
    """
    row = db.get(KernelConfig, SINGLETON_ID)
    if row is None:
        return KernelSettings(
            submit_mode=KernelSubmitMode.SAFE.value,
            max_wrong_submissions=DEFAULT_CTF2_MAX_WRONG_SUBMISSIONS,
            default_sandbox_profile=KernelSandboxProfile.BASE.value,
            max_concurrent_solves=DEFAULT_CTF2_MAX_CONCURRENT_SOLVES,
            solve_poll_seconds=DEFAULT_CTF2_SOLVE_POLL_SECONDS,
            auto_sync_seconds=DEFAULT_CTF2_AUTO_SYNC_SECONDS,
            auto_priority_new=True,
            auto_start_new=False,
            max_turns=DEFAULT_CTF2_MAX_TURNS,
            fallback_provider_id=None,
            enable_agentsec_scenarios=False,
            enable_ir_scenarios=False,
            enable_multi_agent=False,
            mcp_servers=None,
        )
    return KernelSettings(
        submit_mode=row.submit_mode,
        max_wrong_submissions=row.max_wrong_submissions,
        default_sandbox_profile=row.default_sandbox_profile,
        max_concurrent_solves=row.max_concurrent_solves,
        solve_poll_seconds=row.solve_poll_seconds,
        auto_sync_seconds=row.auto_sync_seconds,
        auto_priority_new=row.auto_priority_new,
        auto_start_new=row.auto_start_new,
        max_turns=row.max_turns,
        fallback_provider_id=row.fallback_provider_id,
        enable_agentsec_scenarios=row.enable_agentsec_scenarios,
        enable_ir_scenarios=row.enable_ir_scenarios,
        enable_multi_agent=row.enable_multi_agent,
        mcp_servers=row.mcp_servers,
    )


def get_ctf2_config(db: Session) -> KernelConfig:
    """读配置，没有就建一条。**设置页用这个**（那里"读"本身就要有东西可编辑）。

    ⚠️ 只用于「用户要编辑」的路径（设置页 GET/PUT）。
       扫描启动、调度器轮询这类**只读**路径请用 `effective_ctf2_settings` ——
       它不建行，也就没有下面这条并发问题。

    ★ 走 `get_or_create_singleton`：原先是 `if db.get() is None: db.add + commit`，
      两个请求同时进来会撞 `pk_suvex_config` 主键（实测复现过）。
      而调用方 `scans/service.py` 那处是 `except Exception: logger.warning(...)`
      —— 异常被吞、会话没 rollback，于是**下一个毫不相干的属性访问**才炸
      `PendingRollbackError`，报错位置离真正原因很远。
    """
    from app.core.db import get_or_create_singleton

    return get_or_create_singleton(db, KernelConfig, SINGLETON_ID)  # type: ignore[return-value]


def update_ctf2_settings(
    db: Session,
    *,
    submit_mode: str,
    max_wrong_submissions: int,
    default_sandbox_profile: str,
    max_concurrent_solves: int,
    solve_poll_seconds: float,
    auto_sync_seconds: int,
    auto_priority_new: bool,
    auto_start_new: bool,
    max_turns: int,
    fallback_provider_id: str | None = None,
    enable_agentsec_scenarios: bool,
    enable_ir_scenarios: bool,
    enable_multi_agent: bool,
) -> KernelConfig:
    """保存配置。**逐项校验，不静默夹取。**

    静默夹取的后果在"错误提交次数上限"上尤其严重：用户填 99、
    保存成功、界面显示 10 —— 他会以为这是我们的 bug，然后试图再改一次。

    ## ★ 两个场景包开关为什么没有默认值（v0.44）

    它们是**关键字参数、无默认值**，所以漏传会立刻 `TypeError`
    （Python 在调用时就拒绝，不是运行到一半才发现）。

    ⚠️ 这**不是**风格问题。这个函数是"逐字段赋值"（见下面的
    `row.enable_agentsec_scenarios = ...`），给形参默认值等于说
    "调用方没提这一项 = 把它设成 false"。而调用方没提这一项，最常见
    的原因恰恰是**它不知道有这一项**（新加的功能，旧的调用点还在）——
    于是每一次别的配置保存都会顺手把场景包关掉，且没有任何痕迹。

    `PUT /ctf2/settings` 那侧靠 `KernelSettingsIn` 的必填字段拦住了 HTTP
    入口，但那是**另一个入口**：`tests/` 与运维脚本都直接调这个函数。
    两处都要拦，否则"静默关掉"只是换了个入口复现。
    """
    if submit_mode not in {m.value for m in KernelSubmitMode}:
        raise ApiError.validation("未知的提交模式。", {"submitMode": "取值不在允许范围内"})
    _assert_wrong_ceiling(max_wrong_submissions)
    if default_sandbox_profile not in {p.value for p in KernelSandboxProfile}:
        raise ApiError.validation(
            "未知的沙箱镜像。", {"defaultSandboxProfile": "取值不在允许范围内"}
        )
    _assert_solve_concurrency(max_concurrent_solves)
    _assert_solve_poll_seconds(solve_poll_seconds)
    _assert_auto_sync_seconds(auto_sync_seconds)
    _assert_max_turns(max_turns)
    # ★ 换模 fallback：空串归一成 None（"不换"），给了一个 id 就必须
    #   真的存在 —— 存一个不存在的 id 会让它在解题失败时才暴露，
    #   而那时正是最不该再出一个"配置指错了"的时刻。
    normalized_fallback = (fallback_provider_id or "").strip() or None
    if normalized_fallback is not None:
        from app.providers.models import ProviderAccount

        if db.get(ProviderAccount, normalized_fallback) is None:
            raise ApiError.validation(
                "指定的备用 provider 不存在。",
                {"fallbackProviderId": "请选择一个已配置的 provider，或留空表示不启用"},
            )

    row = get_ctf2_config(db)
    row.submit_mode = submit_mode
    row.max_wrong_submissions = max_wrong_submissions
    row.default_sandbox_profile = normalize_sandbox_profile(default_sandbox_profile)
    row.max_concurrent_solves = max_concurrent_solves
    row.solve_poll_seconds = solve_poll_seconds
    row.auto_sync_seconds = auto_sync_seconds
    row.auto_priority_new = auto_priority_new
    row.auto_start_new = auto_start_new
    row.max_turns = max_turns
    row.fallback_provider_id = normalized_fallback
    row.enable_agentsec_scenarios = bool(enable_agentsec_scenarios)
    row.enable_ir_scenarios = bool(enable_ir_scenarios)
    row.enable_multi_agent = bool(enable_multi_agent)
    db.commit()
    db.refresh(row)
    logger.info(
        "CTF2 配置已更新：mode=%s maxWrong=%d profile=%s 解题并发=%d "
        "认领间隔=%.1fs 自动同步=%s 新题自动优先=%s 新题自动开解=%s 回合预算=%d 备用模型=%s "
        "智能体安全场景=%s 应急响应场景=%s 多智能体=%s",
        submit_mode,
        max_wrong_submissions,
        default_sandbox_profile,
        max_concurrent_solves,
        solve_poll_seconds,
        f"{auto_sync_seconds}s" if auto_sync_seconds else "关",
        auto_priority_new,
        auto_start_new,
        max_turns,
        normalized_fallback or "（不启用）",
        # 这一项会**改变模型的行为纪律**（从"攻破系统"变成"判定考察点"），
        # 所以它的开/关必须能在日志里查得到 —— 复盘时"当时到底开没开"
        # 是一个只能用日志回答的问题。
        "开" if enable_agentsec_scenarios else "关",
        # ★ v0.41：同上，独立一项。两个开关分开记，因为"当时到底开了
        # 哪一个"在复盘时是两个独立问题 —— 记成一项就再也分不出来了。
        "开" if enable_ir_scenarios else "关",
        # ★ v0.58：多智能体编排开关。它改变的是**执行拓扑**（单循环 vs
        # root+子 agent），与两个场景包开关（行为纪律）不同维。事后
        # "这次为什么花了 N 倍 token / 为什么有并行子任务"要看这一项。
        "开" if enable_multi_agent else "关",
    )
    return row


def _assert_solve_concurrency(value: int) -> None:
    """校验解题并发配额。不合法就抛 422，**不静默夹取**。

    与 `MAX_WRONG_SUBMISSIONS_CEILING` 同一条理由：静默夹取会让用户
    以为自己填的值生效了，而实际跑的是另一个数字 —— 在比赛现场，
    "我以为开了 8 个并发但实际只有 3 个"是那种查起来最费时间的错。

    ⚠️ 区间与 `schemas.KernelSettingsIn` 的 `Field(ge=1, le=16)` 是同一对。
        schema 挡 HTTP 入参，这里挡直接调 service 的调用方 —— 两者挡的
        不是同一件事，所以都要有（与 `settings.service` 里那条注释同理）。
    """
    if not (MIN_CTF2_MAX_CONCURRENT_SOLVES <= value <= MAX_CTF2_MAX_CONCURRENT_SOLVES):
        raise ApiError.validation(
            f"解题并发应在 {MIN_CTF2_MAX_CONCURRENT_SOLVES}-"
            f"{MAX_CTF2_MAX_CONCURRENT_SOLVES} 之间。",
            {
                "maxConcurrentSolves": (
                    f"应在 {MIN_CTF2_MAX_CONCURRENT_SOLVES}-"
                    f"{MAX_CTF2_MAX_CONCURRENT_SOLVES} 之间，收到 {value}"
                )
            },
        )


def _assert_solve_poll_seconds(value: float) -> None:
    """校验认领轮询间隔。不合法就抛 422，**不静默夹取**。

    ⚠️ 下界 0.5 是护栏而不是建议值：填 0 会让调度线程变成忙等 ——
       症状是 CPU 打满而解题**反而变慢**，这种"越调越慢"最难被归因到
       一个设置项上。
    """
    if not (MIN_CTF2_SOLVE_POLL_SECONDS <= value <= MAX_CTF2_SOLVE_POLL_SECONDS):
        raise ApiError.validation(
            f"认领间隔应在 {MIN_CTF2_SOLVE_POLL_SECONDS}-"
            f"{MAX_CTF2_SOLVE_POLL_SECONDS} 秒之间。",
            {
                "solvePollSeconds": (
                    f"应在 {MIN_CTF2_SOLVE_POLL_SECONDS}-"
                    f"{MAX_CTF2_SOLVE_POLL_SECONDS} 秒之间，收到 {value}"
                )
            },
        )


def _assert_auto_sync_seconds(value: int) -> None:
    """校验自动同步间隔。**0 是合法值**（表示关闭），不是缺省。

    ★ 这条边界要单独说：把 0 混进"非法值"里会逼用户去填一个最小值，
    而"我不想让它自动同步"是一个完全正当的诉求（比如 PAT 有配额、
    或者比赛结束了想让系统安静下来）。
    """
    if not (MIN_CTF2_AUTO_SYNC_SECONDS <= value <= MAX_CTF2_AUTO_SYNC_SECONDS):
        raise ApiError.validation(
            f"自动同步间隔应在 {MIN_CTF2_AUTO_SYNC_SECONDS}-"
            f"{MAX_CTF2_AUTO_SYNC_SECONDS} 秒之间（0 表示关闭）。",
            {
                "autoSyncSeconds": (
                    f"应在 {MIN_CTF2_AUTO_SYNC_SECONDS}-"
                    f"{MAX_CTF2_AUTO_SYNC_SECONDS} 秒之间（0 表示关闭），收到 {value}"
                )
            },
        )


def apply_new_challenge_priority(db: Session, challenge_ids: list[str]) -> int:
    """给**刚同步进来的**题目发自动优先级。返回改了几道。

    ## 取值

        priority = 1000 + min(max(score, 0), 999)

    ## ★ 为什么是"基准 + 分值"而不是"按分排名次"

    用名次会让**同一批**内部的相对顺序正确，而两批之间毫无关系：先到的
    3 道题占住 1000+ 的最高段，30 秒后新来一道 500 分的题反而排在它们
    **后面**。而抢首杀要的恰恰是"新出现的题插到最前面"。

    "基准 + 分值"让排序键**只由题目自己的属性决定**，于是跨批次可比 ——
    这正是比赛想要的：新来的高分题优先于刚才的低分题。

    ## ★ 它只碰传进来的这批 id，绝不重排全表

    这是整个自动作业里最容易做错的一处。如果为了"让新题排最前"去调
    `set_priorities_by_score` 重排全库，用户的**人工优先级会被整片覆盖**
    —— 那种"我明明调过"的丢失比不自动更糟。

    ## 与人工优先级的边界

    自动值落在 `1000..1999`，人工值约定在 `1..999`（见 `vocab` 里的分层
    说明）。用户随时可以把某道题设成 1000 以上来**压过自动判断** ——
    "人永远在环里"是赛事方明确允许的操作，这条约定保证它不被后台抢走。
    """
    if not challenge_ids:
        return 0
    rows = (
        db.execute(select(KernelChallenge).where(KernelChallenge.id.in_(challenge_ids)))
        .scalars()
        .all()
    )
    changed = 0
    for challenge in rows:
        # score 为 NULL 时按 0 算 —— 它仍然应该是"新题里最不着急的一档"，
        # 但**不能**因此掉回 0：那会和从没调过的题混在一起，新题加成白给。
        score = challenge.score if isinstance(challenge.score, int) else 0
        challenge.priority = CTF2_AUTO_PRIORITY_BASE + min(
            max(score, 0), CTF2_PRIORITY_MANUAL_MAX
        )
        changed += 1
    if changed:
        db.commit()
        logger.info(
            "CTF2 新题自动优先：%d 道（优先级落在 %d-%d）",
            changed,
            CTF2_AUTO_PRIORITY_BASE,
            CTF2_AUTO_PRIORITY_MAX,
        )
    return changed


def _assert_max_turns(value: int) -> None:
    """校验解题回合预算。不合法就抛 422，**不静默夹取**。

    ★ 上界 500 不是成本护栏，而是"别手滑填 100000"的护栏。真正的成本
    （每轮一次 LLM 调用）该由人在现场权衡 —— 难题给 200 轮可能是完全
    理性的决定，我们拦下来等于替他决定"这道题不配解出来"。
    """
    if not (MIN_CTF2_MAX_TURNS <= value <= MAX_CTF2_MAX_TURNS):
        raise ApiError.validation(
            f"回合预算应在 {MIN_CTF2_MAX_TURNS}-{MAX_CTF2_MAX_TURNS} 之间。",
            {
                "maxTurns": (
                    f"应在 {MIN_CTF2_MAX_TURNS}-{MAX_CTF2_MAX_TURNS} 之间，收到 {value}"
                )
            },
        )


def submit_mode_of(db: Session) -> str:
    """当前提交策略。**runner 与 router 都读它**（一处来源）。"""
    return effective_ctf2_settings(db).submit_mode


# ══════════════════════════════════════════════════════════
# MCP 连接（v0.81）
# ══════════════════════════════════════════════════════════
#
# 这一段补的是一处**贯通性缺口**，不是新功能：
# `mcp_servers` 这一列在 v0.72 就存在（`settings/models.py`）、
# `KernelSettings` 读它、`build_spec` 解析它 —— 但**没有任何 HTTP 端点
# 能写它**。所有 MCP 工具在 v0.81 之前只能靠手工改数据库才能启用。
#
# 这正是交接文档里那个缺陷形状的翻版：**配了/写了但静默失效**。
# 只不过这次失效的位置更早 —— 它连"配"这个动作都没有入口。


def get_mcp_servers(db: Session) -> list[dict]:
    """读已保存的 MCP 连接清单（**原样 JSON**，不解析成 dataclass）。

    返回原样 JSON 而不是 `McpServerConfig` 有两个原因：
    1. 这里要回给前端编辑，`McpServerConfig` 是 frozen dataclass、
       没有 `env` 的只读视图，转换会多一层丢失风险；
    2. 坏条目应当**照原样回显**给用户去修，而不是被解析层悄悄丢掉。
       （解析层的"跳过坏条目"是给 `build_spec` 用的运行时容错，
       不是给编辑界面用的展示口径。）
    """
    row = db.get(KernelConfig, SINGLETON_ID)
    if row is None or not row.mcp_servers:
        return []
    raw = row.mcp_servers
    if not isinstance(raw, list):
        logger.warning("mcp_servers 不是列表，按空处理：%r", type(raw).__name__)
        return []
    return [item for item in raw if isinstance(item, dict)]


def _positive_float_or_none(raw: object) -> float | None:
    """把配置里的超时值规整成"正数或 None"。

    ## 为什么非法值退化成 `None` 而不是报错

    与 `mcp_servers` 其余字段同一条容错纪律：一条连接的超时写坏
    （`"abc"` / `0` / 负数）不该让整次任务的配置读写全部失败 ——
    最坏退化成"用内核默认 60 秒"，也就是引入这个字段之前的行为。

    ⚠️ 但**写库路径**不该静默接受坏值：`McpServerIn.callTimeout` 已经用
    `Field(gt=0, le=1800)` 在入参处挡掉了。这里的容错是给**读取**准备的
    （库里可能有手工改过的历史数据），两条是不同用途。
    """
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _public_server_view(item: dict) -> dict:
    """把一条已存配置转成回执形状：**去掉 `env`**，只留 `has_env`。

    干不干净不靠自觉：`McpServerIn` 只做入参，`McpServerOut` 里根本没有
    `env` 字段 —— 这个函数是它唯一的填充来源，漏不掉。
    """
    env = item.get("env")
    allowed = item.get("allowed_tools")
    # ★ v0.83：超时**要回显**（它不是凭据）。不回显的话部署者看不到
    # "我配的 300 秒到底生效了没"，而超时配错的症状是"调用总失败"。
    return {
        "name": str(item.get("name") or ""),
        "command": str(item.get("command") or ""),
        "args": [str(a) for a in item.get("args") or []],
        "notes": str(item.get("notes") or ""),
        "allowed_tools": [str(t) for t in allowed] if isinstance(allowed, list) else None,
        "has_env": bool(env) if isinstance(env, dict) else False,
        "call_timeout": _positive_float_or_none(item.get("call_timeout")),
    }


def public_mcp_servers(db: Session) -> list[dict]:
    """回给界面的连接清单（已去掉 `env`）。"""
    return [_public_server_view(item) for item in get_mcp_servers(db)]


def save_mcp_servers(db: Session, servers: list[dict]) -> list[dict]:
    """整份替换 MCP 连接清单。

    ## 校验在写库**之前**逐条做完

    与 `update_ctf2_settings` 同一纪律：不静默夹取、不静默丢弃。
    重名是最容易犯的错（复制粘贴一条改 command 忘了改 name），
    而它的后果是解析层只收第一条 —— 第二条永远"配了但不生效"。
    所以重名**直接 400**，不允许落到库里。

    返回保存后的公开视图（已去 `env`）。
    """
    seen: set[str] = set()
    for index, item in enumerate(servers):
        name = str(item.get("name") or "").strip()
        if not name:
            raise ApiError.validation(
                f"第 {index + 1} 条连接缺 name。",
                {"servers": "每条连接都必须有名字（模型用它来调用）"},
            )
        if name in seen:
            raise ApiError.validation(
                f"连接名 `{name}` 重复。",
                {"servers": "连接名必须唯一 —— 重名时只有第一条会生效"},
            )
        seen.add(name)
        if not str(item.get("command") or "").strip():
            raise ApiError.validation(
                f"连接 `{name}` 缺 command。",
                {"servers": "需要填写拉起 server 的可执行文件（如 npx / python3）"},
            )

    row = get_ctf2_config(db)

    # ★ v0.82：`preserve_env` —— 保留同名连接已保存的 `env`。
    #
    # 这一段修的是一个**会丢凭据**的缺陷：`env` 只进不出（`McpServerOut`
    # 里根本没有那个字段），所以前端编辑一条已有连接时拿不到原值。
    # 而本函数是全量替换 —— 前端把空 `env` 提交上来就会被真的清掉。
    # 症状是"只改了备注，保存成功、界面正常，三天后那条连接认证失败"。
    #
    # ⚠️ 按**名字**匹配旧值，不按位置：清单允许重排，靠下标会把 A 的
    #    env 贴到 B 上 —— 那比丢掉更难查。
    previous_env: dict[str, dict[str, str]] = {}
    for old in row.mcp_servers or []:
        if not isinstance(old, dict):
            continue
        old_name = str(old.get("name") or "").strip()
        old_env = old.get("env")
        if old_name and isinstance(old_env, dict):
            previous_env[old_name] = {str(k): str(v) for k, v in old_env.items()}

    # 存 `None` 而不是 `[]`：与 `KernelSettings.mcp_servers` 的默认值
    # （`None` = 没配）保持一致，否则同一种状态在库里有两个表示。
    row.mcp_servers = [
        {
            "name": str(item.get("name") or "").strip(),
            "command": str(item.get("command") or "").strip(),
            "args": [str(a) for a in item.get("args") or []],
            "env": (
                previous_env.get(str(item.get("name") or "").strip(), {})
                if item.get("preserve_env")
                else {str(k): str(v) for k, v in (item.get("env") or {}).items()}
            ),
            "notes": str(item.get("notes") or ""),
            "allowed_tools": (
                [str(t) for t in item["allowed_tools"]]
                if isinstance(item.get("allowed_tools"), list)
                else None
            ),
            # ★ v0.83：单次调用超时。`None` = 用内核默认（60 秒）。
            # 不落库的话，界面配了、`build_spec` 也读不到，等于没配 ——
            # 而症状正是"我明明配了 300 秒，为什么还是超时"。
            "call_timeout": _positive_float_or_none(item.get("call_timeout")),
        }
        for item in servers
    ] or None
    db.commit()
    db.refresh(row)
    logger.info(
        "MCP 连接已更新：%s",
        "、".join(f"{s['name']}({s['command']})" for s in (row.mcp_servers or [])) or "（清空）",
    )
    return public_mcp_servers(db)


async def test_mcp_server(item: dict) -> dict:
    """试连**一条尚未保存**的配置，返回结构化结果。

    ## 为什么这个函数必须有

    用户在设置页填完 `command`，唯一能验证的办法在此之前是"保存 →
    跑一次真实解题 → 看模型能不能调" —— 而那条路上的失败是
    **模型侧的一次工具报错**，既不会说清是 command 写错还是握手超时，
    也会污染一次已受理的解题。

    所以这里真拉起一次、握手一次、取一次工具清单，把三件事分开回答：
    拉得起来吗（`reason` 说清为什么拉不起来）／工具叫啥／握手多久。

    ⚠️ **不保存**：试连只读不写，失败不留痕。
    """
    import time

    from app.kernel import mcp as mcp_mod

    started = time.monotonic()
    # ★ v0.83：试连也要带上 call_timeout —— 否则"配了 300 秒的连接"
    # 在试连时仍按 60 秒跑，慢 server 会被误判成"握手失败"。
    raw_timeout = item.get("call_timeout")
    try:
        parsed_timeout = float(raw_timeout) if raw_timeout is not None else 0.0
    except (TypeError, ValueError):
        parsed_timeout = 0.0
    cfg = mcp_mod.McpServerConfig(
        name=str(item.get("name") or "").strip() or "test",
        command=str(item.get("command") or "").strip(),
        args=[str(a) for a in item.get("args") or []],
        env={str(k): str(v) for k, v in (item.get("env") or {}).items()},
        notes=str(item.get("notes") or ""),
        allowed_tools=(
            [str(t) for t in item["allowed_tools"]]
            if isinstance(item.get("allowed_tools"), list)
            else None
        ),
        call_timeout=parsed_timeout if parsed_timeout > 0 else None,
    )
    if not cfg.command:
        return {
            "ok": False,
            "reason": "没有填 command —— 不知道要拉起什么。",
            "tools": [],
            "server_name": "",
            "server_version": "",
            "elapsed_ms": 0,
        }
    try:
        async with mcp_mod.McpClient(cfg) as client:
            tools = await client.list_tools()
    except FileNotFoundError:
        # 单独接这一支：这是最常见的失败，而且原因唯一 ——
        # 可执行文件不在 PATH 上（或路径写错）。
        return {
            "ok": False,
            "reason": (
                f"找不到可执行文件 `{cfg.command}`。"
                "容器里跑的话要确认它装在镜像里（PATH 上），或写绝对路径。"
            ),
            "tools": [],
            "server_name": "",
            "server_version": "",
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
    except mcp_mod.McpError as exc:
        return {
            "ok": False,
            "reason": str(exc),
            "tools": [],
            "server_name": "",
            "server_version": "",
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
    except Exception as exc:  # noqa: BLE001 - 试连的目的就是把各种异常变成一句话
        return {
            "ok": False,
            "reason": f"{type(exc).__name__}: {exc}",
            "tools": [],
            "server_name": "",
            "server_version": "",
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }

    names = [str(t.get("name") or "") for t in tools]
    return {
        "ok": True,
        "reason": "" if names else "连上了，但这个 server 没报任何工具 —— 它可能启动即退出。",
        "tools": names,
        "server_name": "",
        "server_version": "",
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }


# ══════════════════════════════════════════════════════════
# 内置工具清单与开关（v0.82）
# ══════════════════════════════════════════════════════════
#
# 这一段补的是与 v0.81 MCP 管理**互补的那一半**：MCP 管"外部接进来什么"，
# 这里管"内置的 13 个怎么开关"。两件事分开的理由见
# `schemas.py` 的「为什么这一组与 MCP 那一组分开」。


def get_disabled_tools(db: Session) -> tuple[str, ...]:
    """读被显式关掉的内置工具名（已规整：未知名字剔除、去重、保序）。

    ⚠️ 与 `get_mcp_servers` 刻意不同：那个回**原样 JSON**（因为要回给
    前端编辑，坏条目应当照原样回显给用户去修）；而这里回**规整后的**，
    因为工具名是代码里固定的枚举 —— 库里出现不认识的名字只可能是
    "那个工具被删了"，把它显示在界面上只会让用户尝试去关一个
    不存在的开关。
    """
    from app.kernel.tools import normalize_disabled

    row = db.get(KernelConfig, SINGLETON_ID)
    if row is None:
        return ()
    return normalize_disabled(getattr(row, "disabled_tools", None))


def tools_catalog(db: Session) -> dict:
    """组装界面要的完整清单：每个工具的名字、说明、开关、就绪状态。

    ## 为什么 `ready` 写成"逐个按 `requires_connection` 查"

    而不是"写死某几个工具需要某条连接"：这样将来接外部工具时不用改这里，
    也让"某个工具为什么不可用"永远只有一个答案来源。

    ⚠️ 这个判定**只看配置在不在**（不真连、不 exec）：它每次打开页面都会被
    调用，而真连要拉起子进程。真正的连通性由 `POST /kernel/mcp-servers/test`
    回答 —— 界面上把"配置就绪"与"试连通过"分开显示，因为用户该做的事不同。
    """
    from app.kernel.tools import TOOL_GROUPS, TOOL_SPECS

    disabled = set(get_disabled_tools(db))
    servers = public_mcp_servers(db)
    configured = {str(s.get("name") or "") for s in servers}

    tools: list[dict] = []
    for spec in TOOL_SPECS:
        enabled = spec.name not in disabled
        if spec.requires_connection:
            ready = spec.requires_connection in configured
            reason = (
                ""
                if ready
                else f"需要一条名为 `{spec.requires_connection}` 的 MCP 连接。"
            )
        else:
            ready, reason = True, ""

        tools.append(
            {
                "name": spec.name,
                "label": spec.label,
                "group": spec.group,
                "summary": spec.summary,
                "params": spec.params,
                # ★ v0.86：默认参数下的能力边界（空串 = 默认即全功能）。
                # 必须透出到 API —— 否则这个字段等于只写给自己看，
                # 而它存在的全部意义是让**模型和用户**看到"默认会缩水什么"。
                "default_limitation": spec.default_limitation,
                "enabled": enabled,
                "core": spec.core,
                "requires_connection": spec.requires_connection,
                "ready": ready,
                "reason": reason,
            }
        )

    return {
        "tools": tools,
        "groups": list(TOOL_GROUPS),
        "disabled": sorted(disabled),
    }


def save_disabled_tools(db: Session, disabled: list[str]) -> dict:
    """整份替换"关掉的工具"清单。

    ## 校验全部在写库之前做完

    与 `save_mcp_servers` 同一纪律：不静默夹取、不静默丢弃。
    提交一个不存在的工具名**直接 400** —— 它多半是拼错了，
    而"拼错的开关"最坏的后果是用户以为关掉了某样东西、实际没有。

    ⚠️ 空清单存 `None`（不是 `[]`）：与 `KernelSettings.mcp_servers`
    同一条规矩，同一种状态不要在库里有两个表示。
    """
    from app.kernel.tools import TOOL_BY_NAME, normalize_disabled

    unknown = [n for n in disabled if str(n).strip() not in TOOL_BY_NAME]
    if unknown:
        raise ApiError.validation(
            f"未知工具名：{'、'.join(str(n) for n in unknown)}。",
            {
                "disabled": (
                    "只能关内置工具。可用名字："
                    + "、".join(sorted(TOOL_BY_NAME))
                )
            },
        )

    row = get_ctf2_config(db)
    cleaned = normalize_disabled([str(n).strip() for n in disabled])
    row.disabled_tools = list(cleaned) or None
    db.commit()
    db.refresh(row)
    logger.info(
        "内置工具开关已更新：关掉 %s",
        "、".join(cleaned) if cleaned else "（无，全部可用）",
    )
    return tools_catalog(db)


def skills_catalog() -> dict:
    """列出随代码分发的**技能包**（`prompts/skills/**/*.md`）。

    ## 为什么这个端点要有

    技能包是 `load_skill` 的输入，但它此前**没有任何界面入口** ——
    部署者既看不到一共几篇、也不知道模型能查到什么。
    这与"工具清单没有端点"是同一个缺口（v0.82 修过一次，见 `tools_catalog`）。

    ## 为什么按 category 分组、且保留空分类

    分组顺序直接决定界面栏位顺序。空分类**保留**是有意的：
    "这一栏没有技能"与"没有这一栏"在界面上是两回事，
    前者说明打包漏了（本该有），后者是正常布局。

    ⚠️ `path` **不下发**：它是宿主文件系统路径，对界面无用，
    且会泄露部署目录结构。只给 name / category / description。
    """
    from app.kernel.skills import discover_skills

    skills = discover_skills()
    categories: dict[str, list[dict]] = {}
    for skill in skills:
        categories.setdefault(skill.category, []).append(
            {
                "name": skill.name,
                "category": skill.category,
                "description": skill.description,
            }
        )
    for items in categories.values():
        items.sort(key=lambda item: item["name"])
    return {
        "total": len(skills),
        "categories": [
            {"category": name, "skills": items}
            for name, items in sorted(categories.items())
        ],
    }


__all__ = [
    "MAX_SYNC_CHALLENGES",
    "MAX_SYNC_COMPETITIONS",
    "MAX_WRONG_SUBMISSIONS_CEILING",
    "MIN_WRONG_SUBMISSIONS",
    "KernelSettings",
    "StartedEnvironment",
    "SyncResult",
    "apply_new_challenge_priority",
    "bind_account",
    "build_client_sync",
    "cancel_challenge",
    "confirm_submit",
    "decrypt_account_token",
    "effective_ctf2_settings",
    "expire_stale_environments",
    "get_account",
    "get_account_or_404",
    "get_challenge_or_404",
    "get_competition_or_404",
    "get_disabled_tools",
    "save_disabled_tools",
    "skills_catalog",
    "tools_catalog",
    "get_ctf2_config",
    "get_mcp_servers",
    "latest_environment",
    "list_accounts",
    "list_challenges",
    "list_competitions",
    "list_environments",
    "public_mcp_servers",
    "release_environment",
    "release_environment_sync",
    "save_mcp_servers",
    "set_challenge_priority",
    "set_priorities_by_score",
    "start_environment_for",
    "platform_reports_solved",
    "start_solve",
    "submit_mode_of",
    "sync",
    "test_mcp_server",
    "try_sync",
    "unbind_account",
    "update_ctf2_settings",
]

