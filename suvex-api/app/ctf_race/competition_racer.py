"""Layer4：把「赛事自动托管」真正接到 Suvex 的平台上（生产工厂）。

`coordinator.py` 只认两个回调（`list_challenges` / `make_swarm`）；本模块
用真实的 `competition` + `build_spec` + `SandboxContainer` +
`solve_in_sandbox` + `build_system_prompt_by_spec` 把它们实现出来。

## 一道题怎么变成本地可解

比赛平台只给 `RemoteChallenge`（remote_id/title/...）。本地解题要
`KernelChallenge` 行（`build_spec` 从它读凭据/环境/附件/墙钟预算）。
所以 `make_swarm` 第一步是**把 RemoteChallenge upsert 成 KernelChallenge**
（复用 `service._upsert_challenge`，不动解题相关字段），再对它 `build_spec`。

## 多模型：同 provider 换 model 名

赛跑靠「同一道题、不同模型」。这里对每个 model：`build_spec` 拿到带
凭据的 spec，然后**把 spec.llm_model 换成该 model 名**（同一 provider 的
key/base 照用）。这最省——不需要为每个模型单配 provider。真要不同 provider
（不同厂商 key），把 `provider_override` 工厂接进去即可（预留 seam）。

## 提交

`submit_fn(flag, model)` → `competition.submit(db, remote_id, strip_flag_shell(flag))`，
返回 `(给模型看的回执, 是否确认)`。`accepted=False`（限流等）不当成"答案错"，
让 swarm 的冷却去处理而不消耗错误额度。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

from app.ctf_race.adapter import SolveOneAdapter
from app.ctf_race.coordinator import MatchCoordinator
from app.ctf_race.swarm import ChallengeSwarm, SwarmConfig
from app.ctf_race.cost_tracker import CostTracker

logger = logging.getLogger(__name__)

#: 默认赛跑模型清单（同 provider 下换 model 名即可）。可由调用方覆盖。
DEFAULT_RACE_MODELS = ("deepseek-v4.1-flash",)


def _pick(obj: Any, *names: str, default: Any = None) -> Any:
    for n in names:
        if isinstance(obj, dict):
            if n in obj:
                return obj[n]
        elif hasattr(obj, n):
            return getattr(obj, n)
    return default


class CompetitionRacer:
    """一场比赛的自动托管（真实工厂版）。

    用法（在 async 上下文里）：

        racer = CompetitionRacer(db_session_factory, models=[...])
        report = await racer.run()
        racer.snapshot()   # 随时可查进度

    `db_session_factory()` 返回一个 session 上下文管理器（见 `session_scope`）。
    """

    def __init__(
        self,
        session_factory: Callable[[], Any],
        *,
        models: tuple[str, ...] = DEFAULT_RACE_MODELS,
        max_concurrent: int = 2,
        poll_interval: float = 5.0,
        max_budget_usd: float | None = None,
        no_submit: bool = False,
        max_bumps: int = 3,
    ) -> None:
        self._session_factory = session_factory
        self._models = tuple(models)
        self._max_concurrent = max_concurrent
        self._poll_interval = poll_interval
        self._max_budget_usd = max_budget_usd
        self._no_submit = no_submit
        self._max_bumps = max_bumps

        #: 稳定的 remote_id → (KernelChallenge.id, RemoteChallenge 快照)
        self._challenge_ids: dict[str, str] = {}
        self._cost = CostTracker()
        self._coord: MatchCoordinator | None = None
        #: 远程题快照缓存（remote_id → RemoteChallenge），供 spec 构造取标题/分类。
        self._remote_cache: dict[str, Any] = {}

    # ── 快照（给 API/前端轮询）────────────────────────────────────
    def snapshot(self) -> dict:
        base = {"models": list(self._models), "running": self._coord is not None}
        if self._coord is not None:
            base["challenges"] = self._coord.snapshot()
            try:
                base["cost"] = self._cost.summary()
            except Exception:  # noqa: BLE001
                base["cost"] = {}
        return base

    def stop(self, reason: str = "手动停止") -> None:
        if self._coord is not None:
            self._coord.stop(reason)

    # ── 主入口 ────────────────────────────────────────────────────
    async def run(self) -> Any:
        self._coord = MatchCoordinator(
            list_challenges=self._list_challenges,
            make_swarm=self._make_swarm,
            max_concurrent=self._max_concurrent,
            poll_interval=self._poll_interval,
            max_budget_usd=self._max_budget_usd,
            cost_tracker=self._cost,
        )
        try:
            return await self._coord.run_until_idle()
        finally:
            self._coord = None

    # ── 回调 1：拉题库 ────────────────────────────────────────────
    async def _list_challenges(self) -> list[dict]:
        from app.chat import competition as competition_mod

        with self._session_factory() as db:
            tasks = await competition_mod.list_tasks(db)
        out: list[dict] = []
        for t in tasks:
            rid = _pick(t, "remote_id", "remoteId")
            self._remote_cache[str(rid)] = t        # 缓存原始快照
            out.append(
                {
                    "remote_id": rid,
                    "title": _pick(t, "title", default=""),
                    "solved": bool(_pick(t, "solved", default=False)),
                    "is_open": bool(_pick(t, "is_open", "isOpen", default=True)),
                    "category": _pick(t, "category", default=""),
                    "description": _pick(t, "description", default=""),
                    "_remote": t,
                }
            )
        return out

    # ── 回调 2：造一个 swarm ──────────────────────────────────────
    def _make_swarm(self, state: Any) -> ChallengeSwarm:
        # state 是 coordinator 的 ChallengeState（有 remote_id）
        remote_id = _pick(state, "remote_id")

        def spec_factory(model: str):
            return self._build_spec_for(remote_id, model)

        def sandbox_factory(spec: Any):
            return self._build_sandbox(spec)

        async def submit_fn(flag: str, model: str) -> tuple[str, bool]:
            return await self._submit(remote_id, flag, model)

        def system_prompt_builder(spec: Any) -> str:
            from app.kernel.runner import build_system_prompt_by_spec

            return build_system_prompt_by_spec(spec)

        adapter = SolveOneAdapter(
            spec_factory=spec_factory,
            sandbox_factory=sandbox_factory,
            submit_fn=submit_fn,
            cost_tracker=self._cost,
            system_prompt_builder=system_prompt_builder,
        )
        return ChallengeSwarm(
            challenge=remote_id,
            models=self._models,
            solve_one=adapter,
            config=SwarmConfig(models=self._models, max_bumps=self._max_bumps, no_submit=self._no_submit),
            submit_fn=submit_fn,
        )

    # ── 组装 spec：把 RemoteChallenge upsert 成本地 KernelChallenge → build_spec ──
    def _build_spec_for(self, remote_id: str, model: str):
        from app.kernel import runner as runner_mod
        from app.kernel import service as kernel_service
        from app.chat import competition as competition_mod

        with self._session_factory() as db:
            challenge = kernel_service.get_challenge_by_remote_id(db, remote_id) \
                if hasattr(kernel_service, "get_challenge_by_remote_id") else None
            if challenge is None:
                challenge = self._ensure_local_challenge(db, remote_id)
            spec = runner_mod.build_spec(db, challenge)
        # 换模型名（同 provider 的 key/base 照用）
        try:
            spec.llm_model = model
        except Exception:  # noqa: BLE001 - frozen 之类
            logger.debug("无法改写 spec.llm_model（忽略）")
        return spec

    def _ensure_local_challenge(self, db: Any, remote_id: str):
        """把一道远程题 upsert 成 KernelChallenge（若不存在）。"""
        from app.kernel.models import KernelChallenge, KernelCompetition
        from app.kernel import service as kernel_service

        existing = (
            db.query(KernelChallenge)
            .filter(KernelChallenge.remote_id == str(remote_id))
            .first()
        )
        if existing is not None:
            return existing
        # 找/建一个 competition 归属
        comp = db.query(KernelCompetition).first()
        if comp is None:
            raise RuntimeError("没有可用的 competition，无法把远程题落成本地题")
        payload = {"id": remote_id}
        rc = self._remote_cache.get(remote_id)
        if rc is not None:
            payload = {
                "id": remote_id,
                "title": _pick(rc, "title", default=""),
                "category": _pick(rc, "category", default=""),
                "description": _pick(rc, "description", default=""),
                "score": _pick(rc, "score"),
            }
        challenge, _created = kernel_service._upsert_challenge(
            db, competition=comp, stage_id=None, payload=payload
        )
        db.commit()
        return challenge

    def _build_sandbox(self, spec: Any):
        from app.kernel import runner as runner_mod
        from app.kernel.runner import SandboxContainer

        sb = SandboxContainer(
            name=f"race-{spec.challenge_id[-8:]}-{os.urandom(3).hex()}",
            image=runner_mod.image_for_profile(spec.profile),
            network_isolated=spec.network_isolated,
            cpus=getattr(spec, "cpus", None),
            memory_mb=getattr(spec, "memory_mb", None),
            host_resources=runner_mod._host_resources_path(),
            attachments_dir=runner_mod._attachment_dir(spec.challenge_id),
            workspace_dir=runner_mod._workspace_dir(spec.challenge_id, create=True),
            extra_env={"PWNLIB_NOTERM": "1"},
        )
        sb.start()
        return sb

    async def _submit(self, remote_id: str, flag: str, model: str) -> tuple[str, bool]:
        from app.chat import competition as competition_mod

        stripped = competition_mod.strip_flag_shell(flag)
        with self._session_factory() as db:
            try:
                outcome = await competition_mod.submit(db, remote_id, stripped)
            except Exception as exc:  # noqa: BLE001
                return (f"提交异常：{exc}", False)
        accepted = bool(_pick(outcome, "accepted", default=False))
        solved = bool(_pick(outcome, "is_solved", "isSolved", default=False))
        msg = _pick(outcome, "message", default="") or ""
        if solved:
            return (f"CORRECT：{msg}", True)
        if not accepted:
            return (f"平台未受理（不扣错误次数）：{msg}", False)
        return (f"INCORRECT：{msg}", False)


__all__ = ["CompetitionRacer", "DEFAULT_RACE_MODELS"]
