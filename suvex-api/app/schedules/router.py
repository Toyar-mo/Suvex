"""`schedules` 领域路由（v0.85）。

| 方法   | 路径                      | 鉴权 | 说明                       |
| ------ | ------------------------- | ---- | -------------------------- |
| GET    | `/schedules`              | read | 全部定时计划（带积压计数） |
| POST   | `/schedules`              | scan | 新建一条计划               |
| PATCH  | `/schedules/{id}`         | scan | 改名字 / 改间隔            |
| POST   | `/schedules/{id}/enabled` | scan | 启用 / 停用                |
| DELETE | `/schedules/{id}`         | scan | 删掉一条计划               |

## 鉴权为什么是「读用 read、写用 scan」

写操作（新建、改、启停）**本身不发包** —— 它们只是改一行配置。但它们是
「以后会定期对某个目标发包」的**授权动作**，所以门槛跟 `POST /scans` 一致：
`require_scan`。若只要求 `read`，一个只读账号就能排一条每天扫十次的任务，
而它自己一次扫描都发起不了 —— 那是一个明显的权限绕过。

## 为什么没有 `GET /schedules/{id}`

一条计划的全貌就是列表里的那一行（没有子资源、没有历史明细 ——
历史在 `lastScanId` 指向的那条扫描里）。给它单独一个端点等于让两处
维护同一个投影，而其中一处早晚会漏掉新加的字段。

## 授权声明：为什么是**存下来逐字校验**而不是只存一个布尔

定时扫描的授权发生在「建计划」那一刻，而发包发生在**未来每一次到点**。
如果只记一个 `confirmed=true`，那条记录回答不了"用户同意的是哪句话" ——
而契约 §5.2 要求逐字留痕的全部理由就是"防止前端顺手勾一个默认值"。
所以这里存原文，并在创建时按 `settings.scan_authorization_statement`
逐字比对（与 `POST /scans` 同一个口径、同一段校验代码思路）。
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.auth.deps import Identity, client_ip, require_read, require_scan
from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import ApiError
from app.core.timeutil import now_utc
from app.scans import service as scans_service
from app.scans import targets
from app.schedules import service
from app.schedules.schemas import (
    ScheduleCreateIn,
    ScheduleListOut,
    ScheduleOut,
    ScheduleToggleIn,
    ScheduleUpdateIn,
)

logger = logging.getLogger("suvex.schedules")

router = APIRouter(tags=["定时扫描"])

ReadDep = Annotated[Identity, Depends(require_read)]
ScanDep = Annotated[Identity, Depends(require_scan)]
DbDep = Annotated[Session, Depends(get_db)]


def _check_statement(statement: str) -> str:
    """逐字校验授权声明，返回要落库的那句话。

    ⚠️ 响应**不回显**用户输入（契约 §1.3 第 3 条）：错误只说期望的是什么。

    这条校验与 `scans/service.check_authorization` 是同一个判据，
    但那一份接受的是 `AuthorizationIn`（`confirmed` + `statement` 两个字段）。
    定时扫描没有"这一次的确认动作"，用户在界面上勾的就是 `authorizationStatement`
    一个字段，所以这里单独判一次而不是硬套那个签名 —— 判据（与设置里那句话
    逐字相等）只有一处定义，直接读 `get_settings()`。
    """
    expected = get_settings().scan_authorization_statement
    if statement.strip() != expected:
        raise ApiError.validation(
            "授权声明的措辞与约定不一致，请逐字确认后再提交。",
            {"authorizationStatement": f"应为「{expected}」"},
        )
    return expected


@router.get("/schedules", response_model=ScheduleListOut, summary="定时计划清单")
def list_schedules(_identity: ReadDep, db: DbDep) -> ScheduleListOut:
    """全部计划，按下次到点时间排序（停用的排在后面）。"""
    items = service.list_schedules(db)
    return ScheduleListOut(
        items=[ScheduleOut.model_validate(s) for s in items],
        total=len(items),
        # ★ `dueCount` 单独查一次，而不是从上面这个 list 里数。
        #
        # 列表里的 `nextRunAt` 是**原样返回**的存储值，而"到期"要用
        # `now_utc()` 的同一口径来比较；在 Python 里对它们做时区转换
        # 只会多一处可能出错的地方。让数据库回答"有几条到期了"最省事，
        # 而且这一条查询很便宜（走 `ix_scan_schedules_due`）。
        due_count=service.count_due(db),
    )


@router.post("/schedules", response_model=ScheduleOut, status_code=201, summary="新建定时计划")
def create_schedule(
    payload: ScheduleCreateIn,
    identity: ScanDep,
    db: DbDep,
    request: Request,
) -> ScheduleOut:
    """新建一条计划。

    ## 为什么这里要重新走一遍目标的归一化与校验

    `POST /scans` 也是这么做的（前端可以被绕过）。定时计划的目标会在
    **未来很多次**扫描里被复用，所以这一次输错，错的就不止一次 ——
    校验门槛只能是"和建扫描一样"。

    ## 为什么没有 reachability 预检

    预检是为了给**立刻要跑**的那次扫描挑协议（http 还是 https）。
    计划可能几小时后才跑，那时的连通性和现在没关系 ——
    现在探一次只会拿到一个会过期的结论，还可能把"目标此刻不通"
    变成一个假的创建失败。
    """
    normalized, check = targets.normalize_and_validate(payload.targetKind, payload.target)
    if not check.ok:
        raise ApiError.validation(
            check.error or "目标不合法",
            {"target": check.error or "目标不合法"},
        )

    # provider 必须存在。复用 `_require_provider` 而不是 `db.get(...)`：
    # 那条 400 的文案与 `POST /scans` 完全一致，用户在两处看到的
    # 是同一句话，不必怀疑自己遇到了另一个问题。
    provider = scans_service._require_provider(db, payload.providerId)  # noqa: SLF001

    statement = _check_statement(payload.authorizationStatement)
    moment = now_utc()

    schedule = service.create_schedule(
        db,
        name=payload.name,
        target=normalized,
        target_kind=payload.targetKind,
        goal=payload.goal,
        interval_minutes=payload.intervalMinutes,
        provider_id=payload.providerId,
        provider_label=provider.label,
        skills=payload.skills,
        brief=payload.brief,
        authorized_by=scans_service._authorized_by(identity),  # noqa: SLF001
        authorization_statement=statement,
        authorization_confirmed_at=moment,
        now=moment,
    )
    logger.info(
        "定时计划 %s 已建（目标 %s，每 %d 分钟）；授权来自 %s @ %s",
        schedule.id,
        normalized,
        schedule.interval_minutes,
        identity.describe(),
        client_ip(request),
    )
    return ScheduleOut.model_validate(schedule)


@router.patch("/schedules/{schedule_id}", response_model=ScheduleOut, summary="改名字 / 改间隔")
def update_schedule(
    schedule_id: str,
    payload: ScheduleUpdateIn,
    _identity: ScanDep,
    db: DbDep,
) -> ScheduleOut:
    """只允许改**名字**与**间隔**。

    目标 / provider / skills / brief 一律不许改 —— 理由见
    `service.update_schedule`：授权留痕绑定在建计划那一刻的参数上，
    允许改等于允许"把一条已授权的计划静默换成另一件事"。
    """
    try:
        schedule = service.update_schedule(
            db,
            schedule_id,
            name=payload.name,
            interval_minutes=payload.intervalMinutes,
        )
    except service.ScheduleError as exc:
        raise ApiError.not_found("这条定时计划") from exc
    return ScheduleOut.model_validate(schedule)


@router.post("/schedules/{schedule_id}/enabled", response_model=ScheduleOut, summary="启用 / 停用")
def toggle_schedule(
    schedule_id: str,
    payload: ScheduleToggleIn,
    _identity: ScanDep,
    db: DbDep,
) -> ScheduleOut:
    """启用或停用一条计划。

    ## 为什么单独一个端点，而不是 `PATCH` 里带 `enabled`

    停用会把 `nextRunAt` 清成 `null`（数据库 CHECK 约束要求如此），
    而"改个名字"不该有这种副作用。把启停做成**显式的动作**，
    前端的按钮和它做的事就是一一对应的；塞进 PATCH 里则会出现
    "我改了个名字，怎么下次扫描时间没了"。
    """
    try:
        schedule = service.set_enabled(db, schedule_id, payload.enabled)
    except service.ScheduleError as exc:
        raise ApiError.not_found("这条定时计划") from exc
    return ScheduleOut.model_validate(schedule)


@router.delete("/schedules/{schedule_id}", status_code=204, summary="删除一条计划")
def delete_schedule(schedule_id: str, _identity: ScanDep, db: DbDep) -> None:
    """删掉一条计划。

    已经由它建出来的扫描**不会跟着删** —— 那些是独立的记录，
    有自己的报告、漏洞和审计。删计划只停掉"以后还会自动建"这件事。
    """
    try:
        service.delete_schedule(db, schedule_id)
    except service.ScheduleError as exc:
        raise ApiError.not_found("这条定时计划") from exc
