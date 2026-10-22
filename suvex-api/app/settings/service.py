"""`settings` 领域的读写逻辑。

## 为什么要有这一层

`sandbox_config` 是一张**单例表**。"没有就建一条"这件事如果散落在路由与
调度器两处各自实现，迟早会有一处忘了建、或者两处建出两行 —— 而单例表一旦
出现两行，行为就变成"看哪个先返回"，不可复现。取/建只有一个版本。

## ★ 覆盖优先级：DB 覆盖 env，env 是兜底

执行类参数（并发 / 回合上限 / 扫描深度）在两个地方都有值：

* `app/core/config.py` 的 env 默认值 —— **部署基线**。CI、迁移、
  "用户从没打开过设置页"的实例，走的都是它；
* `sandbox_config` 的三列 —— 用户在设置页改过的值。

规则：**DB 有值就用 DB，为 `NULL` 就退回 env。**

`NULL` 在这里不是"没配置"，而是一个明确的语义：**用户没覆盖过**。
所以那三列刻意可空 —— 见 `settings/models.py` 里的说明。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.vocab import ScanMode, values
from app.settings.models import (
    SINGLETON_ID,
    KernelConfig,
    NoticeRule,
    NotifyChannel,
    SandboxConfig,
)

# ── 执行参数的合法区间 ────────────────────────────────────
#
# 下限取 1 而不是 0：并发 0 等于"永远不启动任何扫描"，
# 那不是配置而是把自己锁在门外。
MIN_CONCURRENT_SCANS = 1
MAX_CONCURRENT_SCANS = 8
# 单个 agent 的回合上限。strix 自己的默认值是 500；给它一个上限是因为
# "无限回合"等于"费用不可控"，而这是**会对真实目标发包**的进程。
MIN_SCAN_MAX_TURNS = 10
MAX_SCAN_MAX_TURNS = 2000


@dataclass(frozen=True, slots=True)
class ExecutionSettings:
    """当前**实际生效**的执行参数（DB 覆盖后的结果）。

    单独一个类型而不是直接返回 `Settings`：调用方（调度器、runner）
    只该看到"生效值"，不该知道"这个值是从 DB 还是 env 来的"。
    把来源暴露出去，迟早会有人在调用点上再判一次优先级 —— 那就是第二份口径。
    """

    max_concurrent_scans: int
    scan_max_turns: int
    scan_mode: str


def get_sandbox_config(db: Session) -> SandboxConfig:
    """读沙箱配置，没有就建一条默认的。"""
    config = db.get(SandboxConfig, SINGLETON_ID)
    if config is None:
        config = SandboxConfig(id=SINGLETON_ID)
        db.add(config)
        db.commit()
        db.refresh(config)
    return config


def read_sandbox_config(db: Session) -> SandboxConfig | None:
    """只读读沙箱配置。**没有就返回 None，不建行。**

    ★ 与 `get_sandbox_config()` 的分工要说清楚：

    * 设置页 / `PUT` 走 `get_sandbox_config()` —— 那里"读"本身就要有东西可编辑，
      建行是合理的；
    * **启动扫描**走这个 —— 它只想拿当前配额。让他因为跑了一次扫描
      就凭空建出一行配置，会让"这一行是什么时候建的、按哪版默认值建的"
      变得不可知（与 `settings/models.py` 里那条"不用数据库默认值"的
      理由是同一条）。

    没有行时返回 None，调用方据此**不下发任何配额环境变量** ——
    那正是 strix 的默认行为（docker 不限），也就是用户从未配置过的真实语义。
    """
    return db.get(SandboxConfig, SINGLETON_ID)


def effective_execution_settings(db: Session | None = None) -> ExecutionSettings:
    """算出生效的执行参数。**只读，不建行。**

    ## 为什么不建行

    调度器**每 2 秒**调一次它。为一个读操作写库既没必要，也会让
    "只是跑了一轮调度"变成一次数据库写入 —— 而那张表的 `updated_at`
    是用来回答"用户什么时候改过配置"的，不该被调度器反复刷新。

    所以这里用 `db.get()`（没有就返回 None），而不是 `get_sandbox_config()`。
    """
    env = get_settings()
    row = db.get(SandboxConfig, SINGLETON_ID) if db is not None else None

    # ⚠️ 用 `is not None` 而不是 `or`：后者会把合法的 `0` 也当成"没设"。
    #    当前 `0` 被下面的区间校验挡在外面，但那是另一处的规则 ——
    #    依赖它来保证这里的正确性，等于让两条规则互相拴死。
    concurrent = (
        row.max_concurrent_scans
        if row is not None and row.max_concurrent_scans is not None
        else env.max_concurrent_scans
    )
    max_turns = (
        row.scan_max_turns
        if row is not None and row.scan_max_turns is not None
        else env.scan_max_turns
    )
    mode = row.scan_mode if row is not None and row.scan_mode else env.scan_mode

    return ExecutionSettings(
        max_concurrent_scans=concurrent,
        scan_max_turns=max_turns,
        scan_mode=mode,
    )


def validate_execution_settings(
    *,
    max_concurrent_scans: int,
    scan_max_turns: int,
    scan_mode: str,
) -> None:
    """校验执行参数。不合法就抛 422，**不静默截断**。

    不截断的理由：把用户填的 `999` 悄悄改成 `8` 并保存成功，他下一次
    看到的是一个自己没设过的值 —— 而"我明明填了 999"这件事没有任何痕迹。
    宁可当次报错说清范围。
    """
    if not (MIN_CONCURRENT_SCANS <= max_concurrent_scans <= MAX_CONCURRENT_SCANS):
        raise ApiError.validation(
            f"同时运行的扫描数应在 {MIN_CONCURRENT_SCANS}-{MAX_CONCURRENT_SCANS} 之间",
            {
                "maxConcurrentScans": (
                    f"应在 {MIN_CONCURRENT_SCANS}-{MAX_CONCURRENT_SCANS} 之间，"
                    f"收到 {max_concurrent_scans}"
                )
            },
        )
    if not (MIN_SCAN_MAX_TURNS <= scan_max_turns <= MAX_SCAN_MAX_TURNS):
        raise ApiError.validation(
            f"回合上限应在 {MIN_SCAN_MAX_TURNS}-{MAX_SCAN_MAX_TURNS} 之间",
            {
                "scanMaxTurns": (
                    f"应在 {MIN_SCAN_MAX_TURNS}-{MAX_SCAN_MAX_TURNS} 之间，"
                    f"收到 {scan_max_turns}"
                )
            },
        )
    if scan_mode not in values(ScanMode):
        raise ApiError.validation(
            f"扫描深度取值不合法：{scan_mode!r}",
            {"scanMode": f"应为 {'、'.join(values(ScanMode))} 之一"},
        )


# ══════════════════════════════════════════════════════════
# CTF2 解题策略（v0.20）
# ══════════════════════════════════════════════════════════
#
# ⚠️ 这里**只放读取**。写入（`update_ctf2_settings`）在 `app/kernel/service.py` ——
#    因为校验规则（"什么算合法的提交策略"）是 CTF2 的领域知识，
#    放进 settings 会让这个模块开始理解 CTF2 的语义。
#
# 而这个读函数必须在这里，因为设置页装配时要读它，而 settings
# 不该 import ctf2 的业务模块（那会形成一个反向依赖）。


def read_ctf2_config(db: Session) -> KernelConfig | None:
    """读 CTF2 配置行。**没有就返回 None，不建行。**

    与 `read_sandbox_config` 同一个模式：为一个读操作写库既没必要，
    也会让 `updated_at`（回答"用户什么时候改过"）被反复刷新。
    没有行时调用方用代码常量兜底（见 `ctf2.service.effective_ctf2_settings`）。
    """
    return db.get(KernelConfig, SINGLETON_ID)


# ══════════════════════════════════════════════════════════
# 通知（v0.22，任务 7 的最后一块）
# ══════════════════════════════════════════════════════════
#
# ## 为什么 `NoticeRule` 用「没有就建」，而 `NotifyChannel` 不
#
# `NoticeRule` 是**单例**：它必须有值（"什么级别才发"不能是空的），
# 所以 `get_notice_rule()` 没有就建一条默认的 —— 与 `get_sandbox_config`
# 同一个模式。
#
# `NotifyChannel` 是**列表**：空表是完全正常的状态（"我还没配任何渠道"），
# 所以它没有"建一条默认的"这回事 —— 凭空造一个渠道出来是荒谬的。


def get_notice_rule(db: Session) -> NoticeRule:
    """读通知规则，没有就建一条默认的。"""
    rule = db.get(NoticeRule, SINGLETON_ID)
    if rule is None:
        rule = NoticeRule(id=SINGLETON_ID)
        db.add(rule)
        db.commit()
        db.refresh(rule)
    return rule


def list_channels(db: Session) -> list[NotifyChannel]:
    """全部通知渠道，按创建时间正序。

    ★ **不隐藏 `enabled=False` 的渠道。** 停用是一个状态，不是删除 ——
    用户需要看到"我配过这个、现在停着"，否则他会以为它丢了并重新配一个。
    （与 `GET /settings/tokens` 的"已吊销不列出"不同：那里的令牌
    已经彻底失效、留着只会造成"它还能用"的误解。）
    """
    return list(
        db.scalars(select(NotifyChannel).order_by(NotifyChannel.created_at, NotifyChannel.id))
    )


def get_channel(db: Session, channel_id: str) -> NotifyChannel:
    """按 id 取渠道，不存在就 404。

    404 而不是 400：`/settings/notices/channels/{id}` 是**资源路径**，
    路径里的 id 不存在就是"这个资源不存在" —— 与 `ApiError.not_found`
    的语义一致。
    """
    channel = db.get(NotifyChannel, channel_id)
    if channel is None:
        raise ApiError.not_found("通知渠道")
    return channel


def create_channel(
    db: Session,
    *,
    kind: str,
    label: str,
    target: str,
    enabled: bool,
) -> NotifyChannel:
    """新建渠道。

    ⚠️ `verified` 一律从 `False` 开始，**不接受客户端传**。
    "已验证"是服务端真的发过一次测试通知的**事实**，不是用户的声明 ——
    让请求体带这个字段等于让调用方撒谎，而界面上那个绿勾会变得毫无意义。
    （与 `POST /providers` 不接受 `hasKey` 是同一条规矩。）
    """
    channel = NotifyChannel(
        id=new_id("ntc"),
        kind=kind,
        label=label,
        target=target,
        enabled=enabled,
        verified=False,
        verify_error=None,
    )
    db.add(channel)
    db.commit()
    db.refresh(channel)
    return channel


def update_channel(
    db: Session,
    channel_id: str,
    *,
    label: str | None = None,
    target: str | None = None,
    enabled: bool | None = None,
) -> NotifyChannel:
    """部分更新。

    ★ **改了 `target` 就要把 `verified` 打回 `False`。**

    这是本函数里唯一不显然的一处：`verified` 的含义是"**这个地址**
    发过一次测试并成功了"。地址变了，那个事实就不再成立 ——
    留着绿勾会让用户以为新地址也验证过了，而它可能是个错的 URL。
    症状是"配好了、显示已验证、但真出事时一条通知都没发出去"。

    `label` 与 `enabled` 的改动**不影响**验证状态：换个名字、
    临时停用再启用，地址没变，验证事实依然成立。
    """
    channel = get_channel(db, channel_id)

    if label is not None:
        channel.label = label
    if enabled is not None:
        channel.enabled = enabled
    if target is not None and target != channel.target:
        channel.target = target
        channel.verified = False
        channel.verify_error = None

    db.commit()
    db.refresh(channel)
    return channel


def delete_channel(db: Session, channel_id: str) -> None:
    """删除渠道。**硬删除**，与令牌吊销同一条理由：

    渠道是一次性配置，删掉就是不再需要它。软删除会留下"回收站里的渠道"
    这个概念，而它对用户没有任何价值 —— 需要时重新配一个即可
    （不像漏洞或扫描，那些是**证据**，删掉就没了）。
    """
    channel = get_channel(db, channel_id)
    db.delete(channel)
    db.commit()


def mark_channel_verified(
    db: Session, channel_id: str, *, ok: bool, error: str | None = None
) -> NotifyChannel:
    """记录一次测试通知的结果。**由测试端点调用**，不在路由里写库。

    失败时把 `verify_error` 写下来而不是只留一个红点：
    "为什么没通过"是用户唯一能据以修复的信息。
    """
    channel = get_channel(db, channel_id)
    channel.verified = ok
    channel.verify_error = None if ok else (error or "测试通知失败，未返回具体原因")
    db.commit()
    db.refresh(channel)
    return channel
