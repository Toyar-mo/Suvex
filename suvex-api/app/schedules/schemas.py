"""定时扫描的出入参。

分层口径跟 `app/scans/schemas.py`、`app/assets/schemas.py` 完全一致：

* **字段名直接写 camelCase**（就是契约里的 wire 名字，前端逐字一致）；
* 继承 `CamelModel` 后 `validation_alias` 自动指向 ORM 的 snake_case 属性，
  于是 `ScheduleOut.model_validate(orm)` 能直接读，`response_model` 出去
  也是 camelCase —— 两个方向都不用写 alias；
* 请求体继承 `StrictModel`（多传字段直接 400），响应体继承 `CamelModel`。

⚠️ 别把字段名写成 snake_case。`CamelModel` 的 alias 是**单向**的
（`validation_alias=to_snake_case`），字段名写成 `interval_minutes` 会让
`model_validate(orm)` 读得到、但 `response_model` 出去也是
`interval_minutes` —— 前端拿不到 `intervalMinutes`，症状是"字段全是
undefined"，且不指向这里。

## 入参为什么这么"薄"

`ScheduleCreateIn` 里的字段**不是**可选项的集合，而是"建这条计划必须钉死的东西"：

* `target` / `targetKind` / `goal` —— 它要扫什么、扫到什么程度；
* `providerId` —— 谁来扫（模型/provider 直接决定花多少钱）；
* `authorizationStatement` —— 法律留痕，与 `POST /scans` 同一套契约。

一旦建成，后四样就**不可改**（见 `service.update_schedule` 的说明）。
这不是偷懒，是审计要求：允许改等于允许"把一条已授权的计划静默换成另一件事"。
"""

from __future__ import annotations

from pydantic import Field

from app.core.schemas import CamelModel, StrictModel
from app.core.timeutil import IsoDateTime
from app.schedules.models import MAX_INTERVAL_MINUTES, MIN_INTERVAL_MINUTES

_INTERVAL_DESC = f"间隔分钟数，{MIN_INTERVAL_MINUTES} 分钟到 {MAX_INTERVAL_MINUTES // (24 * 60)} 天"


class ScheduleCreateIn(StrictModel):
    """新建一条定时计划。"""

    name: str = Field(..., min_length=1, max_length=200, description="给这条计划起个名字")
    target: str = Field(..., min_length=1, max_length=512, description="要定期复查的目标")
    targetKind: str = Field("url", max_length=16, description="目标类型，与扫描一致")
    goal: str = Field(..., max_length=32, description="扫描目标类型（如 full/recon）")
    intervalMinutes: int = Field(
        ...,
        ge=MIN_INTERVAL_MINUTES,
        le=MAX_INTERVAL_MINUTES,
        description=_INTERVAL_DESC,
    )
    providerId: str = Field(..., min_length=1, max_length=64, description="用哪个 provider 来扫")
    skills: list[str] = Field(default_factory=list, description="启用的技能")
    brief: str | None = Field(None, description="给智能体的额外说明")

    # ── 授权留痕（与 POST /scans 同一套） ──
    authorizationStatement: str = Field(..., min_length=1, description="授权声明：我确认对该目标有测试授权")


class ScheduleUpdateIn(StrictModel):
    """改一条计划。

    只允许改**名字**和**间隔** —— 其余字段的缺席本身就是在表达"不许改"，
    比在接口文档里写一句"请勿修改目标"可靠。
    """

    name: str | None = Field(None, min_length=1, max_length=200)
    intervalMinutes: int | None = Field(None, ge=MIN_INTERVAL_MINUTES, le=MAX_INTERVAL_MINUTES)


class ScheduleToggleIn(StrictModel):
    enabled: bool = Field(..., description="true = 启用，false = 停用")


class ScheduleOut(CamelModel):
    """一条计划的完整视图。字段名与前端 `Schedule` 类型逐字一致。"""

    id: str
    name: str
    target: str
    targetKind: str
    goal: str
    intervalMinutes: int
    enabled: bool

    #: 下次到点时刻；停用时为 null（与数据库 CHECK 约束一致）。
    nextRunAt: IsoDateTime | None = None
    #: 上一轮建出的扫描 id。刻意**不是**外键 —— 扫描会被清理，
    #: 但计划不该因为扫描被删就"失去历史"。
    lastScanId: str | None = None
    lastRunAt: IsoDateTime | None = None

    #: 连续失败次数。**不会**自动停用计划，只是给界面一个"该看一眼了"的信号。
    failureCount: int = 0
    lastFailureReason: str | None = None

    providerId: str
    providerLabel: str | None = None
    skills: list[str] = Field(default_factory=list)
    brief: str | None = None

    createdAt: IsoDateTime
    updatedAt: IsoDateTime


class ScheduleListOut(CamelModel):
    items: list[ScheduleOut]
    total: int
    #: 还有多少条到点没跑 —— 界面用它提示"积压"。
    dueCount: int = 0
