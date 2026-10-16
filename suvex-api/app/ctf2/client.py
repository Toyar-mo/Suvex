"""CTF2 Open API 客户端（httpx 异步）。

## 这个模块是整个 ctf2 领域**唯一**与外部平台讲话的地方

所有关于 CTF2 的知识（端点路径、状态码含义、`access_ready` 语义、
`Retry-After` 行为）都收在这一个文件里。上游改了只改这里 ——
与 `scans/runner.py` 把 strix 的 CLI 契约收在一处是同一条理由。

## ★ 令牌只从入参传入，且日志里脱敏

三条，缺一不可：

1. **不进构造函数以外的任何地方**：不读环境变量、不读数据库
   （那是 `service.py` 的事）。client 拿到什么就用什么 ——
   这让"这个 PAT 从哪来"只有一个答案。
2. **不进 URL**：只在 `Authorization` 头里。查询串会进 uvicorn 的
   access log、反代的 access log、以及任何抓包记录 ——
   而 access log 是最常被复制粘贴分享的东西（与 `providers/llm.py`
   里记的那条完全一样）。
3. **日志走 `core.crypto.redact`**：它认得 `Bearer xxx` 的形状，
   兜住"某个异常消息恰好带上了请求头"这类手滑。
   ⚠️ 这是兜底，不是"可以放心打日志"的许可证 ——
   本模块**不打印任何请求头或请求体**。

## ★ 401 与 403 不重试，429 与 5xx 才重试

这不是"顺手写的分类"，是两种失败**补救动作完全不同**：

| 状态 | 含义 | 重试有用吗 | 补救动作 |
| --- | --- | --- | --- |
| 401 | PAT 失效/被吊销 | **没有** | 人重新授权 |
| 403 | 缺某个 scope | **没有** | 人去平台加权限 |
| 429 | 打太快 | 有 | 等 `Retry-After` |
| 5xx/超时 | 平台抖动 | 有 | 指数退避 |

对 401 做重试是最坏的一种：它会对着一个已经失效的凭据反复发请求，
而运维看到的是"平台一直报错"（如果日志写得含糊的话）——
顺便还可能因为反复失败触发平台侧的风控。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any, Final
from urllib.parse import urlsplit

import httpx

from app.core import netguard
from app.core.crypto import mask_secret, redact
from app.ctf2.models import CTF2_API_PREFIX, CTF2_OFFICIAL_HOSTS

logger = logging.getLogger("suvex.ctf2")

DEFAULT_BASE_URL = "https://ctf2.dasctf.com"

# 平台错误机器码：这道题**本队伍已经解出了**。
#
# ★ 实测（2026-10-01）向已解出的 hashcat 重复提交正确的 flag，平台回
#   HTTP 400 `{"error": {"code": "ALREADY_SOLVED", ...}}`。
#
# 这个码值得单独拎出来，因为它与"普通 400（参数写错）"要采取的行动
# **完全相反**：前者意味着这题不该再解了，后者意味着我们的代码坏了。
CTF2_ERROR_ALREADY_SOLVED: Final[str] = "ALREADY_SOLVED"

# ── 响应信封的两种层 ────────────────────────────────────────
#
# ★ 实测（用真 PAT 打出来的形状）：
#
#     {"data": {"items": [ … ], "total": 5}, "success": true}
#
# 所以"列表在哪"要分两层找：外面是**信封**（可能缺席），里面是**容器键**。
# 详见 `_as_list` 的说明 —— 那里的教训是"只在顶层找容器键"会让整次同步
# 静默拿到空表（而日志只有一句"形状不认识"）。
_ENVELOPE_KEYS: Final[tuple[str, ...]] = ("data", "result", "payload")
_LIST_CONTAINER_KEYS: Final[tuple[str, ...]] = (
    "items",
    "results",
    "list",
    "records",
    "rows",
)

# 平台错误键 → 用户看得懂的一句话。
#
# ⛔ **只放实测确认过的键**（见 `_identity_hint` 的说明：没见过的原样引用，
#    不猜）。目前这三条都是有出处的：
#    · `membership_required` —— 用真 PAT 调 `/competitions/{id}/stages/`
#      与 `/status/` 时实测得到（那场赛事是团队赛，而这个 PAT 没加入战队）；
#    · `permission_denied` —— 匿名请求时实测得到（未带凭据的 401 信封）；
#    · `not_found` —— 探测不存在的路由时实测得到
#      （`params.resource` 为 `route`，这是"路由不存在"的判据）。
#
# ★ 它们值得进这张表，因为它们是**用户唯一能拿到的原因**：
#   平台不会说"请去加入战队"，它只说 `errors.team.membership_required`。
_ERROR_KEY_HINTS: Final[dict[str, str]] = {
    "errors.team.membership_required": (
        "这个账号还不是该场次的战队成员 —— 请先在 CTF2 上加入对应战队，"
        "或换一场不需要战队身份的比赛。"
    ),
    "errors.auth.permission_denied": "这条凭据没有本次调用所需的权限。",
    "errors.common.not_found": "平台说这个资源不存在（也可能是接口路径已变更）。",
}


# ══════════════════════════════════════════════════════════
# 领域异常
# ══════════════════════════════════════════════════════════
#
# ★ 为什么它们**不是** `ApiError`：`ApiError` 是"要变成 HTTP 响应"的东西，
# 它带着 status_code 与前端错误码表里的 code。而这一层不知道也没必要知道
# HTTP —— 它可能被后台任务、被 CLI、被测试调用。
#
# 翻译成 `ApiError` 是 `service.py` 的责任，那里才知道"用户看到的是什么"。
# 这样做还有一个好处：客户端的测试不必去构造 FastAPI 的请求上下文。


class Ctf2Error(Exception):
    """CTF2 交互失败的基类。

    `detail` 是给运维排查用的补充信息（平台原话、响应片段）。
    ⚠️ 它**不含凭据** —— 构造它的地方必须保证这一点，见 `_safe_excerpt`。
    """

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class Ctf2AuthExpired(Ctf2Error):
    """401 —— 这条 PAT 已经不能用了（过期、被吊销、或复制时截断了）。

    ## ★ 它必须是一个**独立的类型**，不能混进通用的"调用失败"

    补救动作是"人重新授权"，而且**只有人能做**。如果它被归进一个
    泛泛的 `Ctf2Error`，调用点的代码就只能给一句"对接 CTF2 失败，
    请重试"—— 用户会一直重试一个永远不会好的东西。
    """


class Ctf2ScopeMissing(Ctf2Error):
    """403 —— PAT 本身有效，但缺某个 scope。

    ## ★ `missing_scope` 必须带出来

    CTF2 在 403 的响应里告诉我们缺的是哪一个权限。把这个信息**扔掉**、
    只报一句"权限不足"，用户就得去平台把全部权限翻一遍 —— 而这些
    权限项在平台上并不总是自解释的（`challenge:submit` 与
    `challenge:write` 差在哪？）。

    带出来之后，界面能直接说"请在 CTF2 给这个 token 勾上
    `environment:start`"。
    """

    def __init__(
        self, message: str, *, missing_scope: str | None = None, detail: str | None = None
    ) -> None:
        super().__init__(message, detail=detail)
        self.missing_scope = missing_scope

    def describe(self) -> str:
        """可直接展示的中文句子。"""
        if self.missing_scope:
            return f"{self.message}（缺少权限：{self.missing_scope}）"
        return self.message


class Ctf2RateLimited(Ctf2Error):
    """429 且重试次数用尽（或 `Retry-After` 长到不该干等）。"""

    def __init__(
        self, message: str, *, retry_after: float | None = None, detail: str | None = None
    ) -> None:
        super().__init__(message, detail=detail)
        self.retry_after = retry_after


class Ctf2Unavailable(Ctf2Error):
    """网络层失败 / 5xx，且重试次数用尽。"""


class Ctf2ResponseError(Ctf2Error):
    """4xx（401/403/429 之外）或响应结构不是我们预期的形状。

    ⚠️ 与 `Ctf2Unavailable` 分开：前者是"请求错的或平台改了接口"，
    重试没有意义；后者是"链路抖了"，重试有意义。

    ## `status_code` 是给调用方**分类**用的

    同一个 404 在不同端点上的意思**不一样**：在"起环境"那里它意味着
    "这道题没有靶机环境"（纯附件题，见 `start_environment_until_ready`），
    而不是"题目不存在"。没有状态码的话，调用方只能去嗅探 message
    字符串 —— 那种判据会在文案改动时静默失效。

    ## `code` 同理，而且**必须**与 `status_code` 一起看

    `status_code` 只到"哪一类"，`code` 才到"哪一件事"。实测
    （2026-10-01）向一道**已经解出**的题重复提交正确的 flag，平台回：

        HTTP 400
        {"error": {"code": "ALREADY_SOLVED",
                   "key": "errors.submission.already_solved"}, "success": false}

    这是 400，但它与"参数写错了"（同样是 400）要采取的行动完全相反：
    前者说明**这题已经不用做了**，后者说明**我们的代码有问题**。
    只凭状态码分类，前者会被当成后者 —— 于是一道已经解出的题在界面上
    显示为「失败」，而用户完全看不懂为什么。

    ⚠️ `code` 来自别人家的响应，认不出来就是 `None`；调用方**不能**
    把它当成一定有值。
    """

    def __init__(
        self,
        message: str,
        *,
        detail: str | None = None,
        status_code: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message, detail=detail)
        self.status_code = status_code
        self.code = code


class Ctf2EnvironmentTimeout(Ctf2Error):
    """环境启动的幂等轮询**总超时**。

    ★ 单独一个类型，因为它的语义与"启动失败"不同：
    环境**可能还在起**（平台侧在排队），只是我们这边不打算再等了。
    补救动作是"过一会儿再启动一次"（幂等，安全），
    而不是"环境坏了，去看日志"。
    """

    def __init__(
        self, message: str, *, polls: int, elapsed_seconds: float, detail: str | None = None
    ) -> None:
        super().__init__(message, detail=detail)
        self.polls = polls
        self.elapsed_seconds = elapsed_seconds


# ══════════════════════════════════════════════════════════
# 目标白名单（已定设计决策 5）
# ══════════════════════════════════════════════════════════


def _as_host(value: str) -> str:
    """把白名单里的一项规整成**纯 host**。

    ★ 为什么要宽容：调用方拿到的"允许的地址"是从数据库里的
    `access_urls` 来的，而那里存的是 `1.2.3.4:8080` 或
    `http://1.2.3.4:8080` 这种**带端口**的形状。

    如果这里直接拿整串去比 host，那么带端口的白名单项会**永远不匹配**
    —— 而症状是"所有请求被拒"，一个可用性事故；排查的人会去怀疑
    网络、怀疑平台，而不是怀疑"白名单里那一项多写了个冒号"。

    宽容输入、严格输出：**规整的时候尽量认**，
    但判断本身（`==` 或 `.` 前缀）一点不放松。
    """
    text = (value or "").strip()
    if not text:
        return ""
    candidate = text if "://" in text else f"//{text}"
    return (urlsplit(candidate).hostname or "").lower()


def assert_allowed_target(url: str, *, allowed_extra: tuple[str, ...] = ()) -> None:
    """校验一个 URL 是否落在允许的范围内。**给 runner 用**（不在这里调用）。

    ## 边界是什么，以及为什么它必须硬编码

    允许的只有两类：

    1. **CTF2 官方域名**（`ctf2.dasctf.com`）—— PAT 只准发往这里；
    2. **本题的 access_url**（由调用方通过 `allowed_extra` 传入）。

    ⚠️ "本题的 access_url" 是**逐题不同**的，所以它不能硬编码 ——
    但它必须由调用方从**数据库里那一题的记录**取，而不是从"题目里
    写着的一个地址"取。题面是参赛者可控的输入（题目本身就可能诱导
    人去访问某个地址），拿它当白名单等于没有白名单。

    ## 为什么用 host 精确匹配而不是后缀匹配

    `endswith("dasctf.com")` 会放过 `evil-dasctf.com` 与
    `dasctf.com.attacker.net`。两者都是有过的绕过手法，
    而写后缀匹配的人当时想的都是"这样能覆盖子域名"——
    子域名要用 `host == x or host.endswith("." + x)` 表达，不是 `endswith(x)`。
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise Ctf2Error(f"不允许的目标协议：{parts.scheme or '（空）'}。只接受 http/https。")
    host = (parts.hostname or "").lower()
    if not host:
        raise Ctf2Error("目标地址里没有主机名。")

    # `allowed_extra` 里的项可能是 `1.2.3.4:8080` / `http://1.2.3.4:8080`
    # 这种带端口的形状 —— 见 `_as_host` 的说明。
    candidates = tuple(_as_host(item) for item in allowed_extra) + tuple(
        h.lower() for h in CTF2_OFFICIAL_HOSTS
    )
    for allowed in candidates:
        if allowed and (host == allowed or host.endswith("." + allowed)):
            return
    raise Ctf2Error(
        f"目标 {host} 不在允许范围内。只允许 CTF2 官方域名与本题的环境地址 —— "
        "这条限制是硬编码的，见 app/ctf2/client.py 的说明。"
    )


