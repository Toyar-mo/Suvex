"""比赛平台驱动：`slab-match/api/v1/agent`。

文档：工作区根的 `api_doc.md`（赛事方给的 AI Agent 接口文档）。

## 这个平台与 CTF2 的四处**结构性**差别

写这个驱动时踩到的每一处都不是"路径不同"，而是形状不同：

| | CTF2 | 这里 |
| --- | --- | --- |
| 成功判据 | HTTP 状态码 | **HTTP 200 + `code == "00000"`** |
| 题目寻址 | `(practice_id, challenge_id, sub_flag_id)` | 单个 `exerciseId` |
| 环境状态 | 独立的 status 端点 | **藏在题目详情里**（`isNeedCheck` / `endpoints`） |
| 环境信息 | 几个 URL | IP + 端口 + **账号密码** + 代理端口映射 |

第三条尤其容易做错：这个平台**没有**"查询环境状态"的端点，
`GET /ctf/exercise` 既是题目详情也是环境状态。所以 `read_environment`
的实现就是再取一次详情 —— 硬套 CTF2 的"状态端点"形状会写出一个
永远查不到东西的函数。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.kernel.adapters.spec import NOT_ACCEPTED_CODES, AuthSpec, EnvelopeSpec
from app.kernel.adapters.transport import JsonPlatformTransport
from app.kernel.client import (
    KernelRateLimited,
    KernelResponseError,
    KernelUnavailable,
)
from app.kernel.platform import (
    RemoteAttachment,
    RemoteChallenge,
    RemoteEndpoint,
    RemoteEnvironment,
    SubmitOutcome,
    parse_millis,
    render_endpoints_prompt,
)

logger = logging.getLogger("suvex.ctf2")

#: 平台的成功码。**判成功只看它** —— 这个平台即使失败也返回 HTTP 200。
SUCCESS_CODE = "00000"

#: agent 接口的固定路径前缀。
DEFAULT_PATH = "/slab-match/api/v1/agent"

# `NOT_ACCEPTED_CODES`（"平台没受理"的错误码）现在住在 `adapters/spec.py`
# —— 手写驱动与配置驱动必须用**同一份**判定，而两份拷贝的漂移会让某个
# 平台拿同一个错答案无限重试。判定逻辑与"为什么默认按判错"的解释也在那里。


def normalize_base_url(raw: str) -> str:
    """把用户填的地址补成完整的 agent 根。

    ★ 赛事方给的可能是 `https://host`，也可能是完整的
    `https://host/slab-match/api/v1/agent`（文档写的是后者）—— 而人在
    设置页里更可能只填域名。两种都要能用。

    不补的话，症状是一条 404，而 404 在设置页里看起来**像 key 不对**，
    排查方向会整个错掉。
    """
    url = (raw or "").strip().rstrip("/")
    if not url:
        return ""
    if url.endswith(DEFAULT_PATH):
        return url
    return url + DEFAULT_PATH


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


class SlabMatchDriver:
    """比赛平台的驱动（同时是它的 HTTP 客户端）。

    ⚠️ 与 `KernelClient` 不同，这里**不拆分** client / driver 两层：这个
    平台没有 CTF2 那种"逐个容器试 id"的畸形路径，一个类就够了。
    多拆一层只会多一个转发方法。
    """

    slug = "slabmatch"
    display_name = "比赛平台（slab-match）"

    def __init__(
        self,
        base_url: str,
        access_key: str,
        *,
        timeout: float = 15.0,
        min_interval: float = 0.2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base = normalize_base_url(base_url)
        # ★ 传输层（认证 + 统一响应包装 + 限速 + 429/401 映射）与
        #   `GenericDriver` **共用**，见 `adapters/transport.py` 的说明：
        #   复制两份的后果是将来只修一份。
        #
        # ⚠️ 下面两个常量是**这个平台的事实**（文档的「认证 Header」与
        #    「统一响应」两节）：成功只看 `code == "00000"`，**即使
        #    HTTP 状态码是 200**。
        self._transport = JsonPlatformTransport(
            self._base,
            access_key,
            auth=AuthSpec(style="header", name="X-Agent-AccessKey"),
            envelope=EnvelopeSpec(
                success_path="code",
                success_value=SUCCESS_CODE,
                data_path="data",
                message_path="message",
            ),
            timeout=timeout,
            min_interval=min_interval,
            transport=transport,
        )

    # ══════════════════════════════════════════════════════
    # 生命周期
    # ══════════════════════════════════════════════════════

    async def __aenter__(self) -> SlabMatchDriver:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._transport.aclose()

    # ══════════════════════════════════════════════════════
    # 传输
    # ══════════════════════════════════════════════════════

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        """一次请求，返回拆包后的 `data`。

        ## ★ 成功判据是 `code`，不是 HTTP 状态码

        这是这个平台与 CTF2 最根本的一处不同：它的**统一响应**意味着
        失败也可能是 HTTP 200。只看状态码的话，"答案错误"会被当成成功，
        然后 `data` 是 `{}` —— 而空字典会被上层读成"没有 isCorrect 字段"，
        最终表现成一道题**反复提交、永远不结束**。
        """
        # 认证、统一响应包装、限速、429/401 的映射全部在传输层里
        # （与 `GenericDriver` 共用）。这里只做转发。
        return await self._transport.request(method, path, params=params, body=body)

    # ══════════════════════════════════════════════════════
    # 只读接口
    # ══════════════════════════════════════════════════════

    async def match_info(self) -> dict[str, Any]:
        """竞赛注意事项与规则（文档 §1）。"""
        data = await self._request("GET", "/match/notice/match-info")
        return data if isinstance(data, dict) else {}

    async def overview(self) -> dict[str, Any]:
        """当前得分与排名（文档 §2）。

        ★ 比赛里这张表有实际用处：`stageRank` 是"我们现在第几名"，
        而"前三名额外加分"是这场比赛的计分规则 —— 排名直接决定
        要不要更激进地抢题。
        """
        data = await self._request("GET", "/answer-panel/overview")
        return data if isinstance(data, dict) else {}

    async def notice_list(self) -> list[dict[str, Any]]:
        """公告列表（文档 §8）。赛事方常常用公告下发临时规则/提示。"""
        data = await self._request("GET", "/match/notice/now-list")
        return [item for item in (data or []) if isinstance(item, dict)]

    async def notice_detail(self, notice_id: int) -> dict[str, Any]:
        """公告详情（文档 §9）。"""
        data = await self._request("GET", "/match/notice/detail", params={"id": notice_id})
        return data if isinstance(data, dict) else {}

    # ══════════════════════════════════════════════════════
    # 题目
    # ══════════════════════════════════════════════════════

    async def fetch_challenges(self, *, known_ids: set[str]) -> list[RemoteChallenge]:
        """列出全部题目（文档 §4）。

        ## 列表只有五个字段，所以详情必须逐题取

        文档里 `exercise-list` 每项只有 `id` / `name` / `order` /
        `isOpen` / `hasSolved` —— **没有题面、没有分值、没有附件**。
        那些都在 §5 的单题详情里。

        所以这里的策略与 CTF2 相反：
        * CTF2 —— 列表端点**不存在**，靠侧门拿 id，再逐题试详情；
        * 这里 —— 列表端点**存在且便宜**，用 `known_ids` 决定"哪些题
          已经取过详情、不必再取"。

        ⚠️ 已知的题**仍然会被返回**（带上 hasSolved/isOpen），因为那
        两个字段可能在两次同步之间变化 —— 它们是"这道题该不该做"的
        判据。跳过的是**逐题详情**（每个一道请求），不是列表本身。
        """
        data = await self._request("GET", "/ctf/exercise-list")
        groups = data if isinstance(data, list) else []
        out: list[RemoteChallenge] = []
        for group in groups:
            if not isinstance(group, dict):
                continue
            category = str(group.get("name") or "")
            group_id = str(group.get("id") or "")
            for item in group.get("corpus") or []:
                if not isinstance(item, dict):
                    continue
                exercise_id = _as_int(item.get("id"))
                if exercise_id is None:
                    continue
                out.append(
                    RemoteChallenge(
                        remote_id=str(exercise_id),
                        title=str(item.get("name") or f"#{exercise_id}"),
                        category=category,
                        group_id=group_id,
                        group_name=category,
                        order=_as_int(item.get("order")),
                        solved=bool(item.get("hasSolved")),
                        is_open=bool(item.get("isOpen", True)),
                        # ★ handle 只放这一个整数。上层**不认识它**，
                        #   只在提交/起环境时原样传回来。
                        handle={"exerciseId": exercise_id},
                    )
                )
        logger.info(
            "比赛平台题目列表：%d 组 / %d 道（其中本地已知 %d 道）",
            len(groups),
            len(out),
            len({c.remote_id for c in out} & known_ids),
        )
        return out

    async def fetch_challenge(self, handle: dict[str, Any]) -> RemoteChallenge | None:
        """单题详情（文档 §5）。题面、分值、附件、环境都在这里。"""
        exercise_id = _as_int(handle.get("exerciseId"))
        if exercise_id is None:
            return None
        data = await self._request("GET", "/ctf/exercise", params={"exerciseId": exercise_id})
        if not isinstance(data, dict):
            return None
        return self._to_challenge(data, fallback_id=exercise_id)

    @staticmethod
    def _to_challenge(payload: dict[str, Any], *, fallback_id: int) -> RemoteChallenge:
        exercise_id = _as_int(payload.get("id")) or fallback_id
        files = ((payload.get("attachment") or {}).get("files")) or []
        attachments = [
            RemoteAttachment(
                name=str(item.get("name") or ""),
                url=str(item.get("url") or ""),
                ext=str(item.get("ext") or ""),
            )
            for item in files
            if isinstance(item, dict) and item.get("url")
        ]
        return RemoteChallenge(
            remote_id=str(exercise_id),
            title=str(payload.get("name") or f"#{exercise_id}"),
            description=str(payload.get("description") or ""),
            # ⚠️ 分值在文档里是**字符串**（`"score": "100"`）。直接塞进
            #    整数列会炸，而"某道题分值读不出来"这种错很难被发现。
            score=_as_int(payload.get("score")),
            attachments=attachments,
            solved=bool(payload.get("hasSolved")),
            difficulty=str(payload.get("difficulty") or ""),
            handle={"exerciseId": exercise_id},
        )

    # ══════════════════════════════════════════════════════
    # 提交
    # ══════════════════════════════════════════════════════

    async def submit(self, handle: dict[str, Any], answer: str) -> SubmitOutcome:
        """提交答案（文档 §3）。

        ⚠️ 判错的判定见 `NOT_ACCEPTED_CODES` —— 那是**当前最大的未知**，
        拿到真机后要用第一次判错时看到的 code 去修正它。
        """
        exercise_id = _as_int(handle.get("exerciseId"))
        if exercise_id is None:
            raise KernelResponseError("提交时 handle 里没有 exerciseId —— 寻址信息丢了。")
        try:
            data = await self._request(
                "POST",
                "/answer-panel/answer",
                body={"exerciseId": exercise_id, "flag": answer},
            )
        except KernelResponseError as exc:
            if exc.code and exc.code in NOT_ACCEPTED_CODES:
                # 平台没受理：**不计错误次数**（那是不可再生资源）。
                return SubmitOutcome(accepted=False, is_solved=False, message=str(exc))
            # 平台收下了并判错。把原文写进日志 —— 那正是我们用来修正
            # `NOT_ACCEPTED_CODES` 的证据。
            logger.info(
                "比赛平台判错（exerciseId=%s code=%s）：%s", exercise_id, exc.code, exc.message
            )
            return SubmitOutcome(accepted=True, is_solved=False, message=str(exc))

        payload = data if isinstance(data, dict) else {}
        correct = bool(payload.get("isCorrect"))
        return SubmitOutcome(
            accepted=True,
            is_solved=correct,
            message="答案正确。" if correct else "平台判定为错误答案。",
        )

    # ══════════════════════════════════════════════════════
    # 环境
    # ══════════════════════════════════════════════════════

    async def read_environment(self, handle: dict[str, Any]) -> RemoteEnvironment:
        """读环境状态。

        ★ 这个平台**没有**独立的环境状态端点 —— `GET /ctf/exercise`
        既是题目详情也是环境状态（`isNeedCheck` / `isNeedInit` /
        `endpoints`）。所以这里就是再取一次详情。

        硬套 CTF2 那种"状态端点"的形状，会写出一个永远查不到东西的
        函数，而症状是"环境一直显示未就绪"。
        """
        exercise_id = _as_int(handle.get("exerciseId"))
        if exercise_id is None:
            return RemoteEnvironment()
        data = await self._request("GET", "/ctf/exercise", params={"exerciseId": exercise_id})
        if not isinstance(data, dict):
            return RemoteEnvironment()
        return self._to_environment(data)

    @staticmethod
    def _to_environment(payload: dict[str, Any]) -> RemoteEnvironment:
        endpoints: list[RemoteEndpoint] = []
        for item in payload.get("endpoints") or []:
            if not isinstance(item, dict):
                continue
            users: list[tuple[str, str]] = []
            for user in item.get("users") or []:
                if isinstance(user, dict):
                    users.append(
                        (str(user.get("username") or ""), str(user.get("password") or ""))
                    )
            endpoints.append(
                RemoteEndpoint(
                    expose_ips=[str(x) for x in item.get("exposeIps") or []],
                    ports=[str(x) for x in item.get("ports") or []],
                    users=users,
                    is_proxy=bool(item.get("isProxy")),
                    proxy_ips=[str(x) for x in item.get("proxyIps") or []],
                    port_mappings=[
                        x for x in item.get("portMappings") or [] if isinstance(x, dict)
                    ],
                    expire_time=parse_millis(item.get("expireTime")),
                )
            )
        # ★ "就绪"的判据是**两个条件同时成立**：平台不再说"准备中"
        #   （`isNeedCheck == False`）**且**真的给了 endpoints。
        #
        #   只看 `isNeedCheck` 会踩到一个真实的坑：文档说"启动是异步的，
        #   成功后继续轮询直到 isNeedCheck=false 且 endpoints 可用" ——
        #   两份条件缺一不可。只认前者的话，模型会拿到一份**空的**
        #   连接信息，然后卡在"没有靶机地址"上。
        return RemoteEnvironment(
            endpoints=endpoints,
            ready=not bool(payload.get("isNeedCheck")) and bool(endpoints),
            needs_init=bool(payload.get("isNeedInit")),
            can_refresh=bool(payload.get("canRefreshEndpoint")),
        )

    async def start_environment(self, handle: dict[str, Any]) -> None:
        """启动环境（文档 §6）。**幂等**：已经在跑的题再调一次是安全的。

        ⚠️ 不吞异常：启动失败要能被上层看见并重试。这与 `stop_environment`
        正好相反。
        """
        exercise_id = _as_int(handle.get("exerciseId"))
        if exercise_id is None:
            raise KernelResponseError("启动环境时 handle 里没有 exerciseId。")
        await self._request(
            "POST", "/ctf/build-exercise-env", body={"exerciseId": exercise_id}
        )

    async def stop_environment(self, handle: dict[str, Any]) -> None:
        """回收环境（文档 §7）。

        ★ 容错，**不抛**：回收一个本来就没起、或已经过期的环境会报错，
        而那是完全正常的情况（我们的目的是"别在平台侧留资源"，不是为了
        拿到一个成功的回执）。抛出去会让"停止解题"这个动作失败 ——
        而它本该总是成功。
        """
        exercise_id = _as_int(handle.get("exerciseId"))
        if exercise_id is None:
            return
        try:
            await self._request(
                "POST", "/ctf/recover-exercise-env", body={"exerciseId": exercise_id}
            )
        except (KernelResponseError, KernelRateLimited, KernelUnavailable) as exc:
            logger.info("回收环境时平台返回错误（通常无害）：%s", exc)

    async def environment_prompt(self, env: RemoteEnvironment) -> str:
        """渲染靶机信息。

        ★ 实现与 `GenericDriver`（配置驱动）**共用一份**
        （`platform.render_endpoints_prompt`）—— 那里解释了为什么 IP、
        端口、账号密码、代理映射一个都不能少。
        """
        return render_endpoints_prompt(env)

    # ══════════════════════════════════════════════════════
    # 平台元信息
    # ══════════════════════════════════════════════════════

    @property
    def answer_max_length(self) -> int:
        """文档 §3 写明：`flag` 最长 **256** 字符。

        ⚠️ 这个数字比 CTF2 的 1000 小得多，而它是**每一位参赛者都会
        撞到**的差别吗？不是 —— 但它会在"拿到一段长输出需要提交"时
        撞到（比如融合靶场要求提交一整条命令行）。前端要按它校验，
        而不是继续用写死的 1000。
        """
        return 256

    @property
    def supports_environment(self) -> bool:
        """这个平台有靶机环境（`build-exercise-env` / `endpoints`）。"""
        return True


__all__ = [
    "DEFAULT_PATH",
    "NOT_ACCEPTED_CODES",
    "SUCCESS_CODE",
    "SlabMatchDriver",
    "normalize_base_url",
]
