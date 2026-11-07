"""ChallengeSwarm —— 同一道题、多模型并行赛跑，先得 flag 者胜（学
ctf-agent `swarm.py`，但做成驱动无关）。

## 核心契约：注入一个 `SolveOneFn`

本模块**不认识** Suvex 的沙箱/网关/比赛平台。它只认一个 async callable：

    async def solve_one(model: str, *, hint: str | None) -> SolverResult: ...

* `model` 是模型标识（adapter 用它选 gateway）。
* `hint` 是 bump 时注入的「兄弟洞察」文本（首次为 None）。
* 返回 `SolverResult`：`status` ∈ {flag_found, gave_up, error, quota_error,
  cancelled}；`flag` 仅在**平台确认**后非空。

这样核心编排可以脱离 Docker/DB 单测；接线在 `adapter.py`。

## 关键行为（逐条对应 ctf-agent，且都保留硬刹车）

1. **并行首胜**：`asyncio.wait(FIRST_COMPLETED)`，任一 `flag_found` →
   置 cancel 事件、取消其余。
2. **bump 循环**：某模型 `gave_up|error` 且非 broken（0 步 0 花费）→ 冷却
   `min(bump_count*30, 300)` 秒后，把「兄弟洞察」作为 hint 再跑一次；连续
   3 次 error 才真放弃；`quota_error` 交给 adapter（它可能换 gateway）——
   本模块只把它当「可重试」但同样受 bump 次数上限约束。
3. **提交安全**：`submit_flag` 由 adapter 通过 `submit_fn` 注入；本模块只
   负责 per-model 递增冷却 `[0,30,120,300,600]`、全局去重、加锁、胜出短路。
4. **findings 广播**：每个模型每轮结束后把 `findings_summary` post 到
   message_bus（跳过 error/broken/空），供 bump 时作为兄弟洞察。

## 与 ctf-agent 的差异

* bump 也受**总 bump 上限** `max_bumps` 约束（防「无限 bump 烧钱」，ctf-agent
  只靠冷却不设上限——这是它的一个隐患，本包补上）。
* `quota_error` 不在此处做具体回退，只返回状态让 adapter 决策，且**同样计入
  bump 预算**。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from app.ctf_race.message_bus import ChallengeMessageBus
from app.ctf_race.solver_base import (
    CANCELLED,
    ERROR,
    FLAG_FOUND,
    GAVE_UP,
    QUOTA_ERROR,
    SolverResult,
)

logger = logging.getLogger(__name__)

#: 提交冷却（秒）：错得越多等越久。CTF 平台每题提交次数有限（很多 3~50 次），
#: 瞎猜会烧掉真机会——这道闸是「多模型并行」场景的安全前提。
SUBMISSION_COOLDOWNS: tuple[int, ...] = (0, 30, 120, 300, 600)

#: bump 冷却基数（秒）与上限（秒），与 ctf-agent 一致。
BUMP_COOLDOWN_BASE = 30
BUMP_COOLDOWN_MAX = 300


#: 「跑一个模型」的注入契约。
SolveOneFn = Callable[..., Awaitable[SolverResult]]

#: 「提交一个 flag」的注入契约：返回 (给模型看的回执, 是否被平台确认)。
SubmitFn = Callable[[str, str], Awaitable[tuple[str, bool]]]


@dataclass
class SwarmConfig:
    """一次赛跑的硬边界（全部是刹车，不是可选项）。"""

    models: list[str]
    max_bumps: int = 3                 # 每模型最多被 bump 几次（防无限续跑烧钱）
    max_consecutive_errors: int = 3    # 连续 error 达到就弃
    bump_cooldown_base: int = BUMP_COOLDOWN_BASE
    bump_cooldown_max: int = BUMP_COOLDOWN_MAX
    no_submit: bool = False            # True=只求解不提交（演练模式）


@dataclass
class SwarmResult:
    """整场赛跑的结果。"""

    challenge: str
    winner_model: str | None = None
    flag: str | None = None
    per_model: dict[str, SolverResult] = field(default_factory=dict)
    cancelled: bool = False


class ChallengeSwarm:
    """同一道题的多模型并行赛跑。"""

    def __init__(
        self,
        *,
        challenge: str,
        models: list[str],
        solve_one: SolveOneFn | None = None,
        config: SwarmConfig | None = None,
        submit_fn: SubmitFn | None = None,
        message_bus: ChallengeMessageBus | None = None,
    ) -> None:
        self.challenge = challenge
        cfg = config or SwarmConfig(models=models)
        self.config = cfg
        self.models = list(cfg.models)
        self._solve_one = solve_one
        self._submit_fn = submit_fn
        self.message_bus = message_bus or ChallengeMessageBus()

        self.cancel_event = asyncio.Event()
        self.per_model: dict[str, SolverResult] = {}
        self.winner: SolverResult | None = None
        self.confirmed_flag: str | None = None

        self._flag_lock = asyncio.Lock()
        self._submit_count: dict[str, int] = {}       # per-model 错误提交数
        self._submitted_flags: set[str] = set()       # 全局精确去重
        self._last_submit_at: dict[str, float] = {}   # per-model 上次提交时钟
        self._findings_text: dict[str, str] = {}       # per-model 最近 findings 摘要

    def bind_solver(self, solve_one: SolveOneFn) -> None:
        """事后绑定「跑一个模型」的实现。

        ## 为什么需要它（v1.73）

        生产接线里两个对象互相依赖：`SolveOneAdapter` 要拿
        `swarm.try_submit_flag` 当提交闸（no_submit / 冷却 / 去重 / 已解
        短路），而 `ChallengeSwarm` 要拿 adapter 当 `solve_one`。

        原来绕开的办法是"adapter 直接调 `competition.submit`"——代价是
        **整道闸被跳过**：演练模式（`noSubmit=true`）照样真提交、模型连错
        时没有冷却、多个模型会把同一个 flag 重复交。每一次都在消耗平台
        给的错误提交次数（不可再生资源）。

        所以改成：先建 swarm（`solve_one` 占位），绑定 adapter 再跑。
        """
        self._solve_one = solve_one

    # ── 提交安全 ───────────────────────────────────────────────────────
    def _now(self) -> float:
        # 用事件循环时钟，便于测试 monkeypatch。
        return asyncio.get_event_loop().time()

    async def try_submit_flag(self, flag: str, model: str) -> tuple[str, bool]:
        """冷却门控 + 去重 + 加锁 + 胜出短路的提交。

        返回 `(给模型看的回执, 是否被确认)`。即使 `no_submit=True`（演练），
        也做去重，但不真提交、永不确认——让模型可以照常「尝试提交」而不扣分。
        """
        normalized = (flag or "").strip()
        async with self._flag_lock:
            if self.confirmed_flag:
                return f"ALREADY SOLVED —— flag 已确认：{self.confirmed_flag}", True
            if not normalized:
                return "INCORRECT —— 空 flag。", False
            if normalized in self._submitted_flags:
                return "INCORRECT —— 这个 flag 精确地尝试过了。", False

            wrong = self._submit_count.get(model, 0)
            cooldown = SUBMISSION_COOLDOWNS[min(wrong, len(SUBMISSION_COOLDOWNS) - 1)]
            if cooldown > 0:
                last = self._last_submit_at.get(model, 0.0)
                elapsed = self._now() - last
                if elapsed < cooldown:
                    remaining = int(cooldown - elapsed)
                    return (
                        f"COOLDOWN —— 还要等 {remaining}s 才能再提交。你已错 {wrong} 次，"
                        "趁这时间再深入验证，别乱猜。",
                        False,
                    )

            self._submitted_flags.add(normalized)

            if self.config.no_submit or self._submit_fn is None:
                return "INCORRECT —— 演练模式：未真提交（不扣分）。", False

            display, confirmed = await self._submit_fn(normalized, model)
            if confirmed:
                self.confirmed_flag = normalized
            else:
                self._submit_count[model] = wrong + 1
                self._last_submit_at[model] = self._now()
            return display, confirmed

    # ── 单模型 bump 循环 ───────────────────────────────────────────────
    def _sibling_insights(self, exclude_model: str) -> str:
        parts = [
            f"[{m}]: {t}"
            for m, t in self._findings_text.items()
            if m != exclude_model and t
        ]
        return "\n\n".join(parts) if parts else "（其它模型暂时还没有可用洞察。）"

    async def _run_solver(self, model: str) -> SolverResult:
        if self._solve_one is None:
            # 生产接线忘了 `bind_solver` 时，宁可在这里**大声**炸掉，
            # 也不要让模型跑到一半才发现没有解题实现。
            raise RuntimeError(
                f"swarm({self.challenge}) 没有绑定 solve_one —— 忘了 bind_solver()"
            )
        bump_count = 0
        consecutive_errors = 0
        last = SolverResult(model=model, status=CANCELLED)

        while not self.cancel_event.is_set():
            hint = None
            if bump_count > 0:
                hint = self._sibling_insights(model)
            try:
                result = await self._solve_one(model, hint=hint)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 单模型崩了不该拖垮整场
                logger.warning("[%s/%s] solve_one 抛异常: %s", self.challenge, model, exc)
                result = SolverResult(model=model, status=ERROR, step_count=0, cost_usd=0.0)
            last = result

            # 广播有用洞察（跳过 error/broken/空/明显是错误串）
            if (
                result.status not in (ERROR, QUOTA_ERROR)
                and not result.is_broken
                and result.findings_summary
                and not result.findings_summary.startswith(("Error:", "Turn failed:"))
            ):
                self._findings_text[model] = result.findings_summary
                await self.message_bus.post(model, result.findings_summary[:500])

            if result.is_win:
                self.cancel_event.set()
                self.winner = result
                return result

            if result.status == CANCELLED:
                break

            if result.status == QUOTA_ERROR:
                # 额度问题：交给 adapter 决定是否换网关（这里只是把它当可重试，
                # 但仍受 bump 预算约束，且不计入连续 error）。
                if bump_count >= self.config.max_bumps:
                    break
                bump_count += 1
                if await self._bump_wait(bump_count):
                    break
                continue

            if result.status in (GAVE_UP, ERROR):
                if result.is_broken:
                    # 0 步 0 花费：环境坏了/没真跑，bump 也没用。
                    break
                if result.status == ERROR:
                    consecutive_errors += 1
                    if consecutive_errors >= self.config.max_consecutive_errors:
                        break
                else:
                    consecutive_errors = 0
                if bump_count >= self.config.max_bumps:
                    break
                bump_count += 1
                if await self._bump_wait(bump_count):
                    break
                continue

            # 其它状态（未知）：保守收尾
            break

        return last

    async def _bump_wait(self, bump_count: int) -> bool:
        """bump 前冷却。返回 True 表示冷却期间被取消（调用方应 break）。"""
        cooldown = min(self.config.bump_cooldown_base * bump_count, self.config.bump_cooldown_max)
        if cooldown <= 0:
            return False
        try:
            await asyncio.wait_for(self.cancel_event.wait(), timeout=cooldown)
            return True  # 冷却期间被取消
        except (asyncio.TimeoutError, TimeoutError):
            return False

    # ── 整场 ───────────────────────────────────────────────────────────
    async def run(self) -> SwarmResult:
        tasks = {
            asyncio.create_task(self._run_solver(model), name=f"solver-{model}"): model
            for model in self.models
        }
        result = SwarmResult(challenge=self.challenge)
        try:
            pending = set(tasks)
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    model = tasks[task]
                    try:
                        r = task.result()
                        self.per_model[model] = r
                    except asyncio.CancelledError:
                        pass
                    except Exception as exc:
                        logger.warning("[%s/%s] task 异常: %s", self.challenge, model, exc)
                # 任一模型胜出（FLAG_FOUND 置 self.winner 且 set cancel_event，
                # 或经提交确认置 confirmed_flag）→ 取消其余、收场。
                if self.winner is not None or self.confirmed_flag:
                    self.cancel_event.set()
                    for p in pending:
                        p.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    break

            result.per_model = dict(self.per_model)
            result.cancelled = self.cancel_event.is_set()
            if self.winner is not None:
                result.winner_model = self.winner.model
                result.flag = self.winner.flag
            return result
        except Exception as exc:
            logger.error("[%s] swarm 崩溃: %s", self.challenge, exc, exc_info=True)
            self.cancel_event.set()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            result.per_model = dict(self.per_model)
            return result

    def kill(self) -> None:
        self.cancel_event.set()

    def status(self) -> dict:
        return {
            "challenge": self.challenge,
            "cancelled": self.cancel_event.is_set(),
            "winner": self.winner.flag if self.winner else None,
            "winnerModel": self.winner.model if self.winner else None,
            "models": {
                m: {
                    "findings": self._findings_text.get(m, ""),
                    "status": (
                        self.per_model[m].status
                        if m in self.per_model
                        else ("running" if not self.cancel_event.is_set() else "stopped")
                    ),
                }
                for m in self.models
            },
        }


__all__ = [
    "BUMP_COOLDOWN_BASE",
    "BUMP_COOLDOWN_MAX",
    "ChallengeSwarm",
    "SUBMISSION_COOLDOWNS",
    "SolveOneFn",
    "SubmitFn",
    "SwarmConfig",
    "SwarmResult",
]