# ══════════════════════════════════════════════════════════
# 令牌桶限流
# ══════════════════════════════════════════════════════════


class TokenBucket:
    """进程内令牌桶（异步）。

    ## 为什么是**令牌桶**而不是 `core/ratelimit.py` 那个滑动窗口

    那个 `Limiter` 回答的是"你打得太多了，**拒绝**"（429 语义），
    它是给**别人**用的（保护我们的接口不被刷）。而这里要的是
    "我们别打太快，**排队等一等**"—— 我们自己是被限流的一方。

    两者一个拒一个等，复用会得到一个语义错位的类。

    ## 为什么允许多余的突发（`burst`）

    一次同步会连着发十几个请求（赛事 → 阶段 → 题目）。让它们全部
    排队会白白慢一个数量级，而平台侧真正在意的是**长期速率**。
    桶容量就是"允许一次突发多少个"。

    ## 时钟与 sleep 是**注入**的

    这让测试不必真的等：传一个假的单调时钟与一个立刻返回的 sleep
    就能把限流逻辑跑完整。⛔ **不是**为了"生产里可换"—— 生产永远
    用默认值。
    """

    def __init__(
        self,
        rate_per_second: float,
        burst: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate_per_second <= 0:
            raise ValueError("速率必须为正数：0 或负数等于永久阻塞。")
        if burst < 1:
            raise ValueError("桶容量至少为 1，否则永远拿不到令牌。")
        self._rate = float(rate_per_second)
        self._capacity = float(burst)
        self._tokens = float(burst)
        self._clock = clock
        self._sleep = sleep
        self._updated_at = clock()
        self._lock = asyncio.Lock()

    async def acquire(self) -> float:
        """拿一个令牌，必要时等待。返回等了多久（秒），给测试断言用。

        ⚠️ `await self._sleep()` **在锁里**，这是刻意的：它把并发调用
        串行化成"按到达顺序排队取令牌"。放到锁外的话，N 个并发请求会
        同时算出"我还差 0.1 秒"然后同时睡同时醒 —— 等于没有限流。

        代价是一次只能有一个等待者在睡，吞吐上限就是 `rate`。
        对"同步几十道题"这个场景完全够用，而这正是我们要的形状。
        """
        waited = 0.0
        async with self._lock:
            while True:
                now = self._clock()
                elapsed = max(0.0, now - self._updated_at)
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
                self._updated_at = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return waited
                delay = (1.0 - self._tokens) / self._rate
                waited += delay
                await self._sleep(delay)


# ══════════════════════════════════════════════════════════
# 重试策略
# ══════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """指数退避的参数（上限与抖动都可配）。"""

    max_attempts: int = 4
    base_delay: float = 0.5
    max_delay: float = 30.0
    # 抖动比例（0~1）。**必须非零**：完全确定的退避会让所有并发调用
    # 在同一毫秒醒来，形成"雪崩式重试"—— 而那种模式恰好会再次触发
    # 平台的限流，于是永远好不了。
    #
    # 0.25 是"足够打散"与"不明显变慢"之间的折中：退避 10 秒时
    # 实际落在 7.5~12.5 秒之间。
    jitter_ratio: float = 0.25
    # ★ `Retry-After` 的上限。超过它就不再干等，直接抛 `Ctf2RateLimited`。
    #
    # 为什么需要这个上限：`Retry-After` 是**服务端给的**，它可以给
    # 86400（一天）。老老实实睡一天会把一个 worker 挂死一整天，
    # 而请求方（前端）早就超时走了 —— 那是资源泄漏，不是"尊重平台"。
    # 尊重平台在这里的正确表达是"停下来，把这个情况报给人"。
    max_retry_after: float = 120.0

    def backoff(self, attempt: int, *, rng: random.Random | None = None) -> float:
        """第 `attempt` 次失败后睡多久（`attempt` 从 1 开始）。

        抖动用**乘法**（`delay * (1 ± ratio)`）而不是加法：加法在小
        delay 上会算出负数或接近零的值（退避等于没退），而乘法在
        整个区间上都是成比例的。
        """
        raw = min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))
        randomizer = rng or random
        if self.jitter_ratio <= 0:
            return raw
        factor = 1.0 + randomizer.uniform(-self.jitter_ratio, self.jitter_ratio)
        return max(0.0, raw * factor)


