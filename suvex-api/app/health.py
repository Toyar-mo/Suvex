"""健康检查与指标（契约 §3.8）。

| 方法 | 路径               | 鉴权   |
| ---- | ------------------ | ------ |
| GET  | `/health`          | public |
| GET  | `/health/sandbox`  | public |
| GET  | `/metrics`         | public |

⚠️ **都是 public**。这不是疏忽：调用方是容器编排器、监控与
Prometheus 抓取器，它们没有凭据。所以它们**不能泄露内部信息** ——

* `/health` 只回 `status` 与版本号。**不回数据库地址、不回延迟、不回表数量。**
* `/health/sandbox` 回沙箱状态与镜像名。镜像名算不算敏感？
  它只是本平台自己的镜像 tag（`suvex/sandbox:1.4`），不涉及用户资产 ——
  而它正是 Settings 页面要显示的东西，且该页面本身要登录。
  这里放行是因为容器的 healthcheck 需要在没有凭据的情况下判断沙箱是否可用。
* `/metrics` 只回**数字**（请求计数、耗时总和、扫描状态分布），
  不含任何业务数据内容 —— 见 `core/metrics.py` 的说明。

`status` 的取值：`ok` | `degraded`。**用 `degraded` 而不是直接 503** ——
编排器看到 503 会重启容器，而"数据库连不上"重启应用是没用的，
它只会让重启循环掩盖真正的错误。所以 HTTP 状态码保持 200 表示
"进程本身活着"，body 里的 status 才表达依赖的状态。
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import metrics
from app.core.config import get_settings
from app.core.db import get_db, ping
from app.scans.models import Scan
from app.settings.models import SINGLETON_ID, SandboxConfig

logger = logging.getLogger("suvex.health")

router = APIRouter(tags=["健康检查"])


class HealthOut(BaseModel):
    status: str
    version: str


class SandboxHealthOut(BaseModel):
    status: str
    image: str


@router.get("/health", response_model=HealthOut, summary="健康检查")
def health() -> HealthOut:
    settings = get_settings()
    return HealthOut(
        status="ok" if ping() else "degraded",
        version=settings.app_version,
    )


@router.get("/health/sandbox", response_model=SandboxHealthOut, summary="沙箱状态")
def sandbox_health(db: Annotated[Session, Depends(get_db)]) -> SandboxHealthOut:
    """对应 Settings 里 `SandboxConfig.status` 的展示。

    读的是数据库里那一行，不是去问 Docker —— **本后端目前不管理容器**
    （契约 §3.5.1 说明沙箱是后续任务的范围）。所以这里返回的是
    "配置里记录的状态"，与"沙箱进程实际在不在跑"可能不一致。

    这一点必须写清楚，否则界面会宣称沙箱在跑而实际没有 ——
    那正是 `settings/models.py` 把初始值从 `running` 改成 `stopped` 的原因
    （状态语义不能撒谎）。等真正接上容器运行时，这个方法改成去问它即可，
    调用方不用动。
    """
    config = db.get(SandboxConfig, SINGLETON_ID)
    if config is None:
        # 单例行还没建（迁移跑过但初始化没跑）。返回 stopped 而不是 404：
        # 容器 healthcheck 不该因为数据库里少一行种子数据就失败。
        return SandboxHealthOut(status="stopped", image="")
    return SandboxHealthOut(status=config.status, image=config.image)


@router.get("/metrics", summary="Prometheus 指标")
def prometheus_metrics(db: Annotated[Session, Depends(get_db)]) -> Response:
    """Prometheus 文本格式的指标（v0.23）。

    ## 为什么即时值在这里现算，而不是维护进程内计数器

    "当前有多少次扫描处于某个状态"这类值有两个来源：进程内维护一个
    计数器（每次状态变化 ±1），或者导出时**读一次库**。

    选了后者。理由：进程内计数器的正确性依赖"每一处状态变更都记得改它"，
    而漏一处的症状是数字**静默地**偏了 —— 没人会去核对它。
    读库虽然多一次查询（几毫秒，每 15 秒一次），但它**永远是对的**。

    这条判断与项目里其它几处一致：**宁可慢一点，也不要一个会撒谎的数字。**

    ## 数据库挂了怎么办

    `ping()` 为假时**不**查那些即时值，只回进程内的计数器 + 一个
    `suvex_database_up 0`。让指标端点因为数据库挂了而 500 是错的 ——
    那正是最需要看指标的时候。
    """
    settings = get_settings()

    if not settings.metrics_enabled:
        # 关掉时返回 404 而不是空指标：空指标会让 Prometheus 以为
        # "服务在跑但没有任何请求"，而真相是"这个端点被关掉了"。
        from app.core.errors import ApiError

        raise ApiError.not_found("指标端点")

    extra: dict[str, float] = {}

    if ping():
        extra["database_up"] = 1
        # 扫描的状态分布。按状态分组的 COUNT 走索引，且只回几行。
        rows = db.execute(select(Scan.status, func.count()).group_by(Scan.status)).all()
        for status, count in rows:
            # 标签值来自枚举（受控取值），不是用户输入 —— 没有标签爆炸风险
            extra[f'scans_by_status{{status="{status}"}}'] = float(count)
    else:
        extra["database_up"] = 0

    body = metrics.render(extra=extra)
    return Response(
        content=body, media_type="text/plain; version=0.0.4; charset=utf-8"
    )
