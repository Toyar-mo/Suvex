"""通知投递 —— 把「什么情况下发」与「怎么发」真的接上。

## 这个模块存在的理由

`NotifyChannel` / `NoticeRule` 两张表从 v0.22 起就只做了 CRUD：能存、能读、
界面能配，但**没有任何代码在扫描结束时去发一条通知**。`POST
/settings/notices/channels/{id}/test` 当时也诚实地说"真实发送尚未接入"。

于是用户配好邮箱、配好阈值、看到"保存成功"，然后什么都不会发生 ——
而且是**静默地**不发生。这类缺陷比崩溃更坏：崩溃会被发现，静默失效会
被当成"可能没配好吧"。

本模块补的就是这一段：**扫描结束 → 按规则筛选 → 按渠道发送 → 记录结果**。

## ★ 为什么发送**不在**扫描收尾的事务里

`scans.service.finalize_scan` 的 docstring 记着一件事：那一段跑在一个
**已经开着的事务**里，读一次文件系统就撞过 `DeadlockDetected`。而发送
通知要发 HTTP 请求，超时按秒算、对端慢的时候更久。

所以边界这样划：

    finalize_scan（持事务、纯本地）
        └─ 落库 + 发一个"该发通知了"的意图
    调度器 / 后台任务（无事务）
        └─ 建自己的 session → 读配置 → 真发 → 写回结果

**发送失败绝不能让扫描显示为失败**：扫描已经跑完了，它的结果是有效的。
通知只是"告诉人一声"，那是另一件事，它的失败要单独记账。

## ★ 三个渠道各自要什么

| kind    | target 形态              | 协议                                  |
| ------- | ------------------------ | ------------------------------------- |
| email   | `a@b.c` 或 `a@b.c,d@e.f` | 本轮**不发**（见下）                  |
| webhook | `https://...`            | `POST` JSON，body 是扫见的结构         |
| slack   | `https://hooks.slack.com/...` | `POST` JSON `{"text": ...}`      |

### 为什么 email 这一轮仍然不发

发邮件要一个 SMTP 客户端、要证书信任策略（`smtplib` 默认校验对端，
而内网邮件服务器常常是自签的）、要处理 TLS 与 AUTH 的组合、还要一个
**异步化的线**（`smtplib` 是同步阻塞的，直接放进事件循环会卡住整个
服务）。这几件事各有各的判断（信任谁的自签证书？用谁的凭据？），
塞进这一轮只会得到一个半吊子实现。

诚实的做法是：email 渠道被**选中**时，结果里写"未发送（邮件通道尚未
接入）"，和 v0.22 里那句"尚未接入"是同一个口径。**不假装发了**。

⚠️ webhook 与 slack 是**同一件事的两种载荷**（都是 POST JSON），所以
这一轮能真做完。它们是用户最需要的两种（接 Jira / 泳道 / IM）。

## ★ SSRF

`target` 是**管理员填的** URL —— 这本身就是一类 SSRF 面：填
`http://169.254.169.254/latest/meta-data/` 就能让服务端去打云元数据。

判据放在**连接层**（`app/core/netguard.py` 的 `GuardedHTTPTransport`），
而不是在字符串上正则一把：`169.254.169.254.nip.io` 这种域名写法要解析
之后才知道指向哪。

## ★ 重试

不重试。这类通知的时效窗口很短（"扫描完成了"晚 5 分钟说就没有意义），
而对端 5xx 时立即重试会**加速**把对端打挂。失败就记下来，由用户在渠道
页面看到红点并手动重测 —— 与我们不做无限轮询是同一个取舍。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

import httpx
from sqlalchemy.orm import Session

from app.core import netguard
from app.core.vocab import NotifyChannelKind, Severity
from app.settings.models import NoticeRule, NotifyChannel

logger = logging.getLogger(__name__)

#: 单次发送的超时（秒）。**总时长**，不是每阶段。
#:
#: 为什么是 8 秒：这是一条"扫完了"的通知，人在等它。而对端是个 webhook
#: 接收器（Jira / 泳道 / 自建服务），正常应在 1 秒内应答。8 秒已经覆盖了
#: 跨地域 + 对端排队的情况；再久就等于让一次发送占住一条工作线程一分多钟，
#: 而那条线程可能正在处理别的扫描的收尾。
SEND_TIMEOUT_SECONDS: Final[float] = 8.0

#: 严重度的序，用来比较"这条发现够不够格发"。
#:
#: 与 `NoticeRule.min_severity` 同一套词（`Severity`）。放在这里做成本地
#: 顺序表，是因为比较发生在**筛选**这一步，而不是在 SQL 里 —— 见
#: `_passes_severity`。
_SEVERITY_RANK: Final[dict[str, int]] = {
    Severity.INFO.value: 0,
    Severity.LOW.value: 1,
    Severity.MEDIUM.value: 2,
    Severity.HIGH.value: 3,
    Severity.CRITICAL.value: 4,
}


#: 邮件渠道"这一轮不发"的标准说法。
#:
#: ★ 抽成常量而不是在两处各写一遍文案：`build_body`（批量投递路径）与
#: `channel_problem`（`/test` 路径）都要说这句话，而**同一个问题在两条
#: 路径上给出两种说法**是最容易让人困惑的事情之一 —— 用户会以为其中
#: 一条是旧版本。
#:
#: 它写的是「本轮只实现了 webhook 与 slack」而不是「不支持邮件」：
#: 后者听起来像产品的永久限制，前者才是实话，也顺带告诉用户现在能用什么。
_EMAIL_UNSUPPORTED: Final[str] = "邮件通道尚未接入（本轮只实现了 webhook 与 slack）"


class _ChannelLike(Protocol):
    """`_post` 对渠道的全部要求：四个只读字段。

    用 Protocol 而不是把入参写成联合类型，是因为两个来源彼此无关：
    批量路径传 `_ChannelView`（值快照），单条试发路径传 `NotifyChannel`
    （ORM 对象）。它们唯一的共同点是这四样东西可读 —— 那就只说这一件事。
    """

    @property
    def id(self) -> str: ...

    @property
    def label(self) -> str: ...

    @property
    def kind(self) -> str: ...

    @property
    def target(self) -> str: ...


@dataclass(frozen=True)
class _ChannelView:
    """一条渠道的**值快照**。

    ## ★ 为什么不在循环里直接用 ORM 对象

    两条理由，都跟"发送不该知道数据库的事"有关：

    ① `_post()` 的契约是「给我一条渠道的值，我发出这条消息」。它无法
       从实体上判断"这条是否需要清失败原因"，也不该承担会话生命周期的
       责任。值对象把这层隔离钉在类型上（`_ChannelLike`）。

    ② 循环里每条渠道都可能 `mark_channel_verified`（内部 commit），而
       `_post` 是网络调用。把网络调用与会话状态混在同一个循环里，失败
       现场就会从"对端没响应"变成 SQLAlchemy 报的别的东西 —— 最难查
       的一类。

    ⚠️ **不要**把这里理解成在防 `expire_on_commit`。本项目的 sessionmaker
    是 `expire_on_commit=False`（`app/core/db.py:147`），commit 之后其余
    实体不会过期；实测直接遍历 ORM 实体的 SELECT 次数与快照版完全相同
    （各 6 次）。也就是说「直接用实体」在本项目跑得通，值快照换来的是
    上面 ①② 的可维护性，不是一个现存 bug 的修复。

    `verified` 也拍进来，是因为"要不要清除上次的失败原因"这个判断
    必须在发送**之前**就定下来 —— 发送之后的 `channel.verified` 已经是
    别的东西了。
    """

    id: str
    label: str
    kind: str
    target: str
    verified: bool


@dataclass(frozen=True)
class NoticePayload:
    """要通知的那件事 —— 与"怎么发"无关。

    ★ 这里的字段是**跨渠道共有的最小集**：谁扫的、扫了什么、什么结果、
    有几条高危。各渠道自己决定怎么用它们（slack 拼一句话，webhook 原样
    序列化）。渠道专属的东西（如 Slack 的 `blocks`）不进这里。
    """

    scan_id: str
    target: str
    status: str
    summary: str
    severity: str
    duration_sec: int | None = None
    critical_count: int = 0
    high_count: int = 0
    scan_url: str | None = None

    def as_webhook_body(self) -> dict[str, Any]:
        """webhook 的 JSON 载荷。

        键名用 snake_case（不含前端 camelCase 约定）：这是**对外**的
        集成协议，它的消费者是别人写的脚本，改一次名字就断一次。
        """
        return {
            "event": "scan.finished",
            "scan_id": self.scan_id,
            "target": self.target,
            "status": self.status,
            "summary": self.summary,
            "severity": self.severity,
            "duration_sec": self.duration_sec,
            "critical_count": self.critical_count,
            "high_count": self.high_count,
            "scan_url": self.scan_url,
        }

    def as_slack_text(self) -> str:
        """Slack 的一句话文案。

        ★ 第一段就是结论（"扫描完成" / "扫描失败"），不带前缀标题：
        Slack 的消息预览截断得很早，前缀会把结论挤出去。
        """
        head = f"{self.target}：{self.summary}"
        counts = []
        if self.critical_count:
            counts.append(f"严重 {self.critical_count}")
        if self.high_count:
            counts.append(f"高危 {self.high_count}")
        tail = f"（{'、'.join(counts)}）" if counts else ""
        link = f" <{self.scan_url}|查看>" if self.scan_url else ""
        return f"{head}{tail}{link}"


@dataclass
class ChannelResult:
    """一条渠道的发送结果。

    `sent=False` 时 `reason` **必须**非空 —— 否则界面只能显示一个红点，
    而用户无从知道该改什么。这是 `verify_error` 那条规矩的延伸。
    """

    channel_id: str
    label: str
    kind: str
    sent: bool
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.sent and not self.reason:
            raise ValueError("发送失败必须给原因 —— 界面要据此告诉用户改哪里")


@dataclass
class DispatchOutcome:
    """一次投递的整体结果。

    ★ `skipped` 与 `results` 分开：前者是"规则说这次不发"（正常状态，
    比如阈值没够、或扫描失败而规则只关心成功），后者是"发了但结果如何"。
    混在一起会让界面把"按规则没发"也渲染成一个失败。
    """

    sent: int = 0
    failed: int = 0
    skipped: bool = False
    skip_reason: str = ""
    results: list[ChannelResult] = field(default_factory=list)

    @property
    def delivered(self) -> bool:
        """是否至少有一条真的送上去了。"""
        return self.sent > 0


# ══════════════════════════════════════════════════════════
# 规则筛选
# ══════════════════════════════════════════════════════════


def _passes_severity(severity: str, min_severity: str) -> bool:
    """这条通知的严重度够不够格，按 `NoticeRule.min_severity` 比。

    ★ 未知严重度**不放行**（返回 False）：数据里出现一个我们不认识的值，
    多发一条通知的代价（打扰人、稀释信号）比少发一条（他下次自己看）更大。
    这与 `vocab.scopes_for_role` 对未知角色返回空集是同一条取向。
    """
    rank = _SEVERITY_RANK.get(severity)
    threshold = _SEVERITY_RANK.get(min_severity)
    if rank is None or threshold is None:
        return False
    return rank >= threshold


def should_notify(rule: NoticeRule, payload: NoticePayload) -> tuple[bool, str]:
    """按规则判断这次要不要发。返回 `(发不发, 不发的原因)`。

    ## ★ 为什么"扫描失败"永远要发

    `notify_on_scan_complete` 字面只说"完成"，但**失败比成功更需要通知**：
    成功的结果躺在那儿等用户下次打开看；失败意味着"你交代的这件事没做成"，
    而他可能正指望今晚有结果。所以失败不受 `notify_on_scan_complete`
    约束 —— 那条开关控制的是"每次扫完都吵你一下"，不是"出事也别吭声"。

    ## `notify_on_critical_immediate`

    这个开关在**扫描结束时**没有作用 —— 它的意思是在扫描**过程中**
    发现严重漏洞就立刻发。本轮只在收尾投递，所以它不影响这里的判断。
    它在 `_SKIP_*` 之外的说明写在设置页上。
    """
    if payload.status != "completed":
        # 失败 / 中止：一定发，只看渠道在不在（由 list_channels 决定）。
        return True, ""

    if not rule.notify_on_scan_complete:
        return False, "通知规则关掉了「扫描完成后通知」"

    if not _passes_severity(payload.severity, rule.min_severity):
        return (
            False,
            f"本次最高严重度是 {payload.severity}，低于规则的 {rule.min_severity}",
        )
    return True, ""


def list_channels(db: Session) -> list[NotifyChannel]:
    """可以真的发出去的渠道。

    ★ **只返回 enabled 的**：停用是一个信号，不是装饰。老提示里那句
    "停用的渠道不该被发送"就是这条。与 settings 页的 `list_channels`
    （返回全部，含停用）是两回事 —— 那个是给界面看的台账。
    """
    from app.settings.service import list_channels as _all_channels

    return [channel for channel in _all_channels(db) if channel.enabled]


# ══════════════════════════════════════════════════════════
# 真正的发送
# ══════════════════════════════════════════════════════════


def build_body(kind: str, payload: NoticePayload) -> tuple[dict[str, Any], str]:
    """按渠道**类型**造 (json body, 一句话描述)。发不出去时抛 `ValueError`。

    ★ 收的是 `kind: str` 而不是渠道对象：`/test` 端点要在**入库之前**、
    甚至对一条还不存在的渠道试跑一次载荷形状，那里没有 ORM 实例可传。
    收类型串让这条路走得通，也让本函数变成纯函数（好测）。

    分支集中在这里，是为了让"每种渠道发什么"只有一处 —— 加第三种渠道时
    只改这里，不需要动 HTTP 那一层。

    ## ★ 两种"发不出去"要给不同的话

    * **email**：一个**已知、已规划、但这一轮没做**的类型。用户配了邮箱却
      收不到通知时，他要的是"为什么"，而不是"不支持" —— 后者听起来像
      这个产品的永久限制。所以这条单独写清楚"本轮只实现了 webhook 与 slack"。
    * **未知类型**：`carrier-pigeon` 这种只可能是编程错误或手工改库，
      "这一轮不支持 X" 就够了。

    这两句话都必须与 `channel_problem` 里那句**一模一样** —— 同一个问题
    在"批量投递"和"/test"两条路径上不能给出两种说法，否则用户看到的是
    "测试时说邮件尚未接入、扫描结束时说这一轮不支持 email"。
    """
    if kind == NotifyChannelKind.WEBHOOK.value:
        return payload.as_webhook_body(), "webhook JSON"
    if kind == NotifyChannelKind.SLACK.value:
        return {"text": payload.as_slack_text()}, "slack text"
    if kind == NotifyChannelKind.EMAIL.value:
        # 与 `channel_problem` 同一个常量，保证两处说的是同一句话。
        raise ValueError(_EMAIL_UNSUPPORTED)
    raise ValueError(f"这一轮不支持 {kind} 渠道的发送")


def _post(channel: _ChannelLike, payload: NoticePayload) -> ChannelResult:
    """同步发一条。只在**事务外**调用（见模块 docstring）。

    ⚠️ `trust_env=False`：出站请求不走环境变量里的代理。理由与
    `providers/llm.py` 里那段完全一致 —— 打开 `trust_env` 等于把
    "这次请求由谁收"交给环境，而"这台机器上还有别的进程共享环境"。

    入参是 `_ChannelLike` 而不是 `NotifyChannel`：批量路径传的是
    `_ChannelView` 值快照（理由见那个类），单条试发路径传的是 ORM 对象。
    这里只用四个只读字段，两种都满足。
    """
    try:
        body, _shape = build_body(channel.kind, payload)
    except ValueError as exc:
        return ChannelResult(
            channel_id=channel.id,
            label=channel.label,
            kind=channel.kind,
            sent=False,
            reason=str(exc),
        )

    try:
        with httpx.Client(
            timeout=httpx.Timeout(SEND_TIMEOUT_SECONDS),
            trust_env=False,
            transport=netguard.GuardedHTTPTransport(),
        ) as client:
            # fail-closed：确认 guard 真装在了所有池上（含代理池）。
            netguard.ensure_guarded(client)
            response = client.post(channel.target, json=body)
    except netguard.BlockedAddressError:
        # ★ 单独一条：这不是"对端挂了"，是**配置里填了个内网地址**。
        # 混进通用错误里会让人去查网络，而真正该看的是渠道配置。
        return ChannelResult(
            channel_id=channel.id,
            label=channel.label,
            kind=channel.kind,
            sent=False,
            reason="目标地址解析到内网/本机地址，已按安全策略拦下",
        )
    except httpx.TimeoutException:
        return ChannelResult(
            channel_id=channel.id,
            label=channel.label,
            kind=channel.kind,
            sent=False,
            reason=f"对端 {SEND_TIMEOUT_SECONDS:g} 秒内没有应答",
        )
    except httpx.HTTPError as exc:
        return ChannelResult(
            channel_id=channel.id,
            label=channel.label,
            kind=channel.kind,
            sent=False,
            reason=f"请求失败：{type(exc).__name__}",
        )

    if response.status_code >= 400:
        # 截一段响应体：对端常说"为什么不收"（Jira 会返回字段错误）。
        # 截 200 字符是因为它会进 `verify_error` 列（Text），而错误消息
        # 太长会在界面上撑坏布局；前 200 字通常已经含着那条原因。
        snippet = (response.text or "").strip()[:200]
        return ChannelResult(
            channel_id=channel.id,
            label=channel.label,
            kind=channel.kind,
            sent=False,
            reason=f"对端返回 HTTP {response.status_code}" + (f"：{snippet}" if snippet else ""),
        )

    return ChannelResult(
        channel_id=channel.id,
        label=channel.label,
        kind=channel.kind,
        sent=True,
    )


def dispatch_one(channel: NotifyChannel, payload: NoticePayload) -> ChannelResult:
    """对**一条**渠道发一次，不读规则、不写库。

    给 `/channels/{id}/test` 用：那条路要的是"这一条通不通"，而不是
    "按业务规则要不要通知"。拆出来也让测试不必造 `NoticeRule`。

    ⚠️ 它**不**更新 `verified` / `verify_error` —— 那是调用方的事。
    本函数只回答"发成了没有"，写库由 `dispatch`（批量路径）或路由（单条
    路径）各自决定，这样两处都不会漏写、也不会写两遍。
    """
    return _post(channel, payload)


def dispatch(db: Session, payload: NoticePayload) -> DispatchOutcome:
    """按规则与渠道把一条通知发出去。**同步**，调用方负责放到线程里。

    流程：读规则 → 判断要不要发 → 读启用的渠道 → 逐条发 → 汇总。

    ## ★ 一条渠道的失败不影响其他渠道

    这是刻意的不短路：`for` 里每条各自 try，结果各自记。理由是用户
    往往同时配了 Slack 与 webhook（一个给人看、一个进工单系统），
    Slack 挂了不该让工单系统也收不到。

    ## ★ 结果会写回 `verify_error`

    发送失败时把原因写进那条渠道的 `verify_error` 并置 `verified=False`，
    界面上就是一个红点 + 一句原因。这样"我的通知为什么没收到"不需要
    翻日志 —— 它就在渠道列表上。
    """
    from app.settings.service import get_notice_rule, mark_channel_verified

    rule = get_notice_rule(db)
    ok, reason = should_notify(rule, payload)
    if not ok:
        return DispatchOutcome(skipped=True, skip_reason=reason)

    channels = list_channels(db)
    if not channels:
        # 不是错误：没有渠道 = 用户没配过 = 他本来就不想要通知。
        return DispatchOutcome(skipped=True, skip_reason="没有启用中的通知渠道")

    # ★ 先把渠道拍成**纯值**再发送（v0.89）。
    #
    # 为什么坚持值对象而不是直接遍历 ORM 实体：
    #
    # ① `_post()` 签名的契约就是「给我一条渠道的值，我发出这条消息」。
    #    它不该、也无法从实体上知道"这条已经发过了吗"。值对象把这层
    #    隔离钉死在类型上（`_ChannelLike`），而不是靠调用方自觉。
    #
    # ② 发送与写库是**两件事**。循环里每条渠道都可能 `mark_channel_verified`
    #    （内部 commit），而 `_post` 是网络调用。一旦某个分支把网络调用
    #    和会话状态混在一起，失败现场就会从"对端没响应"变成
    #    "SQLAlchemy 报了个别的东西" —— 那是最难查的一类。
    #
    # ⚠️ 这里**不是**在防 `expire_on_commit`：本项目的 sessionmaker 是
    #    `expire_on_commit=False`（`app/core/db.py:147`），commit 之后其余
    #    实体不会过期，实测两条路径的 SELECT 次数完全相同（各 6 次）。
    #    换句话说，改成直接遍历实体在本项目里**跑得通** —— 值快照换来的是
    #    上面 ①② 的可维护性，不是修一个现存 bug。别把它当成防御性 bugfix 写。
    #
    # 拍掉之后 `_post` 拿到的是值对象，与事务生死无关。
    snapshot = [
        _ChannelView(
            id=channel.id,
            label=channel.label,
            kind=channel.kind,
            target=channel.target,
            verified=channel.verified,
        )
        for channel in channels
    ]

    outcome = DispatchOutcome()
    for view in snapshot:
        result = _post(view, payload)
        outcome.results.append(result)
        if result.sent:
            outcome.sent += 1
            # 成功了就把上次的失败原因清掉 —— 否则一个已修好的红点会
            # 一直挂在界面上，而下一次发送只会在旁边默默加一条。
            if not view.verified:
                mark_channel_verified(db, view.id, ok=True)
        else:
            outcome.failed += 1
            mark_channel_verified(db, view.id, ok=False, error=result.reason)
            logger.warning(
                "通知发送失败 channel=%s kind=%s reason=%s",
                view.id,
                view.kind,
                result.reason,
            )

    logger.info(
        "通知已投递 scan=%s sent=%d failed=%d",
        payload.scan_id,
        outcome.sent,
        outcome.failed,
    )
    return outcome


def channel_problem(kind: str, target: str) -> str | None:
    """这个渠道这一轮能不能真发。返回原因，能发返回 `None`。

    给 `/test` 端点用：它要能诚实回答"发了没有"。

    ⚠️ 只按 `kind` 判载荷、**不碰 `target` 的内容** —— 地址形状是
    `router._target_shape_problem` 的活（那里对三类各自宽松）。这里回答的是
    另一个问题：「即使地址是对的，这条渠道我们发得出去吗」。
    """
    if kind == NotifyChannelKind.EMAIL.value:
        return _EMAIL_UNSUPPORTED
    try:
        build_body(
            kind,
            NoticePayload(scan_id="", target="", status="completed", summary="测试", severity="info"),
        )
    except ValueError as exc:
        return str(exc)
    return None


__all__ = [
    "SEND_TIMEOUT_SECONDS",
    "ChannelResult",
    "DispatchOutcome",
    "NoticePayload",
    "build_body",
    "channel_problem",
    "dispatch",
    "dispatch_one",
    "list_channels",
    "should_notify",
]