# ══════════════════════════════════════════════════════════
# 环境的返回结构
# ══════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class EnvironmentAccess:
    """一次环境启动的**结果**（不是中间态）。"""

    access_url: str | None
    access_urls: tuple[str, ...]
    # ★ 平台给的**接入类型**（`http` / `nc` …）—— 它是**权威信号**。
    #
    # 实测（2026-10-01，就绪帧原文）：
    #   {"access_ready": true, "access_type": "http",
    #    "access_url": "http://…:80",
    #    "access_urls": [{"nc_ssl": null, "type": "http", "url": "http://…:80"}],
    #    "environment_id": "…", "expires_at": "…", "status": "running"}
    #
    # ⚠️ 我们那个 `needs_raw_network(category)` 是**按题型猜**的，而这一个
    #    是平台直接告诉我们的。两者不一致时以它为准 ——
    #    猜错的代价是"一整类题连不上"（见 runner 里那段）。
    access_type: str = ""
    # 实际轮询了几次。**之前从未返回过**，于是 `service` 只能写死 1 ——
    # 界面上那句"轮询 1 次"一直是假的（实测这道题轮询了 2 次）。
    polls: int = 0
    # 平台给的过期时刻（ISO 串）。环境有租期（实测 1 小时），
    # 落库后界面才能提示"快到期了"，而不是等它静默失效。
    expires_at: str = ""
    # 平台原样返回的那份 JSON。留它是因为 CTF2 可能给额外字段
    # （过期时间、提示信息），而我们还没建模 —— 排查时能看到
    # 当时的原始响应，比事后重新调一次 API 可靠（数据已经变了）。
    raw: dict[str, Any] = field(default_factory=dict)


# ══════════════════════════════════════════════════════════
# 客户端
# ══════════════════════════════════════════════════════════


