"""平台契约：**把"平台之间的差异"关进一个接口**。

## 为什么需要这一层

`app/kernel/` 里 80% 的代码（解题引擎、沙箱、调度、配额、抢首杀、审计、
活动流、UI）**根本不关心题目来自哪个平台** —— 它们只认"题面 + 附件 +
一个答案"。真正绑死平台的只有两件事：

1. **端点路径与响应形状**（`client.py`）
2. **题目寻址模型**（`service._remote_ids` 的三元组）

而目标平台会换：CTF2 只是把功能跑通的试验场，比赛用的是另一套 API
（`api_doc.md`，`slab-match/api/v1/agent`）。这两套的差异不是"路径不同"
那么轻 —— 它们是**两种形状**：

| 维度 | CTF2 | 比赛平台 |
| --- | --- | --- |
| 认证 | `Authorization: Bearer <PAT>` | `X-Agent-AccessKey` |
| 响应 | 裸 JSON | `{code, message, data}`，`code == "00000"` 才算成功 |
| 题目寻址 | 三元组 `(practice_id, challenge_id, sub_flag_id)` | 单个 `exerciseId` |
| 题目列表 | **不存在**（靠 writeups 侧门 + 逐个容器试详情） | `GET /ctf/exercise-list` 直接给 |
| 环境 | start/stop + 轮询状态端点 | build/recover + 详情里 `isNeedCheck` |
| 环境信息 | 几个 URL | IP + 端口 + **账号密码** + 代理端口映射 |
| 提交 | `{flag, sub_flag_id}` → `{accepted, is_solved}` | `{exerciseId, flag}` → `{isCorrect}` |
| 答案长度上限 | 1000 | **256** |

所以这一层的目标是：**上层的每一个调用点都不再知道"平台是谁"**。
加了新平台，只写一个 driver。

## ★ 上层绝对不能看见的东西

* **不要**让上层拼端点、拼 header、拆 `code`/`data` 包装 —— 那是 driver 的活。
* **不要**让上层知道 `handle` 里有什么。它是**不透明的**：CTF2 的 handle
  是三元组，比赛平台的是一个 `exerciseId`。上层只把它从"取题"原样传到
  "提交/起环境"。
* **不要**在平台差异上做"最小公倍数"式的妥协（比如"统一用 URL 表示环境"）
  —— 那会把两边都拧变形。差异大的地方给**可选的表达能力**（见
  `RemoteEndpoint`），让每个 driver 用自己的方式填。

## 为什么 `handle` 是 dict 而不是泛型/联合类型

它要跨进程边界存进数据库（`Ctf2Challenge` 已经有一列存远端 id）。
`dict[str, Any]` 是唯一**不需要为每个平台改数据库**的形状。类型安全的
代价在这里不值得付：真正的安全来自"上层从不解引用它"这条纪律。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

# ⚠️ 复用 CTF2 那套错误类型（`Ctf2Error` 家族）而不是再造一组。
#
# 理由：`service._translate` 已经把 `Ctf2RateLimited` / `Ctf2ResponseError`
# 映射成了正确的 HTTP 状态码，而那套映射**与平台无关**（429 就是 429）。
# 再造一组只会让 translate 里多一个分支，而分支正是漏掉的地方。
#
# 名字里的 Ctf2 是历史包袱 —— 读到这里请把它当成"平台"。


@dataclass(frozen=True, slots=True)
class RemoteAttachment:
    """一个附件。字段名照抄比赛平台文档（`name` / `url` / `ext`）。"""

    name: str
    url: str
    ext: str = ""


@dataclass(frozen=True, slots=True)
class RemoteChallenge:
    """一道题的**平台侧快照**。

    这是 driver 交给上层的唯一题目形状。上层拿它去 upsert 本地
    `Ctf2Challenge`，两边字段一一对应。
    """

    remote_id: str
    title: str
    description: str = ""
    category: str = ""
    score: int | None = None
    attachments: list[RemoteAttachment] = field(default_factory=list)
    # ★ 平台**自己说**这道题解没解出。
    #
    # 它是 `hasSolved`（比赛平台有）或本地推断（CTF2 没有这个字段）。
    # 为什么重要：比赛时可能有人在**网页上手工交过**某道题，或者
    # 我们的两个实例同时在跑 —— 平台侧的这个字段是唯一的事实来源。
    solved: bool = False
    # 比赛平台有 `difficulty`（EASY/…），CTF2 没有 → 空串。
    difficulty: str = ""
    # 所属分组（比赛平台的分类名 "Web"、CTF2 的练习集/赛事）。
    group_id: str = ""
    group_name: str = ""
    # 平台给的展示顺序（比赛平台的 `order`）。None = 平台没给。
    order: int | None = None
    # ★ 这道题**是否已经开放**（比赛平台的 `isOpen`）。
    #
    # 为什么它必须进契约：比赛常常**分批放题**，而"抢前三名加分"意味着
    # 一道还没开放的题如果被排进解题队列，它会占着并发名额、烧模型额度，
    # 最后必然失败 —— 而它本该在开放的那一刻才开始跑。默认 True 是给
    # "没有这个概念的平台"（CTF2）用的。
    is_open: bool = True
    # ★ 不透明寻址。**上层绝不解引用它** —— 见模块 docstring。
    handle: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SubmitOutcome:
    """一次提交的结果。

    ⚠️ `accepted` 与 `is_solved` 必须分开：
    * `accepted=False` —— 请求本身没被受理（限流、格式错、额度用完），
      **不计入错误次数**，也不该把题目判成 failed；
    * `accepted=True, is_solved=False` —— 平台**收下了**这个答案并判错，
      那是真的错了一次。
    把两者合成一个布尔会让"平台 429"看起来像"答案错了"，而后者会
    消耗掉一次不可再生的错误提交次数。
    """

    accepted: bool
    is_solved: bool
    message: str = ""
    already_solved: bool = False


@dataclass(frozen=True, slots=True)
class RemoteEndpoint:
    """一台靶机的连接信息（比赛平台的 `endpoints[]`）。

    ## 为什么字段这么"具体"（IP/端口/账号/代理映射）

    我一度想把它归一成"几个 URL"，让上层好处理。**那是错的**：比赛平台
    给的是一台机器的**连接四要素**（IP、端口、账号、密码），有时还要求
    走代理端口映射。把它压成 URL 会丢掉账号密码 —— 而模型拿不到密码就
    连不上靶机，那等于整个环境白起。

    所以差异大的地方**保留它的形状**，由 `environment_prompt()` 决定
    怎么讲给模型听。
    """

    expose_ips: list[str] = field(default_factory=list)
    ports: list[str] = field(default_factory=list)
    users: list[tuple[str, str]] = field(default_factory=list)
    is_proxy: bool = False
    proxy_ips: list[str] = field(default_factory=list)
    port_mappings: list[dict[str, Any]] = field(default_factory=list)
    expire_time: datetime | None = None


@dataclass(frozen=True, slots=True)
class RemoteEnvironment:
    """一道题的环境状态。"""

    endpoints: list[RemoteEndpoint] = field(default_factory=list)
    # ★ 环境是否**已经可用**。比赛平台用 `isNeedCheck == False 且 endpoints 非空`
    #   表达；CTF2 用它自己的 status 端点。上层只问这一个布尔值。
    ready: bool = False
    # 平台说"需要先启动环境"（`isNeedInit`）。
    needs_init: bool = False
    # 平台是否允许刷新（`canRefreshEndpoint`）。
    can_refresh: bool = False
    # CTF2 那种"直接给 URL"的平台填这里；比赛平台留空。
    access_urls: list[str] = field(default_factory=list)


def parse_millis(value: Any) -> datetime | None:
    """毫秒时间戳 → datetime。两个平台的 `expireTime` 都是毫秒。

    返回带时区的 UTC 时间；不是正数、不是数字、或者超出范围都返回
    `None` —— "靶机没有过期时间"是一个正常状态，不该让整次解析失败。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text.lstrip("-").isdigit():
            return None
        value = int(text)
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(float(value) / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def render_endpoints_prompt(env: RemoteEnvironment) -> str:
    """把靶机连接信息讲给模型听。**所有驱动共用这一份。**

    ★ 这里的每一行都是模型**必须**看到的：IP 让它知道去哪、端口让它
    知道敲哪个门、**账号密码**让它能登录、代理映射让它在直连不通时有
    第二条路。少任何一项，模型都会卡在"连不上"，而它会以为是自己
    命令写错了 —— 那是最费时间的一种卡法。

    没有环境（纯附件题）时返回**空串**，而不是一段"靶机：（无）"——
    提示词里出现一个空的环境段落，会让模型以为自己漏看了什么。
    """
    if not env.endpoints:
        if not env.access_urls:
            return ""
        return "靶机地址：\n" + "\n".join(f"- {url}" for url in env.access_urls)

    lines = ["靶机连接信息（每道题的靶机相互独立）："]
    for index, endpoint in enumerate(env.endpoints, start=1):
        prefix = f"靶机 {index}" if len(env.endpoints) > 1 else "靶机"
        lines.append(f"{prefix}：")
        if endpoint.expose_ips:
            lines.append(f"  IP：{'、'.join(endpoint.expose_ips)}")
        if endpoint.ports:
            lines.append(f"  开放端口：{'、'.join(endpoint.ports)}")
        for username, password in endpoint.users:
            lines.append(f"  登录账号：{username} / {password}")
        if endpoint.is_proxy and endpoint.proxy_ips:
            lines.append(f"  ⚠️ 需经代理连接，代理 IP：{'、'.join(endpoint.proxy_ips)}")
        for mapping in endpoint.port_mappings:
            lines.append(
                f"  端口映射：{mapping.get('port')}/{mapping.get('type')}"
                f" → 代理端口 {mapping.get('proxy')}"
            )
        if endpoint.expire_time:
            lines.append(f"  过期时间：{endpoint.expire_time.isoformat()}")
    return "\n".join(lines)


@runtime_checkable
class PlatformDriver(Protocol):
    """一个平台的驱动。

    ## 哪些是必须的

    `fetch_challenges` 与 `submit` —— 没有它们，"解题"这件事不成立。
    其余的都有默认的"不支持"语义（见 `capabilities`）。

    ## 同步与异步

    全部是**协程**。因为两个平台都只有异步 HTTP 客户端，而把它们包成
    同步会强制在协程里开线程（或者相反），那正是 `service` 模块
    docstring 里反复警告的那条边界。
    """

    #: 稳定标识，存进数据库（`Ctf2Account.platform`）。**不要改它的值。**
    slug: str
    #: 给人看的名字（设置页里的选项文案）。
    display_name: str

    # ── 必须实现 ────────────────────────────────────────

    async def fetch_challenges(self, *, known_ids: set[str]) -> list[RemoteChallenge]:
        """列出全部题目。

        `known_ids` 是**本地已经有**的 remote_id 集合，用来做增量：
        CTF2 那条路一次全量要 98+ 个请求、45 秒，而比赛平台一次列表
        请求就够 —— **两者都必须尊重**"已知的可以跳过详情"这个优化，
        只是实现方式不同（CTF2 干脆不请求，比赛平台仍然要列表、
        但可以跳过逐题详情）。
        """
        ...

    async def submit(self, handle: dict[str, Any], answer: str) -> SubmitOutcome:
        """提交一个答案。"""
        ...

    # ── 有默认"不支持"语义的 ────────────────────────────

    async def fetch_challenge(self, handle: dict[str, Any]) -> RemoteChallenge | None:
        """取单题详情。不支持就返回 `None`。

        CTF2 需要它（列表端点不存在，"拉取单题"是唯一的补题入口）；
        比赛平台的列表里信息不全，也需要它。
        """
        return None

    async def read_environment(self, handle: dict[str, Any]) -> RemoteEnvironment:
        """读当前环境状态。**不支持环境的平台返回 `RemoteEnvironment()`。**"""
        return RemoteEnvironment()

    async def start_environment(self, handle: dict[str, Any]) -> None:
        """启动环境。不支持就什么都不做（而不是抛异常）。

        ★ 为什么不抛：一道**静态附件题**在比赛平台上也会走到"起环境"
        这一步（因为流程是统一的），而它没有环境可言。抛异常会让这类题
        全部失败 —— 而它们本该是最容易拿分的那一批。
        """
        return None

    async def stop_environment(self, handle: dict[str, Any]) -> None:
        """回收环境。同样，不支持就什么都不做。"""
        return None

    async def environment_prompt(self, env: RemoteEnvironment) -> str:
        """把环境信息讲给模型听（会拼进解题提示词）。

        ★ 为什么让 driver 自己渲染，而不是上层统一格式化：
        两个平台给的东西**根本不是一类**（CTF2 是 URL，比赛平台是
        IP + 端口 + 账号密码 + 代理映射）。统一格式化必然丢信息，
        而丢掉的恰恰是"怎么连上去"。

        默认实现处理 `access_urls`；比赛平台会覆盖它来输出四要素。
        """
        if not env.access_urls:
            return ""
        return "靶机地址：\n" + "\n".join(f"- {url}" for url in env.access_urls)

    # ── 平台元信息（默认值适用于"标准 CTF 平台"）───────

    @property
    def answer_max_length(self) -> int:
        """平台允许的答案最大长度。

        ⚠️ 这是一个**真实存在的平台差异**：CTF2 收 1000，比赛平台文档
        写明 **256**。做成全局常量会让长答案在比赛平台上被静默拒绝，
        而症状是"这道题明明解对了却交不上去"。

        默认 256 —— **取两者中更严的那个**。理由是默认值应该站在
        "错不起"的一侧：默认宽了会让答案在提交那一刻才失败，默认严了
        只是在本地就被截断提示，用户马上能看到。
        """
        return 256

    @property
    def supports_environment(self) -> bool:
        """这个平台有没有"靶机环境"这个概念。附件题平台返回 False。"""
        return False
