"""`settings` 领域的出入参。

契约 §3.7 里只列了路径，没有逐条给请求体形状。这里按前端
`settings-store.ts` 的实际调用补齐，并把它们**写回契约**（§3.7 的请求体补充）——
先改文档再改代码是这个项目的规矩。

本文件只覆盖任务 1 实现的那部分（改口令 + 令牌管理）；
沙箱 / 通知的出入参属于任务 7，那时补。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from app.auth.schemas import ApiTokenOut
from app.core.schemas import CamelModel, StrictModel
from app.core.timeutil import IsoDateTime
from app.core.vocab import TokenScope
from app.providers.schemas import ProviderOut
from app.settings.models import (
    MIN_CPU_LIMIT,
    MIN_MEMORY_LIMIT_MB,
    MIN_TIMEOUT_MINUTES,
)
from app.settings.service import (
    MAX_CONCURRENT_SCANS,
    MAX_SCAN_MAX_TURNS,
    MIN_CONCURRENT_SCANS,
    MIN_SCAN_MAX_TURNS,
)


class CreateTokenRequest(StrictModel):
    """`POST /settings/tokens`。

    `expiresAt` 用**绝对时刻**而不是 `'30d' | 'never'` 这种相对量：
    相对量在服务端与客户端的时钟/时区不一致时会有歧义，
    而前端已经有 `expiryToIso()` 把选项换算成 ISO 了（`data/settings.ts`）。

    `expiresAt: null` = **永不过期**，且这是**显式选项**不是默认值
    （契约 §6：强制过期会被绕过 —— 用户写死一个永久 token 然后忘记它，
    不如让它显式并标注风险）。所以这里不给默认值，由调用方明确传。
    """

    label: str = Field(min_length=1, max_length=120)
    scopes: list[str] = Field(min_length=1)
    expiresAt: datetime | None = None

    @field_validator("scopes")
    @classmethod
    def _known_scopes(cls, value: list[str]) -> list[str]:
        valid = {member.value for member in TokenScope}
        unknown = [item for item in value if item not in valid]
        if unknown:
            raise ValueError(f"未知的权限范围：{'、'.join(unknown)}")
        # 去重保序：重复的 scope 没有语义，但会让界面显示「只读、只读」。
        seen: list[str] = []
        for item in value:
            if item not in seen:
                seen.append(item)
        return seen


class ScanRetentionOut(CamelModel):
    """`GET /settings/scan-retention`。"""

    # 失败/已终止 N 天后自动归档。**0 = 关闭该规则**
    archiveAfterDays: int
    trashRetentionDays: int
    # ★ 已归档的扫描，产物目录再留 N 天后清理（README 缺口清单「扫描产物目录只增不减」那一条）。
    # **0 = 不自动清理**。它与上面两个串成一条生命周期：
    # 归档（30 天）→ 回收站删除（30 天）→ 产物清理（90 天）。
    artifactRetentionDays: int
    # 上次清理时刻。**必须给** —— 否则"我设了 30 天，为什么还没归档"
    # 是一个无法回答的问题（规则没生效？还是时间没到？）
    lastSweepAt: IsoDateTime | None


class ScanRetentionIn(StrictModel):
    """`PUT /settings/scan-retention`。

    ⚠️ 字段都**必填**（不给默认值）。这是配置类端点，
    漏传一个是调用方 bug；给默认值会让它静默地把值改回默认——
    而用户以为自己只是改了一项。

    ## ★★ `artifactRetentionDays` 是**例外**：可选，且"不传 = 保持原值"

    它不跟随上面那条纪律，理由是**它有部署顺序风险**，而另外两个没有
    （它们从一开始就在契约里）：

    | 场景 | 若设为必填 | 若"不传 = 保持原值" |
    | --- | --- | --- |
    | 新前端 + 新后端 | ✅ | ✅ |
    | **旧前端 + 新后端** | **★ 保存直接 400** | ✅ 原值不动，正常工作 |

    实测过这个窗口**真实存在**：线上 `dist/`（2026-10-16 04:49 构建）
    里没有这个字段，而它是 bind mount 的构建产物 —— 后端先部署、
    前端后重建的那个时间窗里，用户一点"保存"就会吃到 400。

    ⚠️ 而"不传就保持原值"**同时**避开了另一个坑：给一个**默认值**
    （比如 `= 90`）会让旧前端保存一次就把用户改过的产物保留期
    **悄悄改回 90**。那是静默的配置改写，比报错更坏 —— 所以这里
    选的是"不传 = 不动它"，不是"不传 = 用默认值"。

    两个方案的取舍很明确：**必填**把风险转成用户可见的报错，
    **保持原值**把风险直接消掉。后者更好，因为旧前端根本不知道
    这个字段的存在，它"没传"**不等于**"用户想改它"。
    """

    archiveAfterDays: int = Field(ge=0, le=3650)
    trashRetentionDays: int = Field(ge=0, le=3650)
    #: 不传（`None`）= **保持数据库里的现值**，不是"改回默认"。
    artifactRetentionDays: int | None = Field(default=None, ge=0, le=3650)


class ScanViewOut(CamelModel):
    id: str
    name: str
    # 原始 query 串（不含前导 ?）    query: str
    createdAt: IsoDateTime


class ScanViewsOut(CamelModel):
    views: list[ScanViewOut]


class ScanViewIn(StrictModel):
    """单条视图。`id` 可空 = 新建（服务端分配）。"""

    id: str | None = None
    name: str = Field(min_length=1, max_length=60)
    query: str = Field(max_length=512)


class ScanViewsIn(StrictModel):
    """覆盖式保存。**全量提交** —— 前端拿着它认为正确的列表整体覆盖。

    为什么不做增删改三个端点：视图是**短列表**（上限 20 条）且
    编辑它的是一个人。"整体覆盖"让前端不必维护三套调用，
    也不会出现"删了一条但列表没刷新"的中间态。
    """

    views: list[ScanViewIn] = Field(max_length=20)


class FindingSlaOut(CamelModel):
    """`GET /settings/finding-sla`（任务 10 P3）。

    **0 = 该档不设时限**（不是"立刻到期"）——见 `FindingSlaConfig` 的说明。
    """

    criticalDays: int
    highDays: int
    mediumDays: int
    lowDays: int
    infoDays: int
    # 'discovered' | 'confirmed'
    startFrom: str
    updatedAt: IsoDateTime


class FindingSlaIn(StrictModel):
    """`PUT /settings/finding-sla`。

    ⚠️ 全部字段**必填**（同 `ScanRetentionIn` 的理由）：配置类端点漏传
    一项是调用方 bug，而给默认值会让它静默改回默认 —— 用户以为只改了一项。
    """

    criticalDays: int = Field(ge=0, le=3650)
    highDays: int = Field(ge=0, le=3650)
    mediumDays: int = Field(ge=0, le=3650)
    lowDays: int = Field(ge=0, le=3650)
    infoDays: int = Field(ge=0, le=3650)
    startFrom: Literal["discovered", "confirmed"] = "discovered"


class ChangePasswordRequest(StrictModel):
    """`POST /settings/password`。

    `confirm` 是可选的：前端的 `validatePasswordChange(current, next, confirm)`
    已经在客户端比对过两次输入，服务端再要一遍只是重复。
    但如果调用方传了，就顺手校验 —— 幂等的宽容比"少一个字段就 400"好。
    """

    current: str
    next: str = Field(min_length=1)
    confirm: str | None = None


# ══════════════════════════════════════════════════════════
# 沙箱与执行环境（任务 13）
# ══════════════════════════════════════════════════════════


class ExecutionSourceOut(CamelModel):
    """执行类三件套**当前值的来源**。

    ★ 为什么不省掉它：env 里可能已经有一个非默认值（例如部署时设了
    `SUPEX_MAX_CONCURRENT_SCANS=3`）。不标来源的话，用户在设置页看到
    "3" 会以为是自己以前设的，改完再改回去时也不知道该改回什么。
    标出"这是环境默认值"能让这件事有个确定答案。
    """

    maxConcurrentScans: Literal["user", "default"]
    scanMaxTurns: Literal["user", "default"]
    scanMode: Literal["user", "default"]


class SandboxOut(CamelModel):
    """`GET /settings/sandbox` —— 也是 `PUT` 的响应。

    `status` 是**只读的运行时状态**，不在这里保存（它由 `health.py`
    按 Docker 守护进程的真实情况算出来）。把它塞进请求体会让"用户提交了
    一个状态"这件事看起来成立，而实际上状态不是用户能设的东西。
    """

    engine: str
    status: str
    image: str
    cpuLimit: int
    memoryLimitMb: int
    timeoutMinutes: int
    networkIsolated: bool
    allowVolumeWrite: bool
    # ── 执行类三件套（任务 13 新增）──────────────────────
    maxConcurrentScans: int
    scanMaxTurns: int
    scanMode: str
    executionSource: ExecutionSourceOut
    updatedAt: IsoDateTime


class SandboxIn(StrictModel):
    """`PUT /settings/sandbox`。

    ⚠️ **不含 `engine` / `status` / `updatedAt`**：
    `engine` 目前只有 docker 一种取值，`status` 是运行时事实，
    `updatedAt` 由服务端写。让它们出现在请求体里，等于让调用方能提交
    "我认为沙箱正在运行" —— 而那是可以撒谎的字段。

    ⚠️ 字段**全部必填**（同 `ScanRetentionIn` 的理由）：配置类端点漏传一项
    是调用方 bug，而给默认值会让它静默改回默认，用户以为只改了一项。
    """

    image: str = Field(min_length=1, max_length=200)
    cpuLimit: int = Field(ge=MIN_CPU_LIMIT)
    memoryLimitMb: int = Field(ge=MIN_MEMORY_LIMIT_MB)
    timeoutMinutes: int = Field(ge=MIN_TIMEOUT_MINUTES)
    networkIsolated: bool
    allowVolumeWrite: bool
    maxConcurrentScans: int = Field(ge=MIN_CONCURRENT_SCANS, le=MAX_CONCURRENT_SCANS)
    scanMaxTurns: int = Field(ge=MIN_SCAN_MAX_TURNS, le=MAX_SCAN_MAX_TURNS)
    scanMode: str


# ══════════════════════════════════════════════════════════
# 通知（v0.22，任务 7 的最后一块）
# ══════════════════════════════════════════════════════════
#
# ## 这两个形状是**分离的**，不是"一个配置对象拆成两块"
#
# `NoticeRule`（什么情况下发）与 `NotifyChannel`（怎么发）是两个维度。
# 把「Slack 地址」和「只通知严重漏洞」放进同一个对象，会导致
# 「我想让邮件收到所有级别、Slack 只收严重」这种真实需求**无处表达**。
#
# 所以：规则里没有地址，渠道里没有阈值。契约 §2.9 有同样的说明。


class NoticeRuleOut(CamelModel):
    """`GET/PUT /settings/notices/rule`。"""

    minSeverity: str
    notifyOnScanComplete: bool
    notifyOnCriticalImmediate: bool
    updatedAt: IsoDateTime


class NoticeRuleIn(StrictModel):
    """`PUT /settings/notices/rule`。

    ⚠️ 三个字段**全部必填**（与 `ScanRetentionIn` / `SandboxIn` 同一条理由）：
    配置类端点漏传一项是调用方 bug，而给默认值会让它静默改回默认 ——
    用户以为自己只改了一项，实际另一项被重置了。
    """

    minSeverity: str
    notifyOnScanComplete: bool
    notifyOnCriticalImmediate: bool


class NotifyChannelOut(CamelModel):
    """一个通知渠道。

    ⚠️ **没有 `target` 之外的敏感字段** —— 但 `target` 本身是敏感的
    （Slack Webhook URL 就是一把凭据：拿到它就能往那个群发消息）。
    所以它只在 `admin` 级端点上出现（`require_admin`），
    与 provider 的 `apiKeyMask` 同一条思路的**较弱版本**：
    这里没做掩码，因为用户需要看到并编辑完整地址才能改它 ——
    而掩码会让"我只想改个错别字"变成"必须重新粘贴整条 URL"。
    """

    id: str
    kind: str
    label: str
    target: str
    enabled: bool
    verified: bool
    verifyError: str | None
    createdAt: IsoDateTime
    updatedAt: IsoDateTime


class NotifyChannelIn(StrictModel):
    """`POST /settings/notices/channels`（新建）。

    `id` 由**服务端**分配 —— 与 `POST /providers` 同一条规则：
    让客户端指定主键等于让它决定"覆盖哪一条"。
    """

    kind: str
    label: str = Field(min_length=1, max_length=120)
    target: str = Field(min_length=1, max_length=2000)
    enabled: bool = True


class NotifyChannelPatch(StrictModel):
    """`PATCH /settings/notices/channels/{id}`（部分更新）。

    ⚠️ 这里是**唯一允许部分更新**的配置端点，与上面几个"全必填"的不同。
    理由：渠道是**列表里的一条**，而前端改一行时只提交改动的那几个字段
    是自然的；而规则是**单例**，改它就是"这份配置现在长这样"。
    """

    label: str | None = Field(default=None, min_length=1, max_length=120)
    target: str | None = Field(default=None, min_length=1, max_length=2000)
    enabled: bool | None = None


class NoticesOut(CamelModel):
    """通知的两块 —— 规则与渠道。

    它们**打包在一起**返回（而不是两个端点各返回一块），因为
    `GET /settings` 的契约是"一次返回四块快照"，而通知是其中一块。
    单独读它们仍然走 `/settings/notices/rule` 与
    `/settings/notices/channels`。
    """

    rule: NoticeRuleOut
    channels: list[NotifyChannelOut]


class SettingsSnapshotOut(CamelModel):
    """`GET /settings` —— 设置页一次拉全。

    ⚠️ **不含 `status` / `error`**：前端 `SettingsSnapshot` 上那两个字段
    是**本地加载状态**，描述的是"这次请求进行得怎么样"，不是服务端数据。
    服务端下发它们等于替前端声明它自己的状态机。
    """

    accounts: list[ProviderOut]
    sandbox: SandboxOut
    notices: NoticesOut
    tokens: list[ApiTokenOut]
