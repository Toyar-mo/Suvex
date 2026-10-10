"""`findings` 领域的出入参（契约 §2.2 / §3.4）。

字段名逐字对齐前端 `data/finding.ts` 的 `Finding` 与 `FindingDetail`。
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from app.core.schemas import CamelModel, StrictModel
from app.core.timeutil import IsoDateTime
from app.core.vocab import Severity

# ⚠️ 必须与 `core.vocab.FindingStatus` 的**全部**取值一致（任务 10 P0 补充）。
#
# ★ 这里曾经漏过：v0.14 把状态从 4 个扩到 6 个（新增 `retesting` / `closed`）
#   时改了 `vocab.FindingStatus` 与流转表，但**漏了这个 Literal** ——
#   而它是 `POST /findings/status` 的请求体类型，于是「提交复测」「关闭」
#   这两个按钮点下去会被 pydantic 拦成 422，错误消息只有一句
#   "Input should be 'new', 'confirmed', 'fixed' or 'false_positive'"。
#
#   漏掉的原因是它长得不像"状态机的定义"（它在 schemas 里、是个类型别名），
#   而流转表的对账脚本只比对了 `vocab` 与前端 `finding.ts` 两处。
#   教训：状态这种跨层枚举，**唯一事实来源应该是 vocab** ——
#   这里的字面量是"抄了一份"，抄的东西必然会漂移。
StatusLiteral = Literal[
    "new", "confirmed", "fixed", "retesting", "closed", "false_positive"
]

# SLA 到期状态（任务 10 P3）。由**后端**判定，见 `FindingSummaryOut` 的说明。
#   · `none`    —— 不计时（该档 0 天，或已终结）
#   · `onTrack` —— 还早
#   · `dueSoon` —— 剩余 ≤ SLA_WARN_WITHIN_DAYS
#   · `overdue` —— 已超期
SlaStateLiteral = Literal["none", "onTrack", "dueSoon", "overdue"]


class PocOut(CamelModel):
    """`detail.poc`。**两列都空时整体为 null**，不是空对象。

    `poc: null` = 没有脚本；`{language, code}` = 有。
    这个区分在契约 §2.2 里写明了，而"空对象"会把两者混成一个。
    """

    language: str
    code: str


class FindingDetailOut(CamelModel):
    """`FindingDetail`（契约 §2.2）。

    字段名与前端逐字一致。注意这里**没有** `assumptions` /
    `counterevidence` / `cvss` 那些 strix 侧的字段 —— 它们被折进了
    `reasoning`（带小标题），理由见 `strix_vulns.py` 的模块 docstring：
    折进去保住信息，而加字段是前端类型变更。
    """

    description: str
    affected: list[str]
    # ⚠️ 「为什么判断这里有问题」—— 不是 `description` 的同义反复。
    reasoning: str
    # 真实响应片段，**原样保留不翻译**。
    evidence: str
    poc: PocOut | None
    remediation: str | None


class TimelineEntryOut(CamelModel):
    """`FindingTimelineEntry`（契约 §2.2）。

    `from` 是 Python 关键字，所以 Python 侧叫 `from_`，
    但**序列化出去必须是 `from`** —— 契约的字段名不能改。
    """

    id: str
    at: IsoDateTime
    actor: str
    kind: str
    summary: str
    # ⚠️ 用 `serialization_alias` 而不是 `alias`：
    # alias 生成器只设了 `validation_alias`（把 camelCase 映到 snake_case 属性），
    # 序列化时用的是字段名。所以这里要显式指定输出名。
    from_: str | None = Field(default=None, serialization_alias="from")
    to: str | None = None


class FindingOut(CamelModel):
    """一条漏洞（契约 §2.2）。

    ⚠️ **`verification` 与 `status` 是两个独立的轴**，绝不合并：
    一条漏洞可以是「已用 PoC 证明存在（verified），但判定不影响业务
    （false_positive）」。合成一个字段，这个渗透测试里最常见的争议点
    就无处表达 —— 所以表结构上也是两列。
    """

    id: str
    scanId: str
    severity: str
    category: str
    title: str
    target: str
    # 轴一：证据强度（agent 产出，人不改它）
    verification: str
    # 轴二：处置状态（人工流转，agent 不改它）
    status: str
    discoveredAt: IsoDateTime
    detail: FindingDetailOut
    # **倒序**（最新在前），契约 §2.2 明确要求
    timeline: list[TimelineEntryOut]


class FindingSummaryOut(CamelModel):
    """列表项。**不含 `detail` 与 `timeline`** —— 那是详情页才要的东西。

    列表页一次可能拉几百条，每条都带上完整证据文本与时间线，
    响应会有几 MB，而列表根本不渲染它们。
    """

    id: str
    scanId: str
    severity: str
    category: str
    title: str
    target: str
    verification: str
    status: str
    discoveredAt: IsoDateTime
    # ── 指派（任务 10 P2）────────────────────────────────
    #
    # 两个都带：`assigneeId` 给筛选与后续操作，`assigneeName` 给显示。
    # **不让前端拿 id 再去查名单** —— 那会让列表渲染依赖第二个请求，
    # 而中间那一刻负责人那一列是空的（看起来像"没人负责"）。
    # 名字在服务端拼进摘要，列表一次请求就够。
    assigneeId: str | None = None
    assigneeName: str | None = None
    # ── SLA（任务 10 P3）──────────────────────────────────
    #
    # ★ 到期**状态**也由后端给（`overdue` / `dueSoon` / `onTrack`），
    # 而不是让前端拿 `dueAt` 和 `Date.now()` 自己比。
    #
    # 理由与"角标读 stats 而不是本地数一遍"是同一条：两处独立判断
    # 会取到不同时刻的快照，而"这条到底超期没有"在列表刷新前后
    # 给出不同答案是很难解释的。而且前端算的话，"临期"的判定窗口
    # （3 天）就要在前后端各写一份。
    #
    # `dueAt` 为 null = 这个档位不设时限，或这条已终结（见模型说明）。
    dueAt: IsoDateTime | None = None
    slaState: SlaStateLiteral = "none"
    # 剩余天数（负 = 已超期）。界面上那个倒计时标签直接用它 ——
    # 让前端从 dueAt 再算一次天数，等于把同一个判断做了两遍。
    slaDaysLeft: int | None = None
    # ── 回收站（任务 10 P6-a）────────────────────────────
    #
    # 非空 = 在回收站。**与 `Scan.archivedAt` / `Scan.deletedAt` 同构**
    # （契约 §2.1）：用时间戳不用布尔 —— 布尔丢掉"什么时候删的"，
    # 而回收站要按时间算到期、界面也要能说"3 天前删的"。
    #
    # 它只在 `?deleted=true` 的响应里非空（列表默认排除回收站条目）。
    deletedAt: IsoDateTime | None = None


class AssignRequest(StrictModel):
    """`POST /findings/assign` —— 批量指派（契约 §3.4）。

    `assigneeId=None` 是**明确的操作**（取消指派），不是"没传" ——
    所以它是必填字段、可以为 null，而不是可选字段。
    这样"漏传"与"故意清空"在请求里长得不一样，前端不会误清负责人。
    """

    ids: list[str] = Field(min_length=1, max_length=500)
    assigneeId: str | None = None

    @field_validator("ids")
    @classmethod
    def _dedupe(cls, value: list[str]) -> list[str]:
        # 去重保序：重复的 id 会让 `changed` 计数翻倍，
        # 而用户看到的"改了 20 条"其实只改了 10 条。
        seen: list[str] = []
        for item in value:
            if item not in seen:
                seen.append(item)
        return seen


class AssignResponse(CamelModel):
    changed: int
    skipped: int
    findings: list[FindingSummaryOut]


class FindingGroupOut(CamelModel):
    """按标题分组的一组（任务 10 P4）。

    ★ 带 `findingIds` 而不只是计数：展开这一组时前端要用它们去筛列表，
    或者直接对整组做批量操作（"批量确认同一类问题的所有实例"）。
    不带的话，展开就得再发一次请求按标题查 —— 而标题是**可以重复**的
    （两条不同的漏洞恰好同名），用标题当查询条件会多带出不属于这组的条目。
    """

    title: str
    # 组内**最高**严重度 —— 取平均或取第一条会把"这组里有个 critical"藏起来
    severity: str
    category: str
    instanceCount: int
    # 影响面：分组视图存在的理由就是它
    targetCount: int
    targets: list[str]
    findingIds: list[str]
    byStatus: dict[str, int]


# ══════════════════════════════════════════════════════════
# 附件（任务 10 P6-d）
# ══════════════════════════════════════════════════════════


class AttachmentOut(CamelModel):
    """一个附件的元信息（契约 §3.4）。

    ⚠️ **不含存储路径**。`stored_path` 是服务端内部的事 ——
    暴露它等于把文件系统的布局告诉客户端，而客户端**没有任何用途**
    需要知道它（要下载就走 `/attachments/{id}/content`，
    那个端点自己做鉴权与路径校验）。
    """

    id: str
    findingId: str
    # 用户上传时的原始文件名（**已消毒**：去掉了路径与控制字符）
    filename: str
    contentType: str
    sizeBytes: int
    uploadedAt: IsoDateTime
    uploadedBy: str | None = None


class FindingDeletedResponse(CamelModel):
    """批量软删除 / 恢复的响应 —— 与扫描模块**同形**（`BatchResultOut`）。

    `changed` 与 `skipped` 都要给：前端用它提示"实际处理了几条、
    跳过几条"。只给 `changed` 的话，"我选了 10 条、其中 3 条本来就在
    回收站里"这件事无处表达，而用户会以为自己点了 10 次只有 7 次生效。
    """

    changed: int
    skipped: int
    findings: list[FindingSummaryOut]


class BatchIdsRequest(StrictModel):
    """只有一组 id 的批量请求（软删除 / 恢复共用）。

    ⚠️ 与扫描模块的 `BatchIdsIn` **同形但不同名** —— 两个领域各自的
    schema 归各自管。共用的话，将来漏洞这边要加一个参数（比如
    "删除时同时标记误报"）就会牵动扫描那边。形状相同不等于同一个东西。
    """

    ids: list[str] = Field(min_length=1, max_length=500)

    @field_validator("ids")
    @classmethod
    def _dedupe(cls, value: list[str]) -> list[str]:
        # 去重保序，与 `AssignRequest` 同一个理由：
        # 重复的 id 会让 `changed` 计数翻倍。
        seen: list[str] = []
        for item in value:
            if item not in seen:
                seen.append(item)
        return seen


# ══════════════════════════════════════════════════════════
# 请求
# ══════════════════════════════════════════════════════════


class StatusChangeRequest(StrictModel):
    """`POST /findings/status` —— **批量**流转（契约 §3.4）。

    为什么是批量而不是逐条 `PATCH /findings/{id}`：
    前端是全选后一次提交（`BulkActionBar`），逐条 PATCH 会发出 N 个请求。
    而且响应需要**聚合结果**（改了几条、跳过几条）。
    """

    ids: list[str] = Field(min_length=1, max_length=500)
    status: StatusLiteral
    # 可选：覆盖默认的流转摘要文案
    note: str | None = Field(default=None, max_length=500)

    @field_validator("ids")
    @classmethod
    def _dedupe(cls, value: list[str]) -> list[str]:
        # 去重保序：重复的 id 会让 `changed` 计数翻倍，
        # 而前端用它显示"实际改了几条"。
        seen: list[str] = []
        for item in value:
            if item not in seen:
                seen.append(item)
        return seen


class FindignStatusResponse(CamelModel):
    """批量流转的响应，与前端 `setFindingStatus` 的返回值对齐。

    ⚠️ **`skipped` 必须返回**：前端会用它提示"实际改了几条、跳过几条"。
    """

    changed: int
    skipped: int
    # 仅含**真正改了**的那些（契约 §3.4）
    findings: list[FindingSummaryOut]


class SeverityCounts(CamelModel):
    """按严重度的计数。

    ⚠️ 这是 `Partial<Record<Severity, number>>`：**某个级别为 0 时该键不存在**，
    不是 0（契约 §2.1 的同一条规则）。前端用 `counts[sev] ?? 0` 读取，
    两种都能处理，但契约上以"不存在"为准 —— 保持和扫描列表同一个口径。
    """

    critical: int | None = None
    high: int | None = None
    medium: int | None = None
    low: int | None = None
    info: int | None = None


class FindingStatsOut(CamelModel):
    """`GET /findings/stats`（契约 §3.4）。

    ★ **这个接口同时服务 Findings 页与 Overview 页**，所以数字必须
    来自同一处查询逻辑 —— 见 `service.stats` 的说明。
    两页对不上的话，用户会以为其中一个是错的（而那种不信任会扩散到
    整个产品）。
    """

    total: int
    bySeverity: SeverityCounts
    byStatus: dict[str, int]
    byCategory: dict[str, int]
    # ⚠️ **必须单独返回**：Overview 的指标卡排除了误报，界面上有一句
    # 「上面两张指标卡已排除 N 条被判为误报的条目」。前端注释：
    # **"排除是可以的，悄悄排除不行。"**
    falsePositiveCount: int


def severity_counts_from(rows: list[tuple[str, int]]) -> SeverityCounts:
    """`[(severity, count)]` → `SeverityCounts`，**省略为 0 的档**。

    集中在这里而不是各调用点自己拼：`Partial` 语义只有一处实现，
    才不会出现"列表页省略、统计页填 0"这种两处口径不一致。
    """
    table = dict(rows)
    payload = {member.value: table.get(member.value) for member in Severity}
    return SeverityCounts(**{key: value for key, value in payload.items() if value})


__all__ = [
    "FindingDetailOut",
    "FindingOut",
    "FindingStatsOut",
    "FindingSummaryOut",
    "FindignStatusResponse",
    "PocOut",
    "SeverityCounts",
    "StatusChangeRequest",
    "TimelineEntryOut",
]
