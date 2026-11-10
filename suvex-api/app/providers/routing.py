"""按扫描判据选择 provider（v1.81）。

## 规则（顺序固定）

1. 调用方**显式**给了具体 provider → 用它；
2. `"auto"` / 空 → 查 `model_routes.routes[goal]`；
3. 没配路由 → 平台默认 provider（`is_default`）；
4. 都没有 → `None`（调用方报"先配置模型"）。

`auto` 是显式保留词而不是"空字符串 = auto"：前端/API 调用方写 `""`
可能是漏填，而 `"auto"` 明确表达"交给路由"。两者行为相同，但后者
在日志与契约里可读。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.providers.models import ProviderAccount
from app.settings.models import SINGLETON_ID, ModelRoutes


def route_for_goal(
    db: Session,
    goal: str,
    explicit: str | None = None,
    *,
    skills: list[str] | tuple[str, ...] | None = None,
) -> str | None:
    """解析这次扫描该用哪个 provider。"""
    value = (explicit or "").strip()
    if value and value != "auto":
        return value

    routes = db.get(ModelRoutes, SINGLETON_ID)
    table = dict(routes.routes or {}) if routes is not None else {}

    candidate = table.get(goal)
    if isinstance(candidate, str) and candidate.strip():
        return candidate.strip()

    # v1.82：按**扫描技能**路由（`skill:<id>`）。扫描没有显式的
    # recon/exploit 阶段模型，技能集是库里真实存在的维度。
    for skill in skills or ():
        candidate = table.get(f"skill:{skill}")
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()

    return db.execute(
        select(ProviderAccount.id).where(ProviderAccount.is_default).limit(1)
    ).scalar_one_or_none()


__all__ = ["route_for_goal"]
