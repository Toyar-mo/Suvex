"""比赛平台（AI Agent API）的客户端。照着 `api_doc.md` 逐条实现。

## 这个模块与 `app/ctf2/` 的关系

`app/ctf2/` 对接的是 **ctf2.dasdctf.com** 的开放 API（`Authorization: Bearer <PAT>`、
`{data, success}` 信封、UUID 题目 id）。而这里对接的是**赛事方下发的**
AI Agent API（`X-Agent-AccessKey`、`{code, message, data}` 信封、整数题目 id）。

两套契约不同，所以是**两个 client**；但它们服务同一件事，所以解题那一侧
（沙箱、LLM 循环）应当复用，不该各写一份。

## ★ 三个必须按文档来、否则会静默错的地方

**① 失败也是 HTTP 200。**
文档写明：`code == "00000"` 才算成功，**其他 code 表示失败**。
所以绝不能只看 `response.status_code` —— 那样每一次失败都会被当成成功，
而症状是"提交完了但没生效"，排查方向完全错。

**② 提交"答案错误"不是网络/协议异常，是业务结果。**
`/answer-panel/answer` 在答案错时返回非 `00000`（mock 里是 `A0401`）。
这跟"AccessKey 过期""服务端 500"必须区分开：前者要换下一个候选 flag，
后者要重试或报错。所以这里把提交单独拎出来，返回一个**结果对象**而不是抛异常。

**③ 环境是**异步**的。**
`build-exercise-env` 只是"受理"，之后必须**轮询详情**直到
`isNeedCheck == false` 且 `endpoints` 可用 —— 文档第 4 条写得很明确。
调一次就当好了，会拿到空 `endpoints`，而那时看起来像"题目没有靶机"。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Final

import httpx

from app.core.crypto import redact

logger = logging.getLogger("suvex.arena")

#: 文档里约定的成功码。
SUCCESS_CODE: Final[str] = "00000"

#: 提交接口用到的两个业务码（**实测于 mock，赛事方可能不同，所以只做提示不做判断**）。
CODE_ANSWER_WRONG: Final[str] = "A0401"
CODE_ALREADY_SUBMITTED: Final[str] = "A0409"

#: 默认的 Agent API 路径前缀（文档的 Base URL）。
API_PREFIX: Final[str] = "/slab-match/api/v1/agent"

DEFAULT_TIMEOUT: Final[float] = 30.0


# ══════════════════════════════════════════════════════════
# 异常
# ══════════════════════════════════════════════════════════


class ArenaError(Exception):
    """比赛平台交互失败的基类。"""

    def __init__(self, message: str, *, code: str | None = None, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.message}（code={self.code}）" if self.code else self.message


class ArenaAuthError(ArenaError):
    """AccessKey 缺失/无效。**不重试** —— 重试一万次也是同样的结果。"""


class ArenaUnavailable(ArenaError):
    """网络层失败或服务端 5xx。可以重试。"""


class ArenaNotFound(ArenaError):
    """题目/公告不存在。"""


class ArenaEnvironmentTimeout(ArenaError):
    """轮询到超时环境仍未就绪。"""

    def __init__(self, message: str, *, polls: int, elapsed: float) -> None:
        super().__init__(message)
        self.polls = polls
        self.elapsed = elapsed


# ══════════════════════════════════════════════════════════
# 数据结构
# ══════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class Endpoint:
    """一个靶机入口（`endpoints[]` 的一项）。

    ★ `isProxy` 决定**怎么连**：文档写明"是否优先使用代理连接"。
    代理型要用 `proxyIps` + `portMappings`（平台把靶机端口映射到代理地址上），
    直连型用 `exposeIps` + `ports`。混用会连到一个不可达的地址上，
    而报错是"连接超时" —— 看起来像靶机没起来。
    """

    expose_ips: tuple[str, ...]
    ports: tuple[str, ...]
    users: tuple[tuple[str, str], ...]
    port_mappings: tuple[tuple[str, str, str], ...]   # (type, port, proxy)
    proxy_ips: tuple[str, ...]
    is_proxy: bool
    expire_time_ms: int | None

    @property
    def expired(self) -> bool:
        return bool(self.expire_time_ms) and time.time() * 1000 > (self.expire_time_ms or 0)

    def connect_host(self) -> str:
        """该连哪个地址。代理优先（文档：`isProxy` = 是否**优先**使用代理连接）。"""
        if self.is_proxy and self.proxy_ips:
            return self.proxy_ips[0]
        return self.expose_ips[0] if self.expose_ips else ""

    def connect_port(self, port: str) -> str:
        """给定靶机端口，返回实际该连的端口（代理型要换成映射后的端口）。"""
        if self.is_proxy:
            for mapped_type, mapped_port, proxy_port in self.port_mappings:
                if mapped_port == port and mapped_type.lower() in ("tcp", ""):
                    return proxy_port
        return port

    def describe(self) -> str:
        if self.is_proxy:
            mapping = ", ".join(f"{p}→{x}" for _, p, x in self.port_mappings) or "无映射"
            return f"代理 {self.connect_host()}（{mapping}）"
        ports = ",".join(self.ports) or "无端口"
        return f"直连 {self.connect_host()}（{ports}）"


@dataclass(frozen=True, slots=True)
class Exercise:
    """题目（列表项或详情，字段取并集）。"""

    exercise_id: int
    name: str
    category: str = ""
    description: str = ""
    has_solved: bool = False
    score: str = ""
    difficulty: str = ""
    attachments: tuple[dict[str, Any], ...] = ()
    endpoints: tuple[Endpoint, ...] = ()
    is_need_init: bool = False
    is_need_check: bool = False
    endpoint_type: str = ""
    access_type: str = ""
    """靶机是什么类型 —— `http`（默认）还是 `nc`/`tcp` 这种裸协议。

    ★ 它决定两件事：① 沙箱要不要放开裸网络（`runner.run_worker` 拿它覆盖
    `network_isolated`）；② 提示词里要不要说"别只试 http_request，用 nc 直连"
    （`runner._initial_prompt`）。取不到就当空串 = 按 HTTP 处理，这是安全方向。

    ⚠️ 平台的字段名不止一种写法（`accessType` / `access_type`），
    生产侧是 `app/ctf2/client.py:1195` 的 `_first_str` 在兜；这里同理。
    """
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        """按文档第 4 条：`isNeedCheck=false` **且** endpoints 可用才算就绪。"""
        return (not self.is_need_check) and bool(self.endpoints)


@dataclass(frozen=True, slots=True)
class SubmitResult:
    """一次提交的判定。

    ★ 它是**业务结果**，不是异常 —— 见模块 docstring 第 ② 条。
    """

    ok: bool                 # code == 00000
    is_correct: bool
    code: str
    message: str
    already_submitted: bool = False
    wrong_answer: bool = False


# ══════════════════════════════════════════════════════════
# 客户端
# ══════════════════════════════════════════════════════════


class ArenaClient:
    """比赛平台 Agent API 的异步客户端。

    ⚠️ `access_key` 只出现在 `X-Agent-AccessKey` 头里 —— 不进 URL、不进日志
    （与 `ctf2.client` 同一条纪律）。
    """

    def __init__(
        self,
        *,
        base_url: str,
        access_key: str,
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        if not self._base.endswith(API_PREFIX):
            self._base = self._base + API_PREFIX
        self._key = access_key
        # ★ `trust_env=False` —— 这一处**优先级最低**，但加一行无妨。
        #   理由（B 档）：请求带 `X-Agent-AccessKey` 凭据，而
        #   `trust_env` 默认 `True` 会让请求走环境变量里的 `HTTP_PROXY`，
        #   等于把凭据交给那个代理。
        #   ⚠️ 且 `app/arena` 在**生产路径上没有调用者**（只有
        #   `scripts/arena_e2e.py` 用它）—— 也就是说这段是死代码。
        #   留着它的理由是"哪天接回来时不该带着这个洞"。
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout), trust_env=False
        )

    async def __aenter__(self) -> ArenaClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ── 底层 ────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        # 现拼而不是存一份：存一份意味着这个含明文 AccessKey 的 dict 长驻内存。
        return {"X-Agent-AccessKey": self._key, "Accept": "application/json"}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        retryable: bool = True,
        max_attempts: int = 3,
    ) -> Any:
        """发一次请求并**按信封判定成功**（见 docstring 第 ① 条）。"""
        url = f"{self._base}{path}"
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self._client.request(
                    method, url, json=json_body, params=params, headers=self._headers()
                )
            except httpx.HTTPError as exc:
                if not retryable or attempt >= max_attempts:
                    raise ArenaUnavailable(
                        f"连接比赛平台失败：{exc.__class__.__name__}",
                        detail=redact(url),
                    ) from exc
                await asyncio.sleep(min(2.0 * attempt, 8.0))
                continue

            if response.status_code >= 500 and retryable and attempt < max_attempts:
                await asyncio.sleep(min(2.0 * attempt, 8.0))
                continue

            try:
                body = response.json()
            except ValueError as exc:
                raise ArenaError(
                    f"平台返回了非 JSON 响应（HTTP {response.status_code}）",
                    detail=response.text[:200],
                ) from exc

            return self._unwrap(body, path=path, status=response.status_code)

    @staticmethod
    def _unwrap(body: Any, *, path: str, status: int) -> Any:
        """拆信封：`code == "00000"` 才是成功。"""
        if not isinstance(body, dict) or "code" not in body:
            raise ArenaError(f"响应不是约定的信封形状（HTTP {status}）", detail=str(body)[:200])

        code = str(body.get("code"))
        message = str(body.get("message") or "")
        if code == SUCCESS_CODE:
            return body.get("data")

        # ★ 失败也要带上 code —— 它才是调用方唯一能据以判断的东西
        if code in ("A0230", "A0231") or "AccessKey" in message:
            raise ArenaAuthError(f"平台拒绝了这次调用：{message or 'AccessKey 无效'}", code=code)
        if code in ("A0404",) or "不存在" in message:
            raise ArenaNotFound(message or "资源不存在", code=code)
        raise ArenaError(message or f"平台返回失败（{path}）", code=code)

    # ── 端点（顺序与文档的"推荐调用顺序"一致）──────────

    async def match_info(self) -> dict[str, Any]:
        """`GET /match/notice/match-info` —— 注意事项与规则。"""
        return await self._request("GET", "/match/notice/match-info") or {}

    async def overview(self) -> dict[str, Any]:
        """`GET /answer-panel/overview` —— 得分与排名。"""
        return await self._request("GET", "/answer-panel/overview") or {}

    async def notice_list(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/match/notice/now-list") or []

    async def notice_detail(self, notice_id: int) -> dict[str, Any]:
        return await self._request(
            "GET", "/match/notice/detail", params={"id": notice_id}
        ) or {}

    async def exercise_list(self) -> list[dict[str, Any]]:
        """`GET /ctf/exercise-list` —— 分类树（题目挂在 `corpus` 下）。"""
        return await self._request("GET", "/ctf/exercise-list") or []

    async def flatten_exercises(self) -> list[Exercise]:
        """把分类树拍平成题目列表。

        ⚠️ 保留 `category`（来自父分类）—— 它决定用哪个沙箱镜像，
        丢了它就只能一律用通用镜像。
        """
        out: list[Exercise] = []
        for category in await self.exercise_list():
            if not isinstance(category, dict):
                continue
            name = str(category.get("name") or "")
            for item in category.get("corpus") or []:
                if not isinstance(item, dict) or item.get("id") is None:
                    continue
                out.append(
                    Exercise(
                        exercise_id=int(item["id"]),
                        name=str(item.get("name") or f"#{item.get('id')}"),
                        category=name,
                        has_solved=bool(item.get("hasSolved")),
                        raw=item,
                    )
                )
        return out

    async def exercise(self, exercise_id: int) -> Exercise:
        """`GET /ctf/exercise?exerciseId=` —— 题目详情（含附件与靶机）。"""
        data = await self._request(
            "GET", "/ctf/exercise", params={"exerciseId": exercise_id}
        )
        return self._to_exercise(data or {}, exercise_id=exercise_id)

    @staticmethod
    def _to_exercise(data: dict[str, Any], *, exercise_id: int) -> Exercise:
        attachments_raw = data.get("attachment")
        files: list[dict[str, Any]] = []
        if isinstance(attachments_raw, dict):
            files = [f for f in (attachments_raw.get("files") or []) if isinstance(f, dict)]
        elif isinstance(attachments_raw, list):
            files = [f for f in attachments_raw if isinstance(f, dict)]

        endpoints = tuple(
            Endpoint(
                expose_ips=tuple(str(x) for x in (item.get("exposeIps") or [])),
                ports=tuple(str(x) for x in (item.get("ports") or [])),
                users=tuple(
                    (str(u.get("username") or ""), str(u.get("password") or ""))
                    for u in (item.get("users") or [])
                    if isinstance(u, dict)
                ),
                port_mappings=tuple(
                    (
                        str(m.get("type") or ""),
                        str(m.get("port") or ""),
                        str(m.get("proxy") or ""),
                    )
                    for m in (item.get("portMappings") or [])
                    if isinstance(m, dict)
                ),
                proxy_ips=tuple(str(x) for x in (item.get("proxyIps") or [])),
                is_proxy=bool(item.get("isProxy")),
                expire_time_ms=item.get("expireTime"),
            )
            for item in (data.get("endpoints") or [])
            if isinstance(item, dict)
        )

        return Exercise(
            exercise_id=int(data.get("id") or exercise_id),
            name=str(data.get("name") or f"#{exercise_id}"),
            description=str(data.get("description") or ""),
            has_solved=bool(data.get("hasSolved")),
            score=str(data.get("score") or ""),
            difficulty=str(data.get("difficulty") or ""),
            attachments=tuple(files),
            endpoints=endpoints,
            is_need_init=bool(data.get("isNeedInit")),
            is_need_check=bool(data.get("isNeedCheck")),
            endpoint_type=str(data.get("endpointType") or ""),
            access_type=str(data.get("accessType") or data.get("access_type") or ""),
            raw=data,
        )

    async def build_environment(self, exercise_id: int) -> None:
        """`POST /ctf/build-exercise-env` —— **只是受理**，之后要轮询详情。"""
        await self._request(
            "POST", "/ctf/build-exercise-env", json_body={"exerciseId": exercise_id}
        )

    async def recover_environment(self, exercise_id: int) -> None:
        """`POST /ctf/recover-exercise-env` —— 回收环境。"""
        await self._request(
            "POST", "/ctf/recover-exercise-env", json_body={"exerciseId": exercise_id}
        )

    async def wait_until_ready(
        self,
        exercise_id: int,
        *,
        timeout_seconds: float = 300.0,
        interval_seconds: float = 5.0,
        max_polls: int = 200,
    ) -> Exercise:
        """轮询详情直到环境就绪（文档第 4 条）。

        就绪判据是 `isNeedCheck == false` **且** `endpoints` 非空 ——
        只看前者会在"环境刚受理、endpoints 还没填"时误判就绪。
        """
        started = time.monotonic()
        polls = 0
        last: Exercise | None = None
        while polls < max_polls:
            polls += 1
            last = await self.exercise(exercise_id)
            if last.ready:
                logger.info(
                    "比赛平台环境就绪：exercise=%s 轮询 %d 次，用时 %.1fs",
                    exercise_id,
                    polls,
                    time.monotonic() - started,
                )
                return last
            if time.monotonic() - started > timeout_seconds:
                break
            await asyncio.sleep(interval_seconds)

        elapsed = time.monotonic() - started
        raise ArenaEnvironmentTimeout(
            f"等了 {elapsed:.0f} 秒，题目 {exercise_id} 的环境仍未就绪"
            f"（isNeedCheck={getattr(last, 'is_need_check', '?')}，"
            f"endpoints={len(getattr(last, 'endpoints', ()))} 条）",
            polls=polls,
            elapsed=elapsed,
        )

    async def submit(self, exercise_id: int, flag: str) -> SubmitResult:
        """`POST /answer-panel/answer` —— 提交一个 flag。

        ★ **返回结果对象而不是抛异常**：答案错误是业务结果（要换下一个候选），
        与"凭据失效""服务端故障"是两回事。见模块 docstring 第 ② 条。
        """
        try:
            data = await self._request(
                "POST",
                "/answer-panel/answer",
                json_body={"exerciseId": exercise_id, "flag": flag},
                retryable=False,   # ★ 提交**不重试**：重试等于重复提交
            )
        except ArenaAuthError as exc:
            return SubmitResult(ok=False, is_correct=False, code=exc.code or "", message=exc.message)
        except ArenaError as exc:
            code = exc.code or ""
            return SubmitResult(
                ok=False,
                is_correct=False,
                code=code,
                message=exc.message,
                already_submitted=code == CODE_ALREADY_SUBMITTED,
                wrong_answer=code == CODE_ANSWER_WRONG,
            )

        is_correct = bool((data or {}).get("isCorrect"))
        return SubmitResult(ok=True, is_correct=is_correct, code=SUCCESS_CODE, message="")


__all__ = [
    "API_PREFIX",
    "CODE_ALREADY_SUBMITTED",
    "CODE_ANSWER_WRONG",
    "SUCCESS_CODE",
    "ArenaAuthError",
    "ArenaClient",
    "ArenaEnvironmentTimeout",
    "ArenaError",
    "ArenaNotFound",
    "ArenaUnavailable",
    "Endpoint",
    "Exercise",
    "SubmitResult",
]
