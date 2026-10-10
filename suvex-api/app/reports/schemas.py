"""`reports` 领域的出入参（契约 §2.7 / §3.5）。

## `ReportDocument` 的出参为什么用**宽松**模型

报告文档 IR 的**权威定义在前端**（`lib/report-document.ts` 的 TypeScript
interface），而且它会随前端演进。后端如果把它写成严格的 Pydantic 模型：

* 前端加一个字段 → 后端要同步改，否则那个字段被**静默丢掉**；
* 前端删一个字段 → 后端还在产出它，前端忽略，没人发现。

两种都是"文档与实现漂移"，而且不报错。所以 `ReportDocumentOut` 只声明
顶层形状（`meta` / `verdict` / `stats` / `distribution` / `entries` /
`method` / `severityDefinitions`），内层用 `dict[str, Any]`。

真正的一致性由**对账脚本**保证，不靠模型：
`scripts/verify_report_parity.py` 拿前端的 `buildReportDocument` 与
后端跑同一批数据、逐字段比对（契约 §3.5.1 要求"那个脚本应当改为对着 API 跑"）。

## 请求体用**严格**模型

反过来，请求体是后端定义的契约面，拼错的字段必须报错 ——
像 `includePoc` 写成 `includePOC` 会被静默忽略，
而用户以为"我勾了 PoC 但报告里没有"。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import ConfigDict, Field, model_validator

from app.core.schemas import CamelModel, StrictModel
from app.core.timeutil import IsoDateTime
from app.reports import service
from app.reports.document import DEFAULT_OPTIONS


class ReportOptionsOut(CamelModel):
    """与前端 `data/report.ts: ReportOptions` 逐字一致。"""

    includePoc: bool
    includeReasoning: bool
    onlyConfirmed: bool
    includeRemediation: bool


class ReportOptionsIn(StrictModel):
    """内容选项。

    四个都是**显式**的（无默认值以外的部分）——
    但给默认值让调用方可以只传关心的那些。默认值与
    `data/report.ts: DEFAULT_REPORT_OPTIONS` 一致：
    **三个开关默认关**（报告默认应该是"干净可交付"，而不是
    "把所有原始材料倒进去"），只有修复建议默认开。

    ⚠️ 尤其是 agent 推理过程：它内部是给工程师看的，
    混进管理层报告会显得杂乱。
    """

    includePoc: bool = DEFAULT_OPTIONS.include_poc
    includeReasoning: bool = DEFAULT_OPTIONS.include_reasoning
    onlyConfirmed: bool = DEFAULT_OPTIONS.only_confirmed
    includeRemediation: bool = DEFAULT_OPTIONS.include_remediation

    def to_domain(self):  # noqa: ANN201
        from app.reports.document import ReportOptions

        return ReportOptions(
            include_poc=self.includePoc,
            include_reasoning=self.includeReasoning,
            only_confirmed=self.onlyConfirmed,
            include_remediation=self.includeRemediation,
        )


class ReportScopeIn(StrictModel):
    """范围。**discriminated union**，不是"三个可选字段"。

    用三个可选字段的话，会出现 `scanId` 和 `target` 同时有值的非法状态，
    而"到底按哪个过滤"要靠读实现猜（契约 §2.7 的原话）。

    这里的 `model_validator` 是那道检查；数据库上还有一条 CHECK 约束
    （`ck_reports_scope_exactly_one_shape`）是第二道。
    """

    kind: Literal["scan", "target", "range", "findings"]
    scanId: str | None = None
    target: str | None = None
    # ★ 手选的那几条（任务 10 P5）。
    #
    # 与另外三种 scope 有一处结构差异：成员是**一串 id**，不是一列能装下的。
    # 所以 `reports` 表上没有对应列 —— 它们存进既有的 `report_findings`
    # 关联表（那本来就是"这份报告冻结了哪些漏洞"）。
    findingIds: list[str] | None = None
    # ══════════════════════════════════════════════════════════════
    # ★ `from` 这个键必须能被接受 —— 它是契约 §2.7 的字段名。
    #
    # `from` 是 Python 关键字，不能当属性名，所以字段叫 `from_`。
    # 但**进出参都必须是 `from`**。
    #
    # ⚠️ 第一版靠 `model_validator(mode="before")` 把 `from` 挪成 `from_`，
    # **那个做法是错的**：`StrictModel` 的 `extra="forbid"` 在那之前就
    # 判了「未识别的字段 scope.from」，整个区间报告用不了。
    # 测试当场抓到（`test_range_scope_accepts_the_from_key`）。
    #
    # 正确做法是让它成为**真正被知道的字段名**：给 `from_` 一个
    # `validation_alias`，于是校验器认识 `from`；序列化再用
    # `serialization_alias` 输出 `from`。两边对称。
    # ══════════════════════════════════════════════════════════════
    from_: IsoDateTime | None = Field(
        default=None, validation_alias="from", serialization_alias="from"
    )
    to: IsoDateTime | None = None

    # `StrictModel.model_config` 已经含 `populate_by_name`，
    # 再在 `ConfigDict(...)` 里传一次会 `TypeError: got multiple values`。
    # 所以用 `create_model` 式的合并**只覆盖要改的项**。
    model_config = ConfigDict(**{**StrictModel.model_config, "populate_by_name": True})

    @model_validator(mode="after")
    def _exactly_one_shape(self) -> ReportScopeIn:
        provided = {
            "scan": self.scanId is not None,
            "target": self.target is not None,
            "range": self.from_ is not None and self.to is not None,
            "findings": bool(self.findingIds),
        }
        if not provided[self.kind]:
            raise ValueError(f"kind={self.kind} 时缺少对应的字段")
        # 另外几组必须为空 —— 这条是 union 语义的关键
        others = {key: value for key, value in provided.items() if key != self.kind}
        if any(others.values()):
            raise ValueError("scope 只能给出一种范围，不能同时给多种")
        return self

    def to_domain(self) -> service.Scope:
        return service.Scope(
            kind=self.kind,
            scan_id=self.scanId,
            target=self.target,
            from_=self.from_,
            to=self.to,
            finding_ids=tuple(self.findingIds) if self.findingIds else None,
        )


class ReportGenerateRequest(StrictModel):
    """`POST /reports`（契约 §3.5）。"""

    title: str = Field(min_length=1, max_length=300)
    scope: ReportScopeIn
    options: ReportOptionsIn = ReportOptionsIn()
    format: Literal["pdf", "markdown"] = "markdown"


class ReportPreviewRequest(StrictModel):
    """`POST /reports/preview` —— 与生成同形，只是不落库、不分配编号。"""

    title: str = Field(min_length=1, max_length=300)
    scope: ReportScopeIn
    options: ReportOptionsIn = ReportOptionsIn()


class ReportOut(CamelModel):
    """`Report` —— 与前端 `data/report.ts` 的 `Report` 逐字一致。

    ⚠️ `excludedCount` 是**生成时冻结**的值，不重算（见 `service` 的说明）。
    ⚠️ `findingIds` 的**顺序是报告的一部分**（严重度降序）。
    """

    id: str
    title: str
    scope: dict[str, Any]
    options: ReportOptionsOut
    createdAt: IsoDateTime
    findingCount: int
    excludedCount: int
    # `Partial<Record<Severity, number>>`：为 0 的档键不存在
    severityCounts: dict[str, int]
    format: str
    findingIds: list[str]
    # 契约的 `Report` 类型里没有 `code`，但报告编号是**交付物的身份**，
    # 历史列表与正文里都要显示它。加上它是加法（前端可以忽略），
    # 而少了它前端就只能从 createdAt + id 现算 —— 那是第二处编号来源。
    code: str


class ReportDocumentOut(CamelModel):
    """`ReportDocument` IR 的**宽松**出参。

    只声明顶层形状，内层用 `dict` —— 理由见模块 docstring：
    IR 的权威定义在前端，锁死后端模型只会制造静默丢字段的漂移。

    `meta.code` 在预览时是**空串**（`provisional: true`），
    生成后是真实编号 —— 这是契约 §2.7 的硬要求。
    """

    meta: dict[str, Any]
    verdict: str
    stats: dict[str, Any]
    distribution: list[dict[str, Any]]
    entries: list[dict[str, Any]]
    method: dict[str, Any]
    severityDefinitions: list[dict[str, Any]]


__all__ = [
    "ReportDocumentOut",
    "ReportGenerateRequest",
    "ReportOptionsIn",
    "ReportOptionsOut",
    "ReportOut",
    "ReportPreviewRequest",
    "ReportScopeIn",
]
