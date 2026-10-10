"""`overview` 领域路由（契约 §3.6）。

| 方法 | 路径        | 鉴权 | 说明                     |
| ---- | ----------- | ---- | ------------------------ |
| GET  | `/overview` | read | 一次拿齐 Overview 页所需 |

**`read` 级**：它只读，不改变任何状态、不发起扫描、不发包。

## 为什么是"一次拿齐"而不是四个端点

契约 §3.6 定的是一个端点返回 `OverviewData` 的全部四块。理由很实际：
这个页面一进去就要同时渲染指标卡、图表和活动流，四个请求会让它们
**各自在稍有不同的时刻到达** —— 用户看到"进行中 2 次扫描"而图表还是旧的，
会以为是两个数据源。一次拿齐则天然是同一个快照。

⚠️ 实现时三个 null/空值的语义必须守住（契约 §3.6 用 ⚠️ 标了）：

1. `newHighSeverityDelta: null` = **没有上周数据**，界面显示"暂无对比数据"，
   **不能返回 0** —— 0 变化和无法比较是两回事。
2. `avgScanDurationSec: null` = 没有已完成扫描。**不能返回 0** ——
   0 秒会让用户以为扫描瞬间完成。
3. `severityBreakdown` 包含**全部发现**（与 Findings 页逐条对得上）；
   而 `metrics` 里只有"本周新增高危"和"已验证 PoC"排除误报。
   这个口径差异必须在界面上说明，所以 `falsePositiveCount` 要返回。
   前端注释原话：**"排除是可以的，悄悄排除不行。"**
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.auth.deps import Identity, require_read
from app.core.db import get_db
from app.overview import service
from app.overview.schemas import OverviewOut

router = APIRouter(tags=["全局统计"])

ReadDep = Annotated[Identity, Depends(require_read)]
DbDep = Annotated[Session, Depends(get_db)]


@router.get("/overview", response_model=OverviewOut, summary="Overview 页所需的全部数据")
def read_overview(
    _identity: ReadDep,
    db: DbDep,
    scanId: Annotated[  # noqa: N803
        str | None,
        Query(description="只统计这一次扫描；不传则统计全部"),
    ] = None,
) -> OverviewOut:
    """一次拿齐 Overview 页所需（契约 §3.6）。

    `?scanId=` 可选：`OverviewData` 的形状不变，只是把统计范围收窄到一次扫描。
    加它的理由是 §3.4.1 里 `stats()` 本来就支持 `scan_id`（任务 5 已经做了），
    这里顺带暴露出去 —— 否则那个参数永远只有测试在用。
    """
    if scanId is None:
        return service.build_overview(db)
    return service.build_overview(db, scan_id=scanId)
