"""健康检查（契约 §3.8）。

| 方法 | 路径               | 鉴权   |
| ---- | ------------------ | ------ |
| GET  | `/health`          | public |
| GET  | `/health/sandbox`  | public |

⚠️ **两个都是 public**。这不是疏忽：健康检查的调用方是容器编排器与监控，
它们没有凭据。所以它们**不能泄露内部信息** ——

* `/health` 只回 `status` 与版本号。**不回数据库地址、不回延迟、不回表数量。**
* `/health/sandbox` 回沙箱状态与镜像名。镜像名算不算敏感？
  它只是本平台自己的镜像 tag（`suvex/sandbox:1.4`），不涉及用户资产 ——
  而它正是 Settings 页面要显示的东西，且该页面本身要登录。
  这里放行是因为容器的 healthcheck 需要在没有凭据的情况下判断沙箱是否可用。

`status` 的取值：`ok` | `degraded`。**用 `degraded` 而不是直接 503** ——
编排器看到 503 会重启容器，而"数据库连不上"重启应用是没用的，
它只会让重启循环掩盖真正的错误。所以 HTTP 状态码保持 200 表示
"进程本身活着"，body 里的 status 才表达依赖的状态。
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db, ping
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