class Ctf2Client:
    """CTF2 Open API 的异步客户端。

    用法：

    ```python
    async with Ctf2Client(token=pat) as client:
        comps = await client.list_competitions()
        access = await client.start_environment_until_ready(
            practice_id="123", challenge_id="456"
        )
    ```

    ## 为什么 `client` 可以注入

    注入 `httpx.AsyncClient` 是**测试**需要的（走 `MockTransport`，
    于是 URL 构造、请求头、状态码解释、重试逻辑全都真的跑一遍，
    只有网络那一层是假的）。这比 mock 掉 `list_competitions()`
    有价值得多 —— 后者会把最值得测的那部分一起跳过。
    与 `tests/factories.patch_llm_transport` 是同一条思路。

    ## 幂等性

    `start_environment` 是**幂等**的（平台侧保证），所以
    `start_environment_until_ready` 可以安全地重复调用它；
    `submit_flag` **不幂等**（每次调用都算一次提交，错的会扣额度），
    所以它的重试策略是"只重试**确定没送达**的失败"
    （见 `submit_flag` 的说明）。
    """

    def __init__(
        self,
        *,
        token: str,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
        rate_per_second: float = 5.0,
        burst: int = 10,
        retry: RetryPolicy | None = None,
        timeout_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not token:
            # 早失败。放行的话每个请求都会得到 401，而 401 的补救动作是
            # "重新授权"—— 而真实情况是"调用方忘了传 token"，
            # 那会把排查方向指到完全错误的地方。
            raise ValueError("Ctf2Client 需要一条 PAT；没有凭据时不应构造客户端。")

        self._token = token
        self._base_url = base_url.rstrip("/")
        # 构造时就校验 base_url：如果它被配成了非官方域名，
        # 那么**每一次**请求都会把 PAT 发到那里 —— 这是凭据外泄，
        # 不是配置错误。宁可在构造时炸，也不要发出第一个请求。
        assert_allowed_target(self._base_url)
        self._retry = retry or RetryPolicy()
        self._clock = clock
        self._sleep = sleep
        self._bucket = TokenBucket(rate_per_second, burst, clock=clock, sleep=sleep)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            # 不跟随重定向：**重定向会把 Authorization 头带到另一个主机**。
            # httpx 默认只在同源时保留凭据头，但那是一条依赖库行为的保证；
            # 这里是凭据，不赌。
            follow_redirects=False,
            # ★ `trust_env=False` —— 这里**不是** SSRF 面（`base_url` 是官方
            #   域名，且构造时 `assert_allowed_target` 就钉死了），但这条
            #   client **专门用来发 PAT**：`trust_env` 默认 `True` 会让请求
            #   走环境变量里的 `HTTP_PROXY`，那等于**把 PAT 交给那个代理**。
            #   同一个理由也适用于它顺带关掉的 `.netrc`。
            trust_env=False,
            # ★ `GuardedAsyncTransport` —— 连接层判据（2026-10-16 补）。
            #   ⚠️ 这一处的收益**比另外三处小**：`base_url` 在构造时就被
            #   `assert_allowed_target` 钉死在官方域名上，所以"管理员填了
            #   一个会解析到 link-local 的域名"这条路径本来就不成立。
            #   加它是为了堵另一种情形：**官方域名的 DNS 被劫持/投毒**到
            #   link-local。那条路 `assert_allowed_target` 看不见
            #   （它比的是字符串）。代价是零 —— 与另外三处同一个形状。
            transport=netguard.GuardedAsyncTransport(),
        )
        if self._owns_client:
            # fail-closed：确认 guard 装在所有池上（含代理池）。
            # ⚠️ **只对自己建的 client 做**：注入进来的那个属于调用方，
            #    去改它的连接池是"改别人的对象"，会让调用方莫名其妙地
            #    被 raise（比如它自己带了一个代理池）。
            netguard.ensure_guarded(self._client)

    # ── 生命周期 ──────────────────────────────────────────

    async def aclose(self) -> None:
        """只关**自己建的** client。注入进来的由调用方负责 ——
        否则调用方会在自己的 client 还能用时发现它被关了。"""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> Ctf2Client:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    # ── 请求底座 ──────────────────────────────────────────

    def _url(self, path: str) -> str:
        """拼 API URL。`path` 一律以 `/` 开头的**相对段**。"""
        if not path.startswith("/"):
            path = "/" + path
        return f"{self._base_url}{CTF2_API_PREFIX}{path}"

    def _headers(self) -> dict[str, str]:
        # 每请求现拼而不是构造时存一份 dict：存一份意味着这个 dict
        # 长驻在内存里，而它是**含明文凭据**的。现拼的窗口更小。
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }

    @staticmethod
    def _safe_excerpt(response: httpx.Response, limit: int = 300) -> str:
        """响应片段，给 detail 用。**过一遍脱敏**。

        为什么不是"直接取 text"：平台可能因为某种原因把请求头回显在
        错误消息里（见过这种实现），而那样一来这段 detail 就带着 PAT，
        而 detail 是会进日志与异常界面的。过一遍 `redact` 的成本是
        一次正则，收益是这条路径**不可能**泄漏凭据。
        """
        try:
            text = response.text
        except Exception:  # noqa: BLE001 - 取不到就算了，不该因为它再抛一次
            return ""
        return redact(text[:limit])

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        retryable: bool = True,
    ) -> Any:
        """发一次请求并解释结果。**所有端点都走它。**

        `retryable=False` 用于**不幂等**的操作（提交 flag）——
        见 `submit_flag` 的说明：那里只重试确定没送达的失败。
        """
        url = self._url(path)
        policy = self._retry
        attempt = 0

        while True:
            attempt += 1
            # 限流在**发之前**拿令牌。放在重试循环里面（而不是外面）
            # 是刻意的：一次重试也是一次真实请求，同样要计入速率。
            await self._bucket.acquire()

            try:
                response = await self._client.request(
                    method, url, json=json_body, headers=self._headers()
                )
            except httpx.HTTPError as exc:
                # 网络层失败。**不确定请求有没有送达** ——
                # 这正是 `retryable` 要区分的东西。
                if not retryable or attempt >= policy.max_attempts:
                    raise Ctf2Unavailable(
                        f"连接 {redact(url)} 失败：{exc.__class__.__name__}。"
                        "请检查网络与平台可用性。"
                    ) from exc
                await self._sleep(policy.backoff(attempt))
                continue

            status = response.status_code

            # ── 401：不重试 ────────────────────────────────
            if status == 401:
                raise Ctf2AuthExpired(
                    "CTF2 拒绝了这条凭据（401）。它可能已过期、被吊销，"
                    "或复制时被截断 —— 请重新绑定账号。"
                    + _identity_hint(self._error_identity(response)),
                    detail=self._safe_excerpt(response),
                )

            # ── 403：不重试，带出缺失的 scope ──────────────
            if status == 403:
                scope = self._missing_scope(response)
                raise Ctf2ScopeMissing(
                    "CTF2 拒绝了这次调用（403）：这条凭据缺少所需权限。"
                    + _identity_hint(self._error_identity(response)),
                    missing_scope=scope,
                    detail=self._safe_excerpt(response),
                )

            # ── 429：按 Retry-After 等 ────────────────────
            if status == 429:
                wait = self._retry_after(response, policy)
                if not retryable or attempt >= policy.max_attempts:
                    raise Ctf2RateLimited(
                        "CTF2 限流（429），已重试到上限。请稍后再试。",
                        retry_after=wait,
                        detail=self._safe_excerpt(response),
                    )
                if wait > policy.max_retry_after:
                    # 见 `RetryPolicy.max_retry_after` 的说明：
                    # 干等一整天是资源泄漏，不是尊重平台。
                    raise Ctf2RateLimited(
                        f"CTF2 要求等待 {wait:.0f} 秒后重试，超过本次调用的等待上限"
                        f"（{policy.max_retry_after:.0f} 秒）。请稍后再发起同步。",
                        retry_after=wait,
                        detail=self._safe_excerpt(response),
                    )
                logger.info("CTF2 限流，%.1fs 后重试（第 %d 次）", wait, attempt)
                await self._sleep(wait)
                continue

            # ── 5xx / 408：指数退避 ────────────────────────
            if status >= 500 or status == 408:
                if not retryable or attempt >= policy.max_attempts:
                    raise Ctf2Unavailable(
                        f"CTF2 返回 {status}，已重试到上限（{policy.max_attempts} 次）。"
                        "这通常是平台侧故障，请稍后再试。",
                        detail=self._safe_excerpt(response),
                    )
                delay = policy.backoff(attempt)
                logger.info("CTF2 返回 %d，%.1fs 后重试（第 %d 次）", status, delay, attempt)
                await self._sleep(delay)
                continue

            # ── 其余 4xx：不重试 ──────────────────────────
            if status >= 400:
                error_code, error_key = self._error_identity(response)
                raise Ctf2ResponseError(
                    f"CTF2 拒绝了这次请求（{status}）。可能是参数不对，"
                    "或平台接口有变更。"
                    # ⚠️ 这里也带上平台标记：404 的 `params.resource == "route"`
                    #    是"**路由**不存在"的判据（而不是"资源不存在"）——
                    #    两者要采取的行动完全不同（前者是接口变了，后者是 id 错了）。
                    + _identity_hint((error_code, error_key)),
                    status_code=status,
                    # ★ 机器码要交给调用方：`ALREADY_SOLVED` 与"参数写错了"
                    #   同为 400，但前者意味着这题已经不用做了。
                    code=error_code,
                    detail=self._safe_excerpt(response),
                )

            return self._decode(response)

    @staticmethod
    def _decode(response: httpx.Response) -> Any:
        """解析响应体。

        ⚠️ **空体是合法的**（DELETE 类端点常常 204）。
        返回 `None` 而不是报错 —— 报错会让"停止环境"这种端点在
        平台改成一个空响应时突然全部失败。

        ⚠️ 非 JSON 的 2xx 也**不报错**，返回原文：CTF2 的某些端点
        （尤其是提交）在成功时会返回一段纯文本。硬要求 JSON 会把
        一次成功的提交判成失败，而后果是**重复提交**（扣额度）。
        这个方向的错误比"少解析一层"贵得多。
        """
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return {"raw_text": response.text}

    @staticmethod
    def _retry_after(response: httpx.Response, policy: RetryPolicy) -> float:
        """读 `Retry-After`。

        支持两种形态（RFC 9110 允许的两种）：
        · 秒数（`Retry-After: 30`）；
        · HTTP-date（`Retry-After: Wed, 21 Oct 2026 07:28:00 GMT`）。

        ⚠️ 两种都要认。只认秒数的话，平台改用 date 形态时我们会
        **静默退回退避**（读不到就退回 base_delay），于是实际等待
        比平台要求的短得多 —— 然后被继续 429，而日志里看不出原因。

        读不到或认不出时退回 `base_delay`：**不猜**一个大数字，
        也不假装平台没限流（等 0 秒等于立刻重试，会把限流打成更严重）。
        """
        raw = response.headers.get("retry-after")
        if not raw:
            return policy.base_delay
        raw = raw.strip()
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
        try:
            from datetime import datetime
            from email.utils import parsedate_to_datetime

            moment = parsedate_to_datetime(raw)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=UTC)
            delta = (moment - datetime.now(UTC)).total_seconds()
            return max(0.0, delta)
        except (TypeError, ValueError):
            logger.warning("无法解析 Retry-After=%r，退回默认退避", raw)
            return policy.base_delay

    @staticmethod
    def _error_identity(response: httpx.Response) -> tuple[str | None, str | None]:
        """取出平台错误信封里的 `(code, key)`。

        ## 这是**实测**出来的形状，不是猜的

        对 `https://ctf2.dasctf.com/api/open/v1` 发未鉴权请求时，
        平台回的是：

        ```json
        {"error": {"code": "AUTH_REQUIRED",
                   "key": "errors.auth.permission_denied",
                   "params": {}}, "success": false}
        ```

        ⚠️ `key` 是一个**i18n 文案键**（`errors.auth.*`），`code` 是
        机器码。两者都**不是** scope 名 —— 所以它们只用于让报错更
        可诊断，绝不喂给 `missing_scope`（见那个函数的说明）。

        ⛔ 这是别人家的接口，形状随时可能变。认不出来就返回
        `(None, None)`，报错文案退回通用说法。
        """
        try:
            payload = response.json()
        except ValueError:
            return None, None
        if not isinstance(payload, dict):
            return None, None
        error = payload.get("error")
        container = error if isinstance(error, dict) else payload
        code = container.get("code")
        key = container.get("key")
        return (
            code.strip() if isinstance(code, str) and code.strip() else None,
            key.strip() if isinstance(key, str) and key.strip() else None,
        )

    @staticmethod
    def _missing_scope(response: httpx.Response) -> str | None:
        """从 403 的响应里挖出缺失的 scope。

        ## 为什么要挖而不是"约定一个字段名"

        这是**别人家的**响应结构，我们能做的是尽量认几种常见的形状
        （`missing_scope` / `required_scope` / `scope`，平铺或在
        `error` / `detail` / `data` 之下）。认不出来返回 None ——
        那时异常给一句没有 scope 的通用说法，仍然可用。

        ⚠️ **实测结论**：上面那个 `AUTH_REQUIRED` 信封里**没有**
        scope 字段（`params` 是空的）。所以本函数在真实平台上
        大概率返回 `None` —— 这是如实记下的现状，不是没做。
        保留识别逻辑的理由是它会随平台演进变化，而"少了一层解析"
        的代价远小于"猜一个错的名字"。

        ⛔ 不去猜、不去推断"哪个 scope 大概缺了"：一个**错的** scope
        名比没有更坏，用户会照着去勾一个不相干的权限，然后问题照旧。
        """
        try:
            payload = response.json()
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None

        candidates: list[dict[str, Any]] = [payload]
        for key in ("error", "detail", "data"):
            nested = payload.get(key)
            if isinstance(nested, dict):
                candidates.append(nested)
                # `params` 也是一层（实测信封里有它，虽然目前是空的）。
                deeper = nested.get("params")
                if isinstance(deeper, dict):
                    candidates.append(deeper)

        for container in candidates:
            for key in ("missing_scope", "required_scope", "scope", "missingScope"):
                value = container.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
                # 有些实现给的是列表（缺多个）。
                if isinstance(value, list) and value:
                    names = [str(v) for v in value if str(v).strip()]
                    if names:
                        return ", ".join(names)
        return None

    # ── 端点：赛事与题目 ───────────────────────────────────

    async def list_competitions(self) -> list[dict[str, Any]]:
        """`GET competitions/` —— 可见的赛事列表。"""
        return _as_list(await self._request("GET", "/competitions/"))

    async def competition_status(self, competition_id: str) -> dict[str, Any]:
        """`GET competitions/{id}/status/` —— 一场赛事的当前状态。"""
        return _as_dict(
            await self._request("GET", f"/competitions/{competition_id}/status/")
        )

    async def list_stages(self, competition_id: str) -> list[dict[str, Any]]:
        """`GET competitions/{id}/stages/` —— 赛事的阶段列表。"""
        return _as_list(
            await self._request("GET", f"/competitions/{competition_id}/stages/")
        )

    async def list_stage_challenges(self, stage_id: str) -> list[dict[str, Any]]:
        """`GET stages/{stageId}/challenges/` —— 一个阶段下的题目。"""
        return _as_list(await self._request("GET", f"/stages/{stage_id}/challenges/"))

    async def list_stage_submissions(self, stage_id: str) -> list[dict[str, Any]]:
        """`GET stages/{stageId}/submissions/` —— 我们在该阶段的提交记录。

        它的用途是**对账**：我们本地可能有 `pending` 的提交（进程在
        发出请求后崩了），而这个端点能告诉我们平台到底收到了没有。
        没有它，那类提交只能永远悬着。
        """
        return _as_list(await self._request("GET", f"/stages/{stage_id}/submissions/"))

    async def list_practice(self) -> list[dict[str, Any]]:
        """`GET practice/` —— 练习集列表（与赛事是两套）。"""
        return _as_list(await self._request("GET", "/practice/"))

    async def list_writeups(self) -> list[dict[str, Any]]:
        """`GET writeups/` —— 公开的已通过解法。

        ★ 它**不是**"题目列表"端点，但它是唯一能**批量**拿到
        `challenge_id` 的地方 —— 每条解法都带着它讲的是哪道题。

        实测（2026-09-30）：把它的 98 个 `challenge_id` 逐个拿去撞练习集，
        **98/98 全部命中**，全是真实可提交的练习题（BUUCTF 96、
        N1BOOK 1、BUUCTF-Real 1）。

        ⚠️ 这推翻了一个我先前的**错误结论**（"批量拉题在这个平台上
        不可行"）。那个结论建立在"只试了 5 个练习集里的 2 个"的样本上，
        而 96/98 恰好落在没试到的那个（BUUCTF）下面。样本取错，
        结论就反了 —— 记在这里是为了下次别再用半个样本下判断。
        """
        return _as_list(await self._request("GET", "/writeups/"))

    async def list_daily(self) -> list[dict[str, Any]]:
        """`GET daily/` —— 每日一练。

        ⚠️ 实测：这 25 道题拿去撞 5 个练习集是 **0 命中** ——
        它们不属于任何练习集，而提交端点要求 `practice_id`，
        所以**每日一练无法提交**。这个端点只对"看看题面"有用。
        """
        return _as_list(await self._request("GET", "/daily/"))

    async def get_practice_challenge(
        self, practice_id: str, challenge_id: str
    ) -> dict[str, Any]:
        """`GET practice/{id}/challenges/{challengeId}/` —— 练习题的详情。

        同步时用它拿 `sub_flag_id`（套题提交必须带，见
        `models.Ctf2Challenge` 的说明）。
        """
        return _as_dict(
            await self._request(
                "GET", f"/practice/{practice_id}/challenges/{challenge_id}/"
            )
        )

    # ── 端点：提交 ─────────────────────────────────────────

    async def submit_flag(
        self,
        *,
        practice_id: str,
        challenge_id: str,
        flag: str,
        sub_flag_id: str | None = None,
    ) -> dict[str, Any]:
        """`POST practice/{id}/challenges/{challengeId}/submit/`。

        ## ★ 这个调用**不幂等**，所以它的重试策略与别处不同

        `retryable=False` —— 只重试**确定没有送达**的失败。

        为什么区别对待：一次提交被平台收到就算数（对的记为解出，
        错的**扣一次额度**）。如果我们在"响应超时"后重试，而那次请求
        实际上已经送达，就白白多扣一次额度 —— 而额度是不可再生的。

        所以这里的取舍是**宁可漏，不可重**：
        · 网络连接没建立就失败（`ConnectError`）→ 重试是安全的；
        · 响应超时（`ReadTimeout` / 已发出）→ **不重试**，
          让它留在 `pending`，由 `list_stage_submissions` 对账。

        ⚠️ 当前实现把**所有** `httpx.HTTPError` 都当"不确定"处理
        （即不重试），把这个安全侧做实。要做到"只对 ConnectError 重试"
        需要在 `_request` 里再分一层异常类型 —— 那层细分值得做，
        但它依赖**尚未验证过的**平台实际行为，所以先不做（见交付说明
        的假设清单）。

        ## `confirmation`：**必填**，不是可选项

        官方 OpenAPI 文档（`https://ctf2.dasctf.com/api/openapi/v1/user.json`，
        公开可下载）把提交体定义为必填 `flag` + `confirmation`。
        实测（2026-09-29，真实平台，`practice=BUUCTF` 的 hashcat 题）：

            {flag}                      → 400 INVALID_REQUEST
                                          params: {confirmation: true}
            {flag, confirmation: false} → 400（同上）
            {flag, confirmation: true}  → 200

        少了它提交**根本发不出去**，而平台的报错把矛头指向"这个字段
        得是 true"。所以这里恒传 `True`：能走到这一层的调用，都已经
        在 UI 上过了确认那一步。

        ## `sub_flag_id`

        套题必须带、且必须是**稳定的**那个（见 models 的说明）。
        普通题不传 —— 带一个空的 `sub_flag_id` 会让平台去校验一个
        不存在的套题位置，而报错看起来像"题目配置有问题"。
        """
        body: dict[str, Any] = {"flag": flag, "confirmation": True}
        if sub_flag_id:
            body["sub_flag_id"] = sub_flag_id
        return _as_dict(
            await self._request(
                "POST",
                f"/practice/{practice_id}/challenges/{challenge_id}/submit/",
                json_body=body,
                retryable=False,
            )
        )

    # ── 端点：环境 ─────────────────────────────────────────

    async def start_environment(
        self, *, practice_id: str, challenge_id: str
    ) -> dict[str, Any]:
        """`POST practice/{id}/challenges/{challengeId}/environment/start/`。

        ⚠️ **这是幂等的**（平台的契约）：重复调用不会重复计费、
        也不会把已经起好的环境重启掉。所以可以安全地放进轮询循环，
        也可以安全地在重试里重发。

        它通常**不会**在第一次返回 `access_ready=true` ——
        平台要调度容器、分配地址，所以必须轮询。轮询见
        `start_environment_until_ready`。
        """
        return _as_dict(
            await self._request(
                "POST",
                f"/practice/{practice_id}/challenges/{challenge_id}/environment/start/",
                json_body={},
            )
        )

    async def stop_environment(self, *, practice_id: str, challenge_id: str) -> None:
        """`DELETE practice/{id}/challenges/{challengeId}/environment/`。

        用途是**释放资源**：题目做完了、或沙箱起不来要重来。
        不调它的代价是平台侧的环境一直占着（有些平台计时收费），
        所以"解题结束"的收尾路径上一定要调它（见 `runner.run_worker`
        的 `finally`）。
        """
        await self._request(
            "DELETE",
            f"/practice/{practice_id}/challenges/{challenge_id}/environment/",
        )

    async def start_environment_until_ready(
        self,
        *,
        practice_id: str,
        challenge_id: str,
        total_timeout_seconds: float = 300.0,
        interval_seconds: float = 3.0,
        max_polls: int = 200,
    ) -> EnvironmentAccess:
        """幂等轮询直到 `access_ready=true`，带**总超时**。

        ## 为什么是"重复调 start"而不是"调 start 一次然后查状态"

        CTF2 没有单独的"查环境状态"端点 —— 官方给的方式就是重复调
        start，它幂等、每次都返回当前进展。所以这个循环的每一次迭代
        都是一次 start 调用，这是**遵循平台的设计**，不是偷懒。

        ## 三个上限，都不能省

        · `total_timeout_seconds` —— **墙钟**总时限。它是最终保险：
          即使平台一直返回"还在起"，我们也要能停下来。
        · `interval_seconds` —— 轮询间隔。3 秒是"平台通常几十秒内起好"
          与"不要把对方打成限流"之间的折中。
        · `max_polls` —— 次数上限。**与墙钟不同**：如果某次调用本身
          很快（比如平台立刻返回 false），墙钟要 300 秒才能拦下来，
          而那会变成上百次请求打在同一秒上 —— 平台角度看就是攻击。
          次数上限把这种情况收在 200 次以内。

        三者取**先到者**：任一触发即抛 `Ctf2EnvironmentTimeout`。

        ## ★ 为什么用 `self._clock()` 而不是 `time.monotonic()` 直接调

        注入时钟让测试能把"300 秒总超时"在微秒内跑完。
        与 `TokenBucket` 同一条理由：测试不该真的等。

        ## 超时 ≠ 失败

        抛的是 `Ctf2EnvironmentTimeout` 而**不是**"环境启动失败"
        （后者不存在，因为"环境启动失败"平台没有明确的信号 ——
        它只是一直不说 ready）。**环境可能还在起**，所以补救动作是
        "过一会儿再调一次这个方法"（幂等，安全），不是"去看日志"。
        """
        started = self._clock()
        deadline = started + total_timeout_seconds
        polls = 0
        # 被限流而多等的次数。**不计入 `polls`**：`polls` 的语义是
        # "问过平台多少次环境好了没"，而一次 429 什么都没问出来。
        # 它的上限由墙钟（`deadline`）负责，见下面 429 分支里的说明。
        rate_limited_waits = 0
        last_payload: dict[str, Any] = {}

        while True:
            if polls >= max_polls:
                raise Ctf2EnvironmentTimeout(
                    f"题目环境启动轮询次数已达上限（{max_polls} 次），仍未就绪。"
                    "环境可能仍在启动，请稍后重试。",
                    polls=polls,
                    elapsed_seconds=self._clock() - started,
                    detail=_safe_json(last_payload),
                )

            try:
                last_payload = await self.start_environment(
                    practice_id=practice_id, challenge_id=challenge_id
                )
            except Ctf2ResponseError as exc:
                if exc.status_code == 404:
                    # ★「这道题没有靶机环境」—— `has_container: false`
                    #
                    # 实测（2026-09-29，真实平台，`practice=BUUCTF` 的 hashcat 题）：
                    #   GET  .../challenges/{challengeId}/                  → 200（题确实在）
                    #   POST .../challenges/{challengeId}/environment/start/ → 404
                    #        params.resource == "practice_challenge"
                    #
                    # **同一个组合**上详情读得到、环境端点却 404 —— 这不是
                    # "题目不存在"，而是"这道题本来就没有靶机"。MISC / CRYPTO
                    # 里的多数题都是这一类（纯附件，或只有页面上的交互靶机）。
                    #
                    # 以前 404 一路抛上去，后果是**所有纯附件题在第一行就判死**、
                    # 一道都解不了（实测症状："run_worker 耗时 0.8s，提交记录 0 条"）。
                    #
                    # 返回一份"没有地址"的结果，让解题继续往下走 —— 附件本身
                    # 就是这类题的解题材料，环境不是。
                    return EnvironmentAccess(
                        access_url=None,
                        access_urls=(),
                        raw={
                            "environment": "none",
                            "reason": "environment/start 返回 404（纯附件题）",
                        },
                    )
                raise
            except Ctf2RateLimited as exc:
                # ★★ 429 **不该**作废整个起环境过程。
                #
                # 实测（2026-10-01，用户一次点了 6 道题）：4 道题卡在
                # **起环境**这一步失败，界面显示
                # 「启动失败 · CTF2 限流（429），已重试到上限」，
                # 而每一道都是 `turns=0` —— 一个 token 都没花在解题上。
                #
                # 根因是这个循环原来**没有捕 429**：`_request` 在 4 次重试
                # （平台给的 `Retry-After` 只有 1 秒，所以总共约 4 秒）之后
                # 抛 `Ctf2RateLimited`，它直接穿出循环，把一份 **300 秒的
                # 预算**整个作废。
                #
                # 换句话说：我们因为"被要求等 1 秒"而放弃了"还能等 300 秒"
                # 的机会。那 4 道题的平台限流在 1 分钟内就都解除了。
                #
                # 为什么在这里等是**安全**的：`start_environment` 是幂等的
                # （平台侧保证，见方法 docstring）。重复调用只是问一句
                # "好了没" —— 这不是"重试一个有副作用的动作"，所以它与
                # `submit_flag` 那条"宁可漏不可重"的纪律不冲突。
                #
                # 上限仍然是那三个，一个都没放松：这里多等的每一秒都从
                # **同一个** 300 秒预算里扣，并且下面显式检查了截止时刻。
                #
                # ⚠️ 截止检查必须放在这里。循环顶部那个 `polls >= max_polls`
                #    管不到这条路径（429 不算一次成功的轮询，所以 `polls`
                #    不增），而循环中段的截止检查在 `_is_ready` 之后 ——
                #    如果每一次调用都被限流，就永远走不到那里，变成死循环。
                if self._clock() >= deadline:
                    raise Ctf2EnvironmentTimeout(
                        f"题目环境启动在 {total_timeout_seconds:.0f} 秒内未就绪"
                        f"（其中最后一次是平台限流：{exc.message}）。"
                        "环境可能仍在启动，请稍后重试。",
                        polls=polls,
                        elapsed_seconds=self._clock() - started,
                        detail=_safe_json(last_payload),
                    ) from exc
                # `Retry-After` 缺失或为 0 时退回**正常轮询间隔** ——
                # 退回一个很小的值等于把限流打成更严重的限流。
                wait = (
                    exc.retry_after
                    if exc.retry_after is not None and exc.retry_after > 0
                    else interval_seconds
                )
                remaining = max(0.0, deadline - self._clock())
                wait = min(wait, remaining)
                rate_limited_waits += 1
                logger.info(
                    "起环境被平台限流，%.1fs 后继续轮询（第 %d 次）：%s",
                    wait,
                    rate_limited_waits,
                    challenge_id,
                )
                await self._sleep(wait)
                continue
            polls += 1

            if _is_ready(last_payload):
                return EnvironmentAccess(
                    access_url=_first_str(last_payload, ("access_url", "accessUrl")),
                    access_urls=_url_list(last_payload),
                    # ★ 这三个都是**实测有、而我们一直没取**的字段：
                    #   · `access_type` 决定沙箱要不要放开裸网络；
                    #   · `polls` 让界面上的"轮询 N 次"不再是假的 1；
                    #   · `expires_at` 让环境租期可见（默认 1 小时）。
                    access_type=_first_str(last_payload, ("access_type", "accessType"))
                    or "",
                    polls=polls,
                    expires_at=_first_str(last_payload, ("expires_at", "expiresAt")) or "",
                    raw=last_payload,
                )

            if self._clock() >= deadline:
                raise Ctf2EnvironmentTimeout(
                    f"题目环境启动在 {total_timeout_seconds:.0f} 秒内未就绪"
                    f"（已轮询 {polls} 次）。环境可能仍在启动，请稍后重试。",
                    polls=polls,
                    elapsed_seconds=self._clock() - started,
                    detail=_safe_json(last_payload),
                )

            # 睡到"下一次轮询"与"总截止"里更早的那个 ——
            # 睡满 interval 会在最后一轮多等一个完整间隔，
            # 那是白白拖长的总时长（实测里这是 3 秒级的可见差异）。
            remaining = deadline - self._clock()
            await self._sleep(min(interval_seconds, max(0.0, remaining)))


