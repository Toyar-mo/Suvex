"""`ctf_race` 赛事自动托管端点（薄封装）。

## 端点

| 方法 | 路径 | scope | 说明 |
| --- | --- | --- | --- |
| POST | `/ctf-race/start` | **scan** | 起一场自动托管（后台跑，立即返回） |
| POST | `/ctf-race/stop` | **scan** | 请求停止（当前轮跑完 / 工具返回后停） |
| GET  | `/ctf-race/status` | read | 查进度 + 成本快照 |

起一场赛跑=**对外发包**（会真的连平台、起容器、消耗模型额度），与
`/ctf2/challenges/{id}/solve` 同级 → `scan`。查进度只读 → `read`。

同一时刻只允许一场（进程内单例）：重复 start 返回 409。因为多场并发
会同时开多个沙箱容器、烧多份额度，且前端面板只显示一场，没有意义。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel, Field

from app.auth.deps import Identity, require_read, require_scan
from app.ctf_race.competition_racer import CompetitionRacer, DEFAULT_RACE_MODELS
from app.core.db import get_session_factory

logger = logging.getLogger(__name__)

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


# ── 进程内单例（同一时刻一场）────────────────────────────────────
_state: dict = {
    "racer": None,       # CompetitionRacer | None
    "task": None,        # asyncio.Task | None
    "report": None,      # 完成后的 MatchReport（dict）| None
    "error": None,       # 上次运行的错误串
}
_lock = asyncio.Lock()


def _snapshot() -> dict:
    racer: CompetitionRacer | None = _state["racer"]
    if racer is None:
        return {
            "running": False,
            "models": [],
            "challenges": {},
            "cost": {},
            "report": _state["report"],
            "error": _state["error"],
        }
    snap = racer.snapshot()
    out = {
        "running": snap.get("running", False) and not _task_done(),
        "models": snap.get("models", []),
        "challenges": snap.get("challenges", {}),
        "cost": snap.get("cost", {}),
        "report": _state["report"],
        "error": _state["error"],
    }
    return out


def _task_done() -> bool:
    task = _state["task"]
    return task is None or task.done()


async def _drive(racer: CompetitionRacer) -> None:
    """后台任务：跑完整场，落 report/error。"""
    try:
        report = await racer.run()
        _state["report"] = _report_to_dict(report)
        _state["error"] = None
        logger.info("ctf_race 完成：%s", _state["report"])
    except Exception as exc:  # noqa: BLE001
        logger.exception("ctf_race 运行异常")
        _state["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        _state["task"] = None


def _report_to_dict(report) -> dict:
    if report is None:
        return {}
    by = {}
    for rid, st in getattr(report, "by_challenge", {}).items():
        by[rid] = {
            "remote_id": getattr(st, "remote_id", rid),
            "title": getattr(st, "title", ""),
            "stage": getattr(getattr(st, "stage", None), "value", getattr(st, "stage", "")),
            "winner_model": getattr(st, "winner_model", None),
            "flag": getattr(st, "flag", None),
            "attempts": getattr(st, "attempts", 0),
            "last_error": getattr(st, "last_error", None),
        }
    return {
        "total": getattr(report, "total", 0),
        "solved": getattr(report, "solved", 0),
        "failed": getattr(report, "failed", 0),
        "skipped": getattr(report, "skipped", 0),
        "rounds": getattr(report, "rounds", 0),
        "stopped_reason": getattr(report, "stopped_reason", None),
        "elapsed_seconds": getattr(report, "elapsed_seconds", 0.0),
        "by_challenge": by,
    }


@router.post("/start", response_model=RaceStatusOut)
async def start_race(payload: RaceStartIn = Body(default_factory=RaceStartIn), _: ScanDep = None) -> RaceStatusOut:
    async with _lock:
        if _state["racer"] is not None and not _task_done():
            from app.core.errors import ApiError

            raise ApiError.conflict("RACE_ALREADY_RUNNING", "已经有一场赛事托管在跑，先停止再重新开始。")
        factory = get_session_factory()
        racer = CompetitionRacer(
            factory,
            models=tuple(payload.models) or DEFAULT_RACE_MODELS,
            max_concurrent=payload.maxConcurrent,
            max_budget_usd=payload.maxBudgetUsd,
            no_submit=payload.noSubmit,
            max_bumps=payload.maxBumps,
        )
        _state["racer"] = racer
        _state["report"] = None
        _state["error"] = None
        _state["task"] = asyncio.create_task(_drive(racer))
    return RaceStatusOut(**_snapshot())


@router.post("/stop", response_model=RaceStatusOut)
async def stop_race(_: ScanDep = None) -> RaceStatusOut:
    racer: CompetitionRacer | None = _state["racer"]
    if racer is not None:
        racer.stop("手动停止")
    return RaceStatusOut(**_snapshot())


@router.get("/status", response_model=RaceStatusOut)
async def race_status(_: ReadDep = None) -> RaceStatusOut:
    return RaceStatusOut(**_snapshot())


__all__ = ["router"]
