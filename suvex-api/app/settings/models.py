"""`settings` 领域（沙箱、通知、令牌元数据）。

令牌本身在 `app.auth.models`（它参与鉴权），这里只管界面上的列表投影。

## 初始值取自前端 `settings-store.ts`，只有一处刻意不同

`data/settings-store.ts` 的初始状态是**演示数据**（沙箱 `status: 'running'`、
两条示例通知渠道）。真实实例的初始状态必须是「没有」：

* `sandbox.status` 默认 **`stopped`**。演示数据写 `running` 是因为要给界面
  提供素材；真实初始值是 `running` 的话，界面会宣称沙箱在跑而实际没有 ——
  这是**状态语义撒谎**，与契约 §3.3 拒绝把 `queued` 写成 `running` 是同一条原则。
* `notify_channels` 初始为**空表**。凭空插入 `security@acme.internal` 这种
  演示邮箱，会让人以为系统配好了通知而实际上永远发不出去。

其余数值（镜像、配额、超时、通知规则）逐字取自前端，不另立一套。
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from sqlalchemy import Boolean, CheckConstraint, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.columns import UtcDateTime
from app.core.db import Base
from app.core.timeutil import now_utc
from app.core.vocab import (
    # ★ v0.20：CTF2 解题策略
    Ctf2SandboxProfile,
    Ctf2SubmitMode,
    NotifyChannelKind,
    SandboxEngine,
    SandboxStatus,
    ScanMode,
    Severity,
    SlaStartFrom,
    values,
)

# 单例行的主键。用常量而不是「取第一行」：单例表若靠 `LIMIT 1` 读，
# 一旦意外出现两行，行为就变成「看哪个先返回」—— 而那是不可复现的。
SINGLETON_ID = "default"

# 前端的默认值（`settings-store.ts`）。放在这里作为**创建单例行时的初值**，
# 不是数据库默认值 —— 数据库默认值会让「这一行是什么时候、按哪版默认值建的」
# 变得不可知，而配置的初值在升级时是需要迁移的。
DEFAULT_SANDBOX_IMAGE = "suvex/sandbox:1.4"
DEFAULT_CPU_LIMIT = 4
DEFAULT_MEMORY_LIMIT_MB = 4096
DEFAULT_TIMEOUT_MINUTES = 30

# 前端的校验下限（`settings-store.ts` 的早退分支）。
# 放在模型层是因为它们要被 service 与测试共用 —— 写在两处必然漂移。
MIN_CPU_LIMIT = 1
MIN_MEMORY_LIMIT_MB = 512
MIN_TIMEOUT_MINUTES = 1


class SandboxConfig(Base):
    """沙箱配置。单例行（`id = 'default'`）。"""

    __tablename__ = "sandbox_config"
    __table_args__ = (
        CheckConstraint(
            f"engine IN ({', '.join(repr(v) for v in values(SandboxEngine))})", name="engine_valid"
        ),
        CheckConstraint(
            f"status IN ({', '.join(repr(v) for v in values(SandboxStatus))})", name="status_valid"
        ),
        # ⚠️ 必须放行 NULL —— 这一列可空（NULL = 没被用户覆盖过）。
        #    写成 `scan_mode IN (...)` 会让"没设过"这个正常状态直接被约束打回。
        CheckConstraint(
            "scan_mode IS NULL OR scan_mode IN ("
            + ", ".join(repr(v) for v in values(ScanMode))
            + ")",
            name="scan_mode_valid",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=SINGLETON_ID)
    engine: Mapped[str] = mapped_column(
        String(16), nullable=False, default=SandboxEngine.DOCKER.value
    )
    # ⚠️ 初始 `stopped`，见模块 docstring。
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=SandboxStatus.STOPPED.value
    )
    image: Mapped[str] = mapped_column(String(200), nullable=False, default=DEFAULT_SANDBOX_IMAGE)

    cpu_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=DEFAULT_CPU_LIMIT)
    memory_limit_mb: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_MEMORY_LIMIT_MB
    )
    # 单位用分钟而不是秒：用户心里想的是「给它半小时」，不是「1800 秒」。
    timeout_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_TIMEOUT_MINUTES
    )

    # ⚠️ 默认**允许**出网（false）。这不是「更安全就一定更好」的开关：
    # 扫描经常需要打外部目标、拉依赖。打开它会让针对外部目标的扫描直接失败，
    # 所以界面上必须说清这个取舍（契约 §2.9）。
    network_isolated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 关掉后所有写入都在容器内，随容器销毁。
    allow_volume_write: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # ── 执行类配置（任务 13）────────────────────────────────
    #
    # ★ 这三列**可空**，`NULL` 的确切含义是「用户没在设置页改过，跟着 env 走」。
    #
    # 为什么不给非空默认值：非空默认值会在**建行那一刻**把配置冻住 ——
    # 更糟的是它会写一个代码常量，于是 `SUPEX_MAX_CONCURRENT_SCANS=3`
    # 这种 env 覆盖被静默无视。而用户要的语义恰恰是
    # 「设置页改过就以设置页为准，没改过就跟着 env」= `NULL` 即未覆盖。
    #
    # ## 生效时机（两者都不需要重启服务）
    #
    # · `max_concurrent_scans` —— 调度器**每 tick 重读**，改完秒级生效。
    #   这一点很要紧：`recover_orphans()` 会在服务重启时**终止所有正在跑的
    #   扫描**（见 scheduler 的说明），所以"改配置要重启"在这个系统里
    #   代价极高，免重启不是锦上添花。
    # · `scan_max_turns` / `scan_mode` —— 在**启动扫描时**读取，
    #   对之后新发起的扫描生效；正在跑的那次不受影响（它已经带着旧参数
    #   起进程了，中途换参数没有意义）。
    max_concurrent_scans: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scan_max_turns: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scan_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=now_utc, onupdate=now_utc
    )


class NoticeRule(Base):
    """通知规则 —— **什么情况下发**。

    单例行。与 `NotifyChannel`（怎么发）**是两个维度，不是一件事**（契约 §2.9）：

    > 把「Slack 地址」和「只通知严重漏洞」放在同一个对象里，会导致
    > 「我想让邮件收到所有级别、Slack 只收严重」这种真实需求无处表达。

    所以这张表里没有地址，`NotifyChannel` 里没有阈值。
    """

    __tablename__ = "notice_rule"
    __table_args__ = (
        CheckConstraint(
            f"min_severity IN ({', '.join(repr(v) for v in values(Severity))})",
            name="min_severity_valid",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=SINGLETON_ID)
    min_severity: Mapped[str] = mapped_column(
        String(16), nullable=False, default=Severity.HIGH.value
    )
    notify_on_scan_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 这个开关有真实代价：扫描过程中发通知会打断人，
    # 但严重问题早一分钟知道就少一分钟暴露窗口。取舍要写在界面上。
    notify_on_critical_immediate: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=now_utc, onupdate=now_utc
    )


# ── 扫描保留（v0.13，任务 9）──────────────────────────────
#
# 默认 30 天：一个"通常够用"的数字。太短会让用户还没来得及回看就被归档，
# 太长则规则形同虚设（"自动整理"要真的减少列表噪音才有意义）。
DEFAULT_ARCHIVE_AFTER_DAYS = 30
DEFAULT_TRASH_RETENTION_DAYS = 30

# ── SLA 修复时限（任务 10 P3）──────────────────────────────
#
# 按**严重度**给天数。默认值取自业界常见的处置窗口（高危一周内、
# 中危一个月内），但它是可改的 —— 不同组织的合同里写的数字不一样。
#
# ⚠️ `0` 表示**该档不设时限**（不是"立刻到期"）。这是刻意的：
# 低危/提示类问题常常不值得排期，而给它们一个必然超期的假期限
# 只会让"超期"这个信号失效 —— 那是一眼看去全是红的、于是没人看。
DEFAULT_SLA_DAYS: Final[dict[str, int]] = {
    "critical": 3,
    "high": 7,
    "medium": 30,
    "low": 90,
    "info": 0,
}

# 到期前的提醒窗口：剩余天数 ≤ 这个值就算"临期"。
# 它决定界面什么时候从灰变黄 —— 太大会让一切都黄，太小等于没有预警。
SLA_WARN_WITHIN_DAYS = 3
# 「我的视图」上限。设上限是因为它是**用户资产**而非无限日志：
# 20 个视图已经远超任何人会用的数量，而没上限意味着一个脚本能把它撑爆。
MAX_SCAN_VIEWS = 20
MAX_SCAN_VIEW_NAME = 60


class ScanRetentionConfig(Base):
    """扫描保留规则。单例行（`id = 'default'`）。

    ★ 与审计日志的保留期（365 天）是**两件事**，别合并：
    审计记录是合规材料，要留一年且只能追加；扫描记录是日常产物，
    归档 + 回收站共 60 天就够了。把两者绑成一条规则，
    必然在"合规要留久"与"列表要清爽"之间二选一。

    `archive_after_days = 0` 表示**关闭自动归档** —— 显式关闭而不是
    "填一个很大的数"，因为后者会让界面上显示"36500 天后归档"这种噪音。
    """

    __tablename__ = "scan_retention_config"
    __table_args__ = (
        CheckConstraint("archive_after_days >= 0", name="archive_days_non_negative"),
        CheckConstraint("trash_retention_days >= 0", name="trash_days_non_negative"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=SINGLETON_ID)
    archive_after_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_ARCHIVE_AFTER_DAYS
    )
    trash_retention_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_TRASH_RETENTION_DAYS
    )
    # 上次清理时刻。**必须暴露给前端** —— 否则"我设了 30 天，
    # 为什么这条还没被归档"是一个无法回答的问题（规则没生效？还是没到时间？）。
    last_sweep_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=now_utc, onupdate=now_utc
    )


class FindingSlaConfig(Base):
    """SLA 修复时限（任务 10 P3）。单例行（`id = 'default'`）。

    ## 它回答的问题

    "这条高危挂了 12 天了，算不算久？" —— 没有这个配置，界面只能显示
    "12 天前"，而判断"该不该催"全靠人肉记忆。有了它，列表能直接标
    「超期 5 天」/「还剩 2 天」。

    ## 为什么按严重度而不是一刀切

    高危和低危的处置窗口本来就不同（一个按天、一个按季度）。一刀切
    会逼用户自己心里换算，而那种换算在几十条上做不了。

    ## ★ `0` = 该档**不设时限**（不是"立刻到期"）

    低危/提示类常常不值得排期。给它们一个必然超期的假期限，
    后果是**"超期"这个信号整体失效** —— 一眼看去全是红的，
    于是没有人再看那个红点。宁可让它们安静地不参与 SLA。

    ## 为什么起算点可配

    `start_from` 取 `discovered`（发现时间）或 `confirmed`（确认时间）。
    按发现时间起算是常见的做法，但它有个已知的不公平：**确认得晚的团队
    会被罚**（漏洞在队列里躺了一周，之后才开始算它的修复期）。
    这是个策略选择，所以交给配置而不是替用户决定。
    """

    __tablename__ = "finding_sla_config"
    __table_args__ = (
        # 天数不能为负。0 是合法的（不设时限），负数是配置错误。
        CheckConstraint("critical_days >= 0", name="critical_days_non_negative"),
        CheckConstraint("high_days >= 0", name="high_days_non_negative"),
        CheckConstraint("medium_days >= 0", name="medium_days_non_negative"),
        CheckConstraint("low_days >= 0", name="low_days_non_negative"),
        CheckConstraint("info_days >= 0", name="info_days_non_negative"),
        CheckConstraint("start_from IN ('discovered', 'confirmed')", name="start_from_valid"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=SINGLETON_ID)
    critical_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_SLA_DAYS["critical"]
    )
    high_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_SLA_DAYS["high"]
    )
    medium_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_SLA_DAYS["medium"]
    )
    low_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_SLA_DAYS["low"]
    )
    info_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_SLA_DAYS["info"]
    )
    start_from: Mapped[str] = mapped_column(
        String(16), nullable=False, default=SlaStartFrom.DISCOVERED.value
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=now_utc, onupdate=now_utc
    )


class ScanView(Base):
    """「我的视图」—— 存下来的一组筛选条件（v0.13）。

    ## 为什么存后端而不是 localStorage

    两条理由，第二条更硬：

    1. 视图是**用户资产**，换个浏览器就该还在；
    2. 项目既有原则是"凭据与状态不写浏览器存储"（`localStorage` 同源的
       任何脚本都能读），而视图里可能含目标名这类敏感信息。

    ## 为什么 `query` 存原始串而不是结构化字段

    视图的语义就是"那一组筛选条件"。存串能保证**将来加筛选维度时
    老视图不失效**（新维度就是串里多一个键）；存成固定字段的对象，
    则每加一个维度都要写一次迁移。
    """

    __tablename__ = "scan_views"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(MAX_SCAN_VIEW_NAME), nullable=False)
    # 形如 `status=failed&sort=severity&dir=desc`，**不含前导 ?**
    query: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=now_utc, onupdate=now_utc
    )


class NotifyChannel(Base):
    """通知渠道 —— **怎么发**。

    初始为空表（见模块 docstring）。`verified` / `verify_error` 与
    provider 的同名两列是同一个模式：未验证要能说明原因，
    而不是只显示一个灰点。
    """

    __tablename__ = "notify_channels"
    __table_args__ = (
        CheckConstraint(
            f"kind IN ({', '.join(repr(v) for v in values(NotifyChannelKind))})", name="kind_valid"
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # 用户可读的渠道名，如「安全组邮件列表」
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    # 邮箱 / Webhook URL / Slack Webhook URL
    target: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    verify_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=now_utc, onupdate=now_utc
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<NotifyChannel id={self.id!r} kind={self.kind!r} label={self.label!r}>"


# ══════════════════════════════════════════════════════════
# CTF2 解题策略（v0.20）
# ══════════════════════════════════════════════════════════
#
# ## ★ 为什么**不**并进 `sandbox_config`
#
# 那张表回答的是「扫描跑起来能动什么」（CPU / 内存 / 并发 / 回合上限）——
# 它是**跨领域通用**的执行参数，渗透扫描与 CTF2 解题都读它。
#
# 而下面这三项回答的是「CTF2 的解题怎么对外提交」，是**领域专属**的
# 业务策略。混进去会让一张叫"沙箱配置"的表开始装 CTF2 的提交策略 ——
# 而下一个领域（比如将来的 SRC 对接）也会想往里塞自己的策略，
# 那张表会在半年内变成一锅什么都装的配置。
#
# 判据很简单：**换掉 CTF2 这个领域，这三列还存在吗？** 不存在。
#
# ## ★ 为什么这三列是**非空**的（与 sandbox_config 的执行三列相反）
#
# 那三列可空，语义是"用户没覆盖过 → 走 env 基线"，因为**资源配额在
# 部署时定基线是合理的**（运维知道这台机器有多少核）。
#
# 而 `submit_mode` **不该**由一个环境变量在部署时静默决定：
# 它管的是"机器能不能在无人确认的情况下对外提交 flag"，
# 而那是一次**安全取向**的选择，必须来自人在设置页里的显式动作。
# 一个 `SUPEX_CTF2_SUBMIT_MODE=auto` 会让所有实例默认放开自动提交，
# 而没有人会去看那一行。所以这里非空、初值来自代码常量。


class Ctf2Config(Base):
    """CTF2 解题策略。单例行（`id = 'default'`）。

    与 `SandboxConfig` 共用 `SINGLETON_ID` —— 单例表的主键值统一，
    读代码的人不必记两个不同的字面量。
    """

    __tablename__ = "ctf2_config"
    __table_args__ = (
        CheckConstraint(
            f"submit_mode IN ({', '.join(repr(v) for v in values(Ctf2SubmitMode))})",
            name="submit_mode_valid",
        ),
        CheckConstraint(
            f"default_sandbox_profile IN ({', '.join(repr(v) for v in values(Ctf2SandboxProfile))})",
            name="default_sandbox_profile_valid",
        ),
        # 与 `ctf2/service.py` 的 `MAX_WRONG_SUBMISSIONS_CEILING` 同一条理由：
        # 它是自动提交的刹车片，允许配成任意大等于让用户自己拆掉安全边界。
        #
        # ⚠️ 上限写**字面量**而不是 import 那个常量：CHECK 表达式一旦建好
        # 就固化在数据库里，而常量可以改。写常量会让"改了常量但没写迁移"
        # 表现为"新值在应用层通过、被数据库拒绝"—— 那正是本项目在
        # `findings.status` 上踩过的那类漂移（见迁移 b1f7c0a25e38）。
        # 两者必须**一起**改，而这条注释就是那个提醒。
        CheckConstraint(
            "max_wrong_submissions >= 0 AND max_wrong_submissions <= 10",
            name="max_wrong_submissions_within_ceiling",
        ),
        # 与 `vocab.MIN/MAX_CTF2_SOLVE_POLL_SECONDS` 是同一对。理由同上：
        # 常量能改，CHECK 表达式改不了 —— 两者必须一起动。
        #
        # ⚠️ 下界 0.5 不是"建议值"而是护栏：填 0 会让调度线程变成忙等，
        #    在比赛现场表现为 CPU 打满、解题反而变慢。
        CheckConstraint(
            "solve_poll_seconds >= 0.5 AND solve_poll_seconds <= 30",
            name="solve_poll_seconds_within_range",
        ),
        # 0 = 关闭自动同步；上界 3600 是"最短一小时一次"。
        CheckConstraint(
            "auto_sync_seconds >= 0 AND auto_sync_seconds <= 3600",
            name="auto_sync_seconds_within_range",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=SINGLETON_ID)

    # 提交策略。**默认 safe** —— 见 `vocab.Ctf2SubmitMode` 的说明：
    # 这是安全取向而不是体验取向。
    submit_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default=Ctf2SubmitMode.SAFE.value
    )
    # 每题的错误提交次数上限（已定设计决策 4：默认 3，可配置）。
    max_wrong_submissions: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    # ── 解题并发配额（v0.22）──────────────────────────────
    #
    # ## ★ 为什么解题需要**自己的**配额，而不是复用扫描那个
    #
    # 原来 `ctf2.scheduler.max_concurrent()` 直接读
    # `execution_settings.max_concurrent_scans`，理由是"两者抢同一批资源，
    # 各给一份配额等于超卖一倍"。**那个担心是对的，但实现是反的** ——
    # 两个调度器**各自**数自己的名额、各自拿这同一个上限：
    #
    #     scans/scheduler.py  数 COUNT(Scan WHERE running) <= 3
    #     ctf2/scheduler.py   数 len(self._running)         <= 3
    #
    # 于是 `=3` 时真的能跑 **3 个扫描 + 3 道题 = 6 个**，正好是那份注释
    # 说要避免的超卖 —— 只是超卖发生在两个调度器之间，而不是某一个内部。
    #
    # 正确的形状是**分层配额**：各领域守自己的子配额，
    # **总并发 = 各子配额之和**。这样不需要第三个"总配额"旋钮，
    # 也没有任何一边能越过自己那一份。
    #
    # ⚠️ 迁移之后总并发会从"3（名义）"变成"扫描 3 + 解题 3 = 6（实际）"
    #    —— 这不是新增的能力，是把原来那个**已经存在但没人意识到**的
    #    事实上限写明。要收紧就调这两个值。
    #
    # 为什么放在这张表而不是 `sandbox_config`：判据还是那句
    # "换掉 CTF2 这个领域，这一列还存在吗"—— 不存在。它是解题的
    # 业务配额，不是跨领域通用的执行参数。
    max_concurrent_solves: Mapped[int] = mapped_column(Integer, nullable=False, default=3)

    # ── 自动作业（v0.24：抢首杀）──────────────────────────
    #
    # ## 这三列解决同一个问题：**发现得太晚**
    #
    # 比赛规则里「每道题目前三名额外加 15%/10%/5%」意味着得分不只由
    # "解得多快"决定，还由"多早开始"决定。而在 v0.23 之前，一道新题
    # 在平台上挂出来之后，系统里**根本不存在它** —— 用户得先想起来
    # 点一次同步它才出现，再点一次解题它才开始跑。这中间的人工延迟
    # 就是被让掉的分。
    #
    # 分工：`auto_sync_seconds` 负责"看见"，`auto_priority_new` 负责
    # "排在前面"，`auto_start_new` 负责"直接开始跑"。
    #
    # ## ★ 为什么自动开解单独默认**关**
    #
    # 三者默认值不同，判据是**可逆性**：
    #
    #   · 同步只是读平台（零副作用），忘了开的代价却是确定的（丢加分）
    #   · 提优先级只换排队位置（用户在那个列表上看得见、能改回来）
    #   · 自动开解会**真的消耗**模型额度与解题并发名额 —— 在"题量明显
    #     超过人工能力"的比赛里，它可能把 3 个名额全占在几道根本解不出
    #     的题上，而那是个**不确定**的代价
    #
    # 默认值应该站在"错不起"的那一侧，所以前两个开、第三个关。
    solve_poll_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    auto_sync_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    auto_priority_new: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    auto_start_new: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # ── 解题回合预算（v0.26）──────────────────────────────
    #
    # ★ 为什么它必须可配置：**这个数没有全局最优**。
    #
    # 实测（2026-10-01）同一批题里，`[ACTF2020 新生赛]Exec` 6 轮就出结果，
    # 而 `[FlareOn3]GUI`（.NET 逆向）跑满 40 轮、发出 **37 条各不相同的
    # 命令**（一路分析到 `decryptBuffer` / `DecompressBuffer` 的资源流），
    # 最后因为预算耗尽而停 —— 它不是在兜圈子，是真的需要更多轮。
    #
    # 所以这个值该由人在现场按题权衡：简单题给 40 省额度，硬题给 200。
    # 写死一个数字，等于我们替他决定"哪道题不配解出来"。
    max_turns: Mapped[int] = mapped_column(Integer, nullable=False, default=80)

    # 新题默认用哪个沙箱镜像。题目同步时按分类推导，这里是**兜底值**
    # （分类认不出来时用它）。
    default_sandbox_profile: Mapped[str] = mapped_column(
        String(32), nullable=False, default=Ctf2SandboxProfile.BASE.value
    )

    # ── 换模 fallback（v0.35）──────────────────────────────
    #
    # ## 它解决什么：一次 LLM 调用失败会作废**整道题**
    #
    # 解题是一个串行循环，每一轮都要调一次 LLM。默认 provider 一旦
    # 返回一个不可重试的错误（401 key 失效、429 限流、5xx、或推理模型
    # 长时间过载），当前实现是**直接把整道题判失败** —— 而那时模型可能
    # 已经跑了三十多轮、在沙箱里做完了大量真金白银的分析。
    #
    # 实测（交接文档 §4.3）：一次 `ReadError` 让一道跑到第 14 轮、
    # 已经反汇编出完整 IL 的题全部作废。连接层抖动已经修了（重试），
    # 但"这个 provider 就是不行了"仍然会毁掉整道题。
    #
    # ## ★ NULL = 关闭，这是默认
    #
    # 存的是**另一个 provider 的 id**；没配（NULL）就完全保持原行为
    # —— 与"不替用户编一个他没设过的上限"是同一条纪律。
    #
    # ⚠️ 为什么它是**显式配置**而不是自动挑一个备用：自动挑意味着
    #    "默认 provider 挂了就悄悄换成另一个（可能是**云端、要花钱**的）"，
    #    而那是一次用户没同意的成本决定。换模要花谁的钱、换成谁，
    #    必须由人指定。
    #
    # 不加外键：与 `ctf2_solve_attempts.provider_id` 同一条理由 ——
    # 删掉一个 provider 不该被这里拦住，取不到时按"没配"处理。
    fallback_provider_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=now_utc, onupdate=now_utc
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"<Ctf2Config submit_mode={self.submit_mode!r} "
            f"max_wrong={self.max_wrong_submissions}>"
        )
