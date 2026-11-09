"""赛事托管的进程内单例管理器（HTTP 端点与对话工具共用）。

## 为什么要有这一层

`router.py` 原来自己攥着一份 `_state`（`racer` / `task` / `report` / `error`）。
现在"在对话里下指令起赛跑"也要能控制同一场 —— 如果对话工具再攥一份，
就会有两份状态：对话里起的那场和 CTF 页面板显示的那场是两回事，
甚至同时起两场赛跑（多倍容器 + 多倍额度）。

所以把单例收敛到这里，`router`（前端面板）与 `chat` 工具都调同一组函数：
**同一时刻只有一场，谁起都是它，谁查都看它**。

## 生命周期

- `start(...)`：建 racer + 后台 asyncio.Task 跑完整场。已在跑则抛
  `RaceAlreadyRunning`（HTTP 层转 409，工具层转人话）。
- `status()`：返回 JSON 友好快照（含 report/error）。
- `stop()`：请求停止（当前轮跑完/工具返回后停）。
- `_drive(...)`：后台任务，跑完落 `report`/`error`。

## 线程/事件循环

比赛跑在本进程的 event loop 上（沙箱/网关都是 async）。所以这个管理器
**必须**在应用的事件循环里用（router 与 chat 引擎都在）。后台任务用
`asyncio.create_task`；不需要跨进程。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.core.db import get_session_factory
from app.ctf_race.competition_racer import DEFAULT_RACE_MODELS, CompetitionRacer

logger = logging.getLogger(__name__)


class RaceAlreadyRunning(RuntimeError):
    """已经有一场在跑（HTTP 层 → 409，工具层 → 人话提示）。"""


# ── 进程内单例（同一时刻一场）────────────────────────────────────
_state: dict[str, Any] = {
    "racer": None,   # CompetitionRacer | None
    "task": None,    # asyncio.Task | None
    "report": None,  # 完成后的 MatchReport（dict）| None
    "error": None,   # 上次运行的错误串
}
_lock = asyncio.Lock()


def _task_done() -> bool:
    task = _state["task"]
    return task is None or task.done()


def is_running() -> bool:
    return _state["racer"] is not None and not _task_done()


def _report_to_dict(report: Any) -> dict:
    if report is None:
        return {}
    by: dict[str, dict] = {}
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


def snapshot() -> dict:
    """JSON 友好快照（router 直接包成 RaceStatusOut）。"""
    racer: CompetitionRacer | None = _state["racer"]
    if racer is None:
        return {
            "running": False,
            "models": [],
            "challenges": {},
            "cost": {},
            "match": "",
            "overview": {},
            "noSubmit": False,
            "report": _state["report"],
            "error": _state["error"],
        }
    snap = racer.snapshot()
    return {
        "running": snap.get("running", False) and not _task_done(),
        "models": snap.get("models", []),
        "challenges": snap.get("challenges", {}),
        "cost": snap.get("cost", {}),
        # ★ v1.73：赛事级信息（规则 / 排名 / 是否演练）与题目级状态分开。
        "match": snap.get("match", ""),
        "overview": snap.get("overview", {}),
        "noSubmit": snap.get("noSubmit", False),
        "report": _state["report"],
        "error": _state["error"],
    }


async def _drive(racer: CompetitionRacer) -> None:
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


async def start(
    *,
    models: tuple[str, ...] | list[str] = DEFAULT_RACE_MODELS,
    max_concurrent: int = 2,
    max_budget_usd: float | None = None,
    no_submit: bool = False,
    max_bumps: int = 3,
) -> dict:
    """起一场赛跑（后台跑，立即返回快照）。已在跑抛 `RaceAlreadyRunning`。"""
    async with _lock:
        if is_running():
            raise RaceAlreadyRunning("已经有一场赛事托管在跑，先停止再重新开始。")
        factory = get_session_factory()
        racer = CompetitionRacer(
            factory,
            models=tuple(models) or DEFAULT_RACE_MODELS,
            max_concurrent=max_concurrent,
            max_budget_usd=max_budget_usd,
            no_submit=no_submit,
            max_bumps=max_bumps,
        )
        _state["racer"] = racer
        _state["report"] = None
        _state["error"] = None
        _state["task"] = asyncio.create_task(_drive(racer))
    return snapshot()


def stop(reason: str = "手动停止") -> dict:
    racer: CompetitionRacer | None = _state["racer"]
    if racer is not None:
        racer.stop(reason)
    return snapshot()


def reset_for_test() -> None:
    """仅测试用：清回初始态。"""
    _state["racer"] = None
    _state["task"] = None
    _state["report"] = None
    _state["error"] = None


__all__ = [
    "RaceAlreadyRunning",
    "start",
    "stop",
    "snapshot",
    "is_running",
    "reset_for_test",
]