# ══════════════════════════════════════════════════════════
# 响应形状的小工具
# ══════════════════════════════════════════════════════════
#
# ## 为什么容忍"裸列表"与"包了一层"两种形状
#
# CTF2 是分页风格的 Open API，而分页响应有两种常见包法：
# `[{...}]` 与 `{"results": [{...}]}` / `{"data": {...}}`。
#
# ★ **刻意不在这里做分页**：分页属于 `service.py`（它知道"要同步多少"），
#   而这一层只负责"把这一页的形状抹平"。若把分页也塞进来，
#   这个文件就要开始理解业务侧的"同步范围"，那是两类知识的混合。
#
# 返回类型一律是 `list[dict]` 或 `dict` —— 不做 Pydantic 建模。
# 理由：这是**别人家的**契约，建模它意味着每次平台加字段都要动我们的类，
# 而加字段本来不该让任何东西失败。转换成我们的模型是 service 的事。


def _identity_hint(identity: tuple[str | None, str | None]) -> str:
    """把 `(code, key)` 变成一句可直接贴在报错后面的话。

    实测过的一件事：平台在 401 时给的是 `AUTH_REQUIRED`，
    **即使我们确实带了凭据**（那就是"凭据不被接受"）。所以这句话
    前半段只做**引用**（"平台标记：…"），不替用户下结论 —— 我们不知道
    `AUTH_REQUIRED` 到底是"没带"还是"不认"，而给一个确定的错结论
    会把人引向错误的方向。

    ★ 后半段是一张**已知键 → 人话**的小表（`_ERROR_KEY_HINTS`）。
    它存在的理由很具体：`errors.team.membership_required` 这种键
    不会出现在任何用户看得懂的地方 —— 而它恰好是"为什么同步不了赛事题目"
    的**真正**答案。用户看到"平台标记：TEAM_MEMBERSHIP_REQUIRED"
    只会一头雾水，看到"这个账号还不是该场次的战队成员"才知道该做什么。

    ⛔ 表里只放**实测确认过**的键。没见过的键原样引用（见上：不猜）。
    两者都没有时返回空串，报错文案自然退回通用说法。
    """
    code, key = identity
    if key and code:
        head = f"平台标记：{code}（{key}）。"
    elif key or code:
        head = f"平台标记：{key or code}。"
    else:
        return ""

    plain = _ERROR_KEY_HINTS.get(key or "", "")
    return f"{head}{plain}" if plain else head


