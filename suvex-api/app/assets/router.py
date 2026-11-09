"""`assets` 领域路由（v0.85）。

| 方法 | 路径     | 鉴权 | 说明                             |
| ---- | -------- | ---- | -------------------------------- |
| GET  | `/assets` | read | 按目标聚合的资产清单（带统计条） |

**`read` 级**：纯读，不写、不发包、不发起扫描。

## 为什么一个端点拿全部，不做 `GET /assets/{target}`

三类调用方（列表页、详情页、仪表盘卡片）要的是**同一份聚合**的不同切面。
分成两个端点会让它们各自维护一份"怎么算未修复"的口径 —— 而那个口径
（排除哪些状态）是这个领域最容易漂移的地方，`service.TERMINAL_FINDING_STATUSES`
已经是单点定义，再复制一份到详情端点就等于把它打散了。

目标数上限 500（`service.MAX_ASSETS`）；超了 `truncated=true` 如实上报。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.assets import service
from app.assets.schemas import AssetListOut, AssetOut, AssetSummaryOut
from app.auth.deps import Identity, require_read
from app.core.db import get_db

router = APIRouter(tags=["资产"])

ReadDep = Annotated[Identity, Depends(require_read)]
DbDep = Annotated[Session, Depends(get_db)]


@router.get("/assets", response_model=AssetListOut, summary="按目标聚合的资产清单")
def list_assets(
    _identity: ReadDep,
    db: DbDep,
    limit: Annotated[
        int,
        Query(ge=1, le=service.MAX_ASSETS, description="最多返回多少个资产"),
    ] = service.MAX_ASSETS,
) -> AssetListOut:
    """资产视图（按风险排序：未修复 critical → high → 未修复总数 → 最近扫描）。"""
    assets, truncated, total = service.list_assets(db, limit=limit)
    # ★ v1.79：`total` 与 `summary` 都是**全量**口径。
    #
    # 原来 `total=len(assets)`、summary 也只算返回的这一页。截断时
    # 界面上的提示变成"只显示了前 500 个目标，共 500 个"——
    # 两个数字相等，而它想表达的是"还有更多"。统计卡也会把
    # "前 500 个里有多少"当成"全站有多少"。
    #
    # 全量口径下，卡片回答的是"这套资产现在的整体状况"，列表回答的是
    # "打开这一页能看到哪些"，`truncated` 与 `total` 一起说明差多少。
    return AssetListOut(
        items=[AssetOut.model_validate(a) for a in assets],
        total=total,
        truncated=truncated,
        summary=AssetSummaryOut.model_validate(service.summary_all(db)),
    )
