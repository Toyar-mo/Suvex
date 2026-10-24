"""`assets` 领域的出参（v0.85）。

字段名与前端 `data/assets.ts` 的 `Asset` 逐字一致 —— 前端接线时不该
需要改任何类型。

## `lastScanAt` 与 `lastCompletedAt` 为什么是两个字段

契约里本来只想要一个"最近扫描时间"。但那个字段有歧义：

* 取"最近一次成功" → 连续失败 3 次的目标会显示"上周扫的"，而它**今天
  刚被扫过 3 次**，只是都没跑完。用户会以为"这机器一周没人管"。
* 取"最近一次任何状态" → 一个从未成功过的目标会显示"今天扫过"，
  而它其实**从来没被真正检查过**，这比前者更危险。

所以两个都给：`lastScanAt` 是"动作发生过没有"，`lastCompletedAt` 是
"上次真正拿到结果是什么时候"。两者相差很远本身就是信息 ——
再加一个 `consecutiveFailures` 让"差距有多大"变成一个能排序的数。
"""

from __future__ import annotations

from app.core.schemas import CamelModel
from app.core.timeutil import IsoDateTime


class AssetOut(CamelModel):
    """一个目标的聚合视图。"""

    #: 归一化后的目标（与 `scans.target` 同一口径）。
    target: str

    # ── 扫描 ────────────────────────────────────────────────
    scanCount: int
    #: 最近一次扫描的**发生时刻**（任何状态）。
    lastScanAt: IsoDateTime | None = None
    #: 最近一次扫描的终态/运行态；从没扫过时是 `null`。
    lastScanStatus: str | None = None
    #: 最近一次**成功**（`completed`）的时刻；从没成功过时是 `null`。
    lastCompletedAt: IsoDateTime | None = None
    #: 从最近一次往前数，连续失败了几次（遇到非失败即停）。
    consecutiveFailures: int = 0

    # ── 漏洞 ────────────────────────────────────────────────
    #: 该目标历史全部发现（含已修复/误报）。
    findingCount: int = 0
    #: **仍然开着**的发现数（排除 `fixed` / `closed` / `false_positive`）。
    openFindingCount: int = 0
    #: 开着的 critical / high 条数 —— 排序和"要不要标红"都用它。
    openCritical: int = 0
    openHigh: int = 0
    #: 按严重度分档的总数（含已终结的）—— 供详情页画图。
    bySeverity: dict[str, int] = {}


class AssetSummaryOut(CamelModel):
    """顶部统计条。

    ⚠️ `targetsWithOpenHigh` 是**资产个数**，不是漏洞条数。
    "高危漏洞有几条"在 `/findings/stats`，两个数不能互相冒充。
    """

    targetCount: int
    findingCount: int
    openFindingCount: int
    targetsWithOpenHigh: int
    #: 只有 findings、一次扫描都没有的目标数（历史导入造成的）。
    neverScanned: int = 0
    #: 有扫描但**一次都没成功过**的目标数 —— 它们"看起来干净"是因为没被检查过。
    neverCompleted: int = 0
    generatedAt: IsoDateTime


class AssetListOut(CamelModel):
    items: list[AssetOut]
    total: int
    #: 目标数超过上限时为 `true` —— 界面必须显示出来，
    #: 否则用户会把"只返回了前 500 个"当成"总共就 500 个"。
    truncated: bool
    summary: AssetSummaryOut