def _as_list(payload: Any) -> list[dict[str, Any]]:
    """把一个响应变成"对象列表"。认不出来返回空表（不抛）。

    ## ★ 信封与列表容器是**两件事**，必须分开找

    原来的版本只在**顶层**找一个容器键（`results` / `items` / `data` / `list`）。
    而实测的平台形状是：

    ```json
    {"data": {"items": [ … ], "total": 5}, "success": true}
    ```

    列表既不在顶层、键名也不是 `data` —— 它在 **`data` 里面**，键名是 `items`。
    于是每一次同步都拿到空表，日志里只有一句"列表形状不认识"：

        同步完成：赛事 0 · 新增题目 0

    而用户看到的是"同步不了题目"（**不是报错**，是"完成了但什么都没有"）。

    ⚠️ 所以这里把两层分开：

    * **信封**（`data` / `result` / `payload`）—— 外面那层包装，可能缺席；
    * **容器**（`items` / `results` / `list` / `records`）—— 装东西的那个键。

    两者各自可能缺席，所以要**组合着找**（顶层容器 → 信封里的容器），
    而不是把十几个键平铺成一张表去碰运气。

    ## ⛔ 不做"随便找一个列表"的兜底

    一个很诱人的兜底是"信封里只有一个列表就用它"。**刻意不做**：
    那样一来 `{"data": {"scopes": [...]}}` 会被当成题目列表，
    于是库里多出一堆垃圾行，而日志里一声不响。
    认不出来时**出声**（warning 带上顶层键名），比静默猜错好：
    猜错的方向是"数据看起来有了，但全是错的"，那比"什么都没有"难查得多。
    """
    if payload is None:
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []

    # ① 顶层就是容器
    for key in _LIST_CONTAINER_KEYS:
        nested = payload.get(key)
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, dict)]

    # ② 剥一层信封再找容器（实测的真实形状走的就是这一条）
    for envelope in _ENVELOPE_KEYS:
        inner = payload.get(envelope)
        if isinstance(inner, list):
            return [item for item in inner if isinstance(item, dict)]
        if isinstance(inner, dict):
            for key in _LIST_CONTAINER_KEYS:
                nested = inner.get(key)
                if isinstance(nested, list):
                    return [item for item in nested if isinstance(item, dict)]

    # 认不出来就返回空表而不是报错：一次同步拿到空表，
    # 用户看到的是"这场赛事没有题目"，还可以自己判断；
    # 而抛异常会让整次同步失败，连赛事都同步不下来。
    #
    # ⚠️ 但日志必须说清"顶层长什么样"与"信封里有什么键" ——
    #    否则下一次平台改形状时，排查的人手上只有一句"形状不认识"，
    #    还得自己去猜该看哪一层（这正是这次踩到的）。
    logger.warning(
        "CTF2 响应里的列表形状不认识，顶层键：%s；信封内层：%s",
        sorted(payload.keys())[:8],
        _envelope_summary(payload),
    )
    return []


