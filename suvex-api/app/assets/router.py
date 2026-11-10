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
from app.assets.schemas import (
    AssetListOut,
    AssetOut,
    AssetSummaryOut,
    CoverageOut,
    HoneypotScoreIn,
    HoneypotScoreOut,
    HoneypotSignatureOut,
    SurfaceMapOut,
)
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


@router.get(
    "/assets/coverage",
    response_model=CoverageOut,
    summary="一个目标的覆盖度快照（测过什么 / 排除了什么）",
)
def asset_coverage(
    _identity: ReadDep,
    db: DbDep,
    target: Annotated[
        str,
        Query(min_length=1, max_length=512, description="扫描目标（与 /assets 同一口径）"),
    ],
) -> CoverageOut:
    """返回同一目标历史上 `record_coverage` 与 `ruled-out` 的记录。

    只列**有记录**的面；没有记录不等于没测过，也不等于有缺口 —— 它是
    "没有认知"。界面不要把两者混为一谈（见 `CoverageOut` 的说明）。
    """
    return CoverageOut.model_validate(service.coverage_for_target(db, target=target))


@router.get(
    "/assets/surface",
    response_model=SurfaceMapOut,
    summary="一个目标的黑板事实图（nodes + edges）",
)
def asset_surface(
    _identity: ReadDep,
    db: DbDep,
    target: Annotated[str, Query(min_length=1, max_length=512)],
) -> SurfaceMapOut:
    """把同一目标的 `scan_facts` 整理成图：事实是节点，`edges` 是关系。

    只保留两端都在结果里的边 —— 悬空边会让前端画出一个不存在的节点。
    """
    from app.facts import service as facts_service

    return SurfaceMapOut.model_validate(facts_service.surface_map(db, target=target))


# -- 蜜罐 / 反 AI 陷阱静态信号（v1.82）----------------------


@router.get(
    "/assets/honeypot/signatures",
    response_model=list[HoneypotSignatureOut],
    summary="蜜罐静态签名库",
)
def honeypot_signatures(_identity: ReadDep) -> list[HoneypotSignatureOut]:
    from app.assets import honeypot

    return [HoneypotSignatureOut.model_validate(item) for item in honeypot.list_signatures()]


@router.post(
    "/assets/honeypot/score",
    response_model=HoneypotScoreOut,
    summary="对 banner/title/响应做蜜罐打分",
)
def honeypot_score(payload: HoneypotScoreIn, _identity: ReadDep) -> HoneypotScoreOut:
    """纯计算，不发包、不写库。"""
    from app.assets import honeypot

    return HoneypotScoreOut.model_validate(
        honeypot.score(
            banner=payload.banner,
            title=payload.title,
            headers=payload.headers,
            body=payload.body,
        )
    )
