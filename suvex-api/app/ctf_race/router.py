"""`ctf_race` 赛事自动托管端点（薄封装）。

## 端点

| 方法 | 路径 | scope | 说明 |
| --- | --- | --- | --- |
| POST | `/ctf-race/start` | **scan** | 起一场自动托管（后台跑，立即返回） |
| POST | `/ctf-race/stop` | **scan** | 请求停止（当前轮跑完 / 工具返回后停） |
| GET  | `/ctf-race/status` | read | 查进度 + 成本快照 |

起一场赛跑=**对外发包**（会真的连平台、起容器、消耗模型额度），与
`/ctf2/challenges/{id}/solve` 同级 → `scan`。查进度只读 → `read`。

同一时刻只允许一场（进程内单例，见 `manager.py` —— 与对话工具共用同一个）：
重复 start 返回 409。因为多场并发会同时开多个沙箱容器、烧多份额度，
且前端面板只显示一场，没有意义。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel, Field

from app.auth.deps import Identity, require_read, require_scan
from app.ctf_race.competition_racer import DEFAULT_RACE_MODELS
from app.ctf_race import manager

router = APIRouter(prefix="/ctf-race", tags=["CTF Race"])

ReadDep = Annotated[Identity, Depends(require_read)]
ScanDep = Annotated[Identity, Depends(require_scan)]


class RaceStartIn(BaseModel):
    models: list[str] = Field(default_factory=lambda: list(DEFAULT_RACE_MODELS))
    maxConcurrent: int = Field(default=2, ge=1, le=8)
    maxBudgetUsd: float | None = Field(default=None, ge=0)
    noSubmit: bool = False
    maxBumps: int = Field(default=3, ge=0, le=10)


class RaceStatusOut(BaseModel):
    running: bool
    models: list[str] = []
    challenges: dict = {}
    cost: dict = {}
    report: dict | None = None
    error: str | None = None


@router.post("/start", response_model=RaceStatusOut)
async def start_race(payload: RaceStartIn = Body(default_factory=RaceStartIn), _: ScanDep = None) -> RaceStatusOut:
    from app.core.errors import ApiError

    try:
        snap = await manager.start(
            models=tuple(payload.models) or DEFAULT_RACE_MODELS,
            max_concurrent=payload.maxConcurrent,
            max_budget_usd=payload.maxBudgetUsd,
            no_submit=payload.noSubmit,
            max_bumps=payload.maxBumps,
        )
    except manager.RaceAlreadyRunning:
        raise ApiError.conflict("RACE_ALREADY_RUNNING", "已经有一场赛事托管在跑，先停止再重新开始。")
    return RaceStatusOut(**snap)


@router.post("/stop", response_model=RaceStatusOut)
async def stop_race(_: ScanDep = None) -> RaceStatusOut:
    return RaceStatusOut(**manager.stop("手动停止"))


@router.get("/status", response_model=RaceStatusOut)
async def race_status(_: ReadDep = None) -> RaceStatusOut:
    return RaceStatusOut(**manager.snapshot())


__all__ = ["router"]