def _envelope_summary(payload: dict[str, Any]) -> str:
    """给"认不出列表"那句日志补上信封内层的键名（**不含值**）。"""
    parts: list[str] = []
    for envelope in _ENVELOPE_KEYS:
        inner = payload.get(envelope)
        if isinstance(inner, dict):
            parts.append(f"{envelope}={sorted(inner.keys())[:8]}")
    return "；".join(parts) or "（没有信封层）"


def _as_dict(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        # 平台有些端点把主体包在 `data` 下。只在顶层只有很少几个键时剥 ——
        # 否则会把一个"本来就有 data 字段的业务对象"误剥一层。
        nested = payload.get("data")
        if isinstance(nested, dict) and len(payload) <= 3:
            return nested
        return payload
    return {}


def _first_str(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _url_list(payload: dict[str, Any]) -> tuple[str, ...]:
    """收集 `access_urls`（可能缺席、可能是单串、可能是列表、**列表项可能是对象**）。

    ★ 单串也要收：平台在只给一个地址时可能返回字符串而不是单元素列表。
    不处理的话，`access_urls` 会是空 —— 而 runner 那边靠它决定
    "往哪些地址发请求"，空表意味着**一个地址都不试**。

    ## ★★ 列表项是**对象**这一条，是被一个真实事故改出来的

    实测（2026-10-01，[护网杯 2018]easy_tornado）：CTF2 的 `access_urls`
    实际返回的是**对象数组**：

        [{"nc_ssl": null, "type": "http", "url": "http://xxx.http-ctf2.dasctf.com:80"}]

    而旧实现是 `str(v)` —— 它把整个 dict **repr 成字符串**：

        "{'nc_ssl': None, 'type': 'http', 'url': 'http://xxx…:80'}"

    这一个 `str()` 同时打穿了**两条**链路，而两条都不会报错：

    · `allowed_hosts()` 从这串文本里抽不出主机名（`urlsplit` 拿到的
      scheme 是 `{'nc_ssl'`）→ 允许集合是**空集** →
      模型对靶机的**每一次**请求都被 `guard_tool_url` 拒掉；
    · `_initial_prompt()` 把同一份值渲染进提示词 → 模型看到的
      "可访问地址"就是那串垃圾，**它根本不知道靶机在哪** ——
      于是去猜 `cf2.dasctf.com` 之类，然后被拒，40 轮耗尽。

    表现出来的样子极具误导性：环境记录正常、`access_urls` 非空、
    界面上一切正常，只有模型那边全军覆没 —— 看起来像"模型太笨"。
    """

    def pick_one(item: Any) -> str:
        """从一项里取出 URL 字符串。项可能是 `str`，也可能是对象。"""
        if isinstance(item, str):
            return item.strip()
        if isinstance(item, dict):
            for key in ("url", "access_url", "accessUrl", "href", "address"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            # ⚠️ **不要**退回 `str(item)` —— 那会把 dict 变成 repr 字符串，
            #    正是上面那个事故。取不出来就返回空，让它被下面的过滤丢掉。
            logger.warning(
                "CTF2 的环境地址项里没有可识别的 url 字段，已跳过：%s",
                sorted(item.keys()),
            )
            return ""
        return str(item).strip()

    for key in ("access_urls", "accessUrls"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return (value.strip(),)
        if isinstance(value, list):
            picked = (pick_one(v) for v in value)
            return tuple(p for p in picked if p)
    return ()


def _is_ready(payload: dict[str, Any]) -> bool:
    """这个响应说环境好了吗。

    ## 判定必须**宽松**，但只对"是"宽松

    CTF2 的字段可能是 `access_ready` / `accessReady` / 或者在
    `environment` 子对象里。所以逐个找。

    ⚠️ 但**只有明确的真值算就绪**（`True` / `"true"` / `"1"`）：
    认不出的东西一律当成"没好"。这是安全的方向 ——
    把"没好"误判成"好了"，会让沙箱对着一个连不上的地址跑一整轮
    （那是本项目真实踩过的形态：32 分钟白跑）；而把"好了"误判成
    "没好"只是多轮询几次，最终超时报错，用户重来一次即可。
    """
    containers: list[dict[str, Any]] = [payload]
    for key in ("environment", "data", "env"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            containers.append(nested)

    for container in containers:
        for key in ("access_ready", "accessReady", "ready"):
            value = container.get(key)
            if value is True or value == 1:
                return True
            if isinstance(value, str) and value.strip().lower() in ("true", "1", "yes"):
                return True

    # ★ 兜底：有 `access_url` 且没有别的信号 —— 那说明环境已经分配了地址。
    #
    # 这条兜底是**必要**的：如果某个端点从不返回 `access_ready`
    # （平台版本差异），只认那个字段会让轮询永远超时，
    # 而环境其实早就好了、地址就在响应里。
    return any(
        container.get("access_url") or container.get("accessUrl")
        for container in containers
    )


def _safe_json(payload: dict[str, Any], limit: int = 500) -> str:
    """把响应塞进异常的 detail 里，**过一遍脱敏**。

    与 `_safe_excerpt` 同一个理由：这段文本会进日志与界面，
    而它装的是平台返回的原始数据 —— 我们没法保证里面没有回显的凭据。
    """
    import json

    try:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = repr(payload)
    return redact(text[:limit])


__all__ = [
    "DEFAULT_BASE_URL",
    "Ctf2AuthExpired",
    "Ctf2Client",
    "Ctf2EnvironmentTimeout",
    "Ctf2Error",
    "Ctf2RateLimited",
    "Ctf2ResponseError",
    "Ctf2ScopeMissing",
    "Ctf2Unavailable",
    "EnvironmentAccess",
    "RetryPolicy",
    "TokenBucket",
    "assert_allowed_target",
    "mask_secret",
]
