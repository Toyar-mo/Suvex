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

★ 适配器拿到的**不是**这个函数本身，而是 `swarm.try_submit_flag`（它内部再
调它）。多这一层是因为那里守着四件事：演练模式不真提交、错答后的递增冷却、
全局 flag 去重、已解短路。绕过它等于把"每题最多 50 次"当无限次用。

## 靶机与赛事信息（v1.73）

比赛平台的靶机在**平台侧**（`GET /ctf/exercise` 的 `endpoints`），本地
`KernelChallenge` 表里没有 → 每道题开跑前 `_provision_environment`
拉一次（需要就先起、再轮询到就绪），把 URL 清单填进 `spec.access_urls`、
把渲染好的连接块填进 `spec.env_prompt`。赛事规则（`match-info`）在开赛时
取一次，拼进每道题的 `spec.brief`；排名（`overview`）节流刷新给状态接口。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from app.kernel.client import KernelRateLimited
from typing import Any, Callable

from app.ctf_race.adapter import SolveOneAdapter
from app.ctf_race.coordinator import MatchCoordinator
from app.ctf_race.swarm import ChallengeSwarm, SwarmConfig
from app.ctf_race.cost_tracker import CostTracker

logger = logging.getLogger(__name__)

#: 默认赛跑模型清单（同 provider 下换 model 名即可）。可由调用方覆盖。
DEFAULT_RACE_MODELS = ("deepseek-v4.1-flash",)

#: 靶机环境就绪的最长等待（秒）。平台起环境是**异步**的：
#: `POST /ctf/reset-endpoint` 只是受理，之后要轮询详情直到
#: `isNeedCheck=false` 且 `endpoints` 非空（见 api_doc §7 的说明）。
ENV_READY_TIMEOUT_SECONDS = 60.0

#: 轮询靶机状态的间隔（秒）。比赛平台的限流是**共享**的（列表/详情/提交
#: 同一个额度），所以这里刻意慢：1 秒一次会把额度烧在"等环境"上。
ENV_POLL_INTERVAL_SECONDS = 3.0

#: 排名（`/answer-panel/overview`）刷新节流（秒）。
OVERVIEW_REFRESH_SECONDS = 30.0


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

        # ★★ P3 提交门（2026-11-08）。fan-out 之后同一道题可能有 2–3 个
        # solver 同时想提交，而错误提交次数是**不可再生**的。
        #
        # 三道闸，全部按 remote_id 记账：
        #   1. **串行化**：同一时刻只允许一个提交在飞（`_submit_lock`）；
        #   2. **去重**：同一个答案只送平台一次（`_submitted`）；
        #   3. **递增冷却**：答错后 30 → 60 → 120 … 最多 600 秒才能再交
        #      （`_wrong_at`），"平台未受理"不触发冷却（那不是答错）。
        self._submit_lock = asyncio.Lock()
        self._submitted: dict[str, set[str]] = {}
        self._wrong_at: dict[str, list[float]] = {}

        #: 稳定的 remote_id → (KernelChallenge.id, RemoteChallenge 快照)
        self._challenge_ids: dict[str, str] = {}
        self._cost = CostTracker()
        self._coord: MatchCoordinator | None = None
        #: 远程题快照缓存（remote_id → RemoteChallenge），供 spec 构造取标题/分类。
        self._remote_cache: dict[str, Any] = {}

        #: ★ v1.73：整场共用一个驱动（HTTP 连接池 + 限速器都挂在它身上）。
        #:   每道题各造一个会让"平台限流"从「每秒 5 次」变成「每道题 5 次」，
        #:   而额度是**账号级共享**的。
        self._driver_obj: Any | None = None
        #: 赛事规则/注意事项（`/match/notice/match-info`），开赛时取一次。
        self._match_rules: str = ""
        #: 最新一次排名（`/answer-panel/overview`），给状态接口看。
        self._overview: dict[str, Any] = {}
        self._overview_at: float = 0.0
        #: ★ v1.73：靶机环境缓存（remote_id → (env, env_text)）。
        #:
        #: 靶机环境在一场里是**稳定**的（起一次、到期前一直是那台）。而每造
        #: 一个 spec（每题每个模型一次、bump 再来一次）都去平台读一次环境，
        #: 会把这些读请求打到平台限流（实测：并发 2 题时 read_environment 连续
        #: 429）。一旦 429，`_provision_environment` 就返回 `(None,"")`，那一次
        #: 尝试的模型**没有靶机地址** —— 看起来像模型无能，其实是限流把靶机
        #: 信息吞了。缓存一份，起一次就够。
        self._env_cache: dict[str, tuple[Any, str]] = {}

    # ── 快照（给 API/前端轮询）────────────────────────────────────
    def snapshot(self) -> dict:
        base = {
            "models": list(self._models),
            "running": self._coord is not None,
            # ★ v1.73：规则与排名是**赛事级**的，与"哪道题什么状态"分开给。
            #   前端面板可以只显示名次，而不用去猜成本；`noSubmit=true` 也
            #   要能一眼看出来 —— 否则"跑了一晚上一个 flag 没交"会被当成故障。
            "match": self._match_rules,
            "overview": dict(self._overview),
            "noSubmit": self._no_submit,
        }
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
        # ★ v1.73：开赛前先取一次赛事规则。比赛规则里常写着"flag 只交花括号
        #   内的内容""前三名额外加分""重置次数上限"这类**改变解题策略**的信息，
        #   而模型只能从我们这里知道它们（它没有任何方式自己去读平台页面）。
        self._match_rules = await self._load_match_rules()
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
            await self._aclose_driver()

    # ── 驱动（整场共用一个）────────────────────────────────────────
    async def _get_driver(self) -> Any:
        if self._driver_obj is not None:
            return self._driver_obj
        from app.chat import competition as competition_mod

        with self._session_factory() as db:
            driver = competition_mod.resolve_driver(db)
        if driver is None:
            raise RuntimeError(
                "当前账号没有指向比赛平台 —— 先在「设置 → 解题 / 比赛平台」绑定 AccessKey。"
            )
        self._driver_obj = driver
        return driver

    async def _aclose_driver(self) -> None:
        driver = self._driver_obj
        self._driver_obj = None
        close = getattr(driver, "aclose", None)
        if close is not None:
            try:
                await close()
            except Exception:  # noqa: BLE001 —— 关连接失败不该影响战报
                logger.debug("[ctf_race] 关闭驱动失败（忽略）", exc_info=True)

    async def _load_match_rules(self) -> str:
        """取赛事规则与注意事项，拼成一段给模型看的文字。

        ⚠️ 失败**不抛**：没有规则也能解题（只是少了策略信息），而开赛失败
        会让整场跑不起来。这与「拉题库失败本轮跳过」是同一条权重。
        """
        try:
            driver = await self._get_driver()
            info = await driver.match_info()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ctf_race] 取赛事规则失败（忽略）：%s", exc)
            return ""
        rule = str((info or {}).get("rule") or "").strip() if isinstance(info, dict) else ""
        note = str((info or {}).get("note") or "").strip() if isinstance(info, dict) else ""
        blocks = []
        if rule:
            blocks.append("### 竞赛规则\n" + rule)
        if note:
            blocks.append("### 竞赛注意事项\n" + note)
        text = "\n\n".join(blocks)
        # 平台可能贴一整页规则。截断而不是整段塞进每道题的提示词 ——
        # 30 道题 × 5000 字规则会把 prompt 预算全吃掉。
        return text[:4000]

    # ── 回调 1：拉题库 ────────────────────────────────────────────
    async def _list_challenges(self) -> list[dict]:
        from app.chat import competition as competition_mod

        with self._session_factory() as db:
            tasks = await competition_mod.list_tasks(db)

        # ★ v1.73：顺带刷新排名（节流）。它决定"要不要更激进地抢题"
        #   （这场比赛的规则里有"前三名额外加分"），而排名的变化频率
        #   远低于题库 —— 每 30 秒一次足够，且不会把限流额度烧在它身上。
        await self._refresh_overview()

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

    async def _refresh_overview(self) -> None:
        now = time.monotonic()
        if now - self._overview_at < OVERVIEW_REFRESH_SECONDS:
            return
        self._overview_at = now
        try:
            driver = await self._get_driver()
            data = await driver.overview()
        except Exception as exc:  # noqa: BLE001 —— 排名拿不到不影响解题
            logger.debug("[ctf_race] 刷新排名失败（忽略）：%s", exc)
            return
        if isinstance(data, dict):
            self._overview = data

    # ── 回调 2：造一个 swarm ──────────────────────────────────────
    def _make_swarm(self, state: Any) -> ChallengeSwarm:
        # state 是 coordinator 的 ChallengeState（有 remote_id）
        remote_id = str(_pick(state, "remote_id") or "")

        async def spec_factory(model: str):
            return await self._build_spec_for(remote_id, model)

        def sandbox_factory(spec: Any):
            return self._build_sandbox(spec)

        async def submit_fn(flag: str, model: str) -> tuple[str, bool]:
            return await self._submit(remote_id, flag, model)

        def system_prompt_builder(spec: Any) -> str:
            from app.kernel.runner import build_system_prompt_by_spec

            return build_system_prompt_by_spec(spec)

        swarm = ChallengeSwarm(
            challenge=remote_id,
            models=self._models,
            solve_one=None,   # 下面 bind_solver 补上（见类里的说明）
            config=SwarmConfig(
                models=self._models, max_bumps=self._max_bumps, no_submit=self._no_submit
            ),
            submit_fn=submit_fn,
        )
        adapter = SolveOneAdapter(
            spec_factory=spec_factory,
            sandbox_factory=sandbox_factory,
            # ★★ v1.73：**必须**走 `swarm.try_submit_flag`，而不是直接调
            #   `self._submit`。它守着四件事：演练模式不真提交、每条错答
            #   之后的递增冷却、全局 flag 去重、已解短路。绕过它等于把
            #   平台给的"每题最多 50 次"当成无限次用 —— 而那是不可再生资源。
            submit_fn=swarm.try_submit_flag,
            cost_tracker=self._cost,
            system_prompt_builder=system_prompt_builder,
        )
        swarm.bind_solver(adapter)
        return swarm

    # ── 组装 spec：把 RemoteChallenge upsert 成本地 KernelChallenge → build_spec ──
    async def _build_spec_for(self, remote_id: str, model: str):
        from app.kernel import runner as runner_mod
        from app.kernel import service as kernel_service
        from app.kernel.platform import endpoint_access_urls

        with self._session_factory() as db:
            challenge = kernel_service.get_challenge_by_remote_id(db, remote_id) \
                if hasattr(kernel_service, "get_challenge_by_remote_id") else None
            if challenge is None:
                challenge = self._ensure_local_challenge(db, remote_id)
            spec = runner_mod.build_spec(db, challenge)

        # ★★ v1.73：靶机连接信息在**平台侧**，本地表里没有。
        #
        # 不接这一步的后果是赛跑看起来"在跑"，但模型手里只有题面：
        # 它不知道去打哪个 IP、用哪个账号 —— 而报错是"连不上"，
        # 看起来像模型无能，实际是接线漏了。
        env, env_text = await self._provision_environment(remote_id)
        if env is not None:
            urls = endpoint_access_urls(env)
            if urls:
                spec.access_urls = urls
            if env_text:
                spec.env_prompt = env_text
                # 有靶机就必须放开容器网络：web 要用 gobuster/ffuf 扫目录，
                # pwn 要用 nc 连 TCP。隔离的容器里这两件事都做不到。
                spec.network_isolated = False

        if self._match_rules:
            spec.brief = (self._match_rules + "\n\n" + spec.brief).strip()

        # 换模型名（同 provider 的 key/base 照用）
        try:
            spec.llm_model = model
        except Exception:  # noqa: BLE001 - frozen 之类
            logger.debug("无法改写 spec.llm_model（忽略）")
        return spec

    async def _provision_environment(self, remote_id: str) -> tuple[Any, str]:
        """拉一道题的靶机环境；需要就先起，再轮询到就绪。

        返回 `(env, 给模型看的文字)`。拿不到时返回 `(None, "")` ——
        纯附件题本来就没有靶机，那**不是**错误。

        ## 为什么要"先起再轮询"

        文档 §5/§7 写得很清楚：环境是**异步**的。`isNeedInit=true` 表示
        还没起（或者已经被回收），而起环境只有"受理"这一步，之后要反复
        取详情直到 `isNeedCheck=false` **且** `endpoints` 非空。

        ## 为什么起一次失败就继续

        `isNeedInit` 为真但平台拒绝（比如免费重置次数用完）时，这道题
        仍然可能已经有可用环境（`endpoints` 非空）。把它当致命错误会让
        整道题白丢 —— 而正确的做法是"用现有的环境接着解"。
        """
        # ★ v1.73：命中缓存直接返回（靶机在一场里是稳定的），省掉重复读、
        #   也就避开了 429 把靶机信息吞掉的问题。
        cached = self._env_cache.get(remote_id)
        if cached is not None:
            return cached

        try:
            driver = await self._get_driver()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ctf_race] 拿不到比赛平台驱动：%s", exc)
            return None, ""

        handle = {"id": remote_id}

        async def _read_env_retry() -> Any:
            """读环境；429 时按 retry_after 退避重试若干次。

            平台限流是**账号级共享**的：并发解题 + 逐题读环境很容易撞 429。
            旧实现一撞 429 就 `return (None,"")`，那次尝试的模型拿不到靶机
            地址 —— 静默地少一份关键输入。这里退避重试，读到为止（有上限）。
            """
            last_exc: Exception | None = None
            for attempt in range(4):
                try:
                    return await driver.read_environment(handle)
                except KernelRateLimited as exc:
                    last_exc = exc
                    wait = getattr(exc, "retry_after", None) or (0.8 * (attempt + 1) + 0.5)
                    await asyncio.sleep(min(wait, 8.0))
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    break
            if last_exc is not None:
                logger.warning("[ctf_race] 题 %s 读环境失败：%s", remote_id, last_exc)
            raise last_exc if last_exc is not None else RuntimeError("read_environment failed")

        try:
            env = await _read_env_retry()
        except Exception:  # noqa: BLE001
            return None, ""

        if (not env.ready) and (env.needs_init or not env.endpoints):
            try:
                await driver.start_environment(handle)
            except Exception as exc:  # noqa: BLE001 —— 起不来就用现有的
                logger.info("[ctf_race] 题 %s 起环境失败（继续用现有环境）：%s", remote_id, exc)

            deadline = time.monotonic() + ENV_READY_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                await asyncio.sleep(ENV_POLL_INTERVAL_SECONDS)
                try:
                    env = await _read_env_retry()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[ctf_race] 题 %s 轮询环境失败：%s", remote_id, exc)
                    continue
                if env.ready:
                    break

        try:
            text = await driver.environment_prompt(env)
        except Exception:  # noqa: BLE001
            text = ""

        # 只有真就绪（或真有 endpoints）才缓存；暂时拿不到的不缓存，下次再试。
        if getattr(env, "ready", False) or getattr(env, "endpoints", None):
            self._env_cache[remote_id] = (env, text)
        return env, text

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

        # ★★ v1.73：靶机在**公网**（DASCTF 的 exposeIps 是 `1.14.76.59` 这种公网
        #   IP），而默认解题档的 `suvex-ctf2` 网桥靠 iptables 白名单**只放行
        #   CTF2 靶站网段（172.18.0.0/16）** —— 于是容器根本连不到比赛靶机，
        #   模型每条命令都超时，几轮就放弃，而症状是"模型不会解题"（实际是
        #   被网络墙挡在门外）。
        #
        #   解法：不隔离时改用 `AGENT_NETWORK`（`suvex-agent`，有公网出站、但
        #   封住平台自身域名），走的是**解题档**的严格 argv（只读根 / cap-drop /
        #   pids-limit 全在），比 `unrestricted=True`（对话档）更收敛。
        network_name = None
        if not spec.network_isolated:
            network_name = runner_mod.AGENT_NETWORK

        # ★ 安全阀：先压一次总量（见 `enforce_race_sandbox_cap` 的事故记录）。
        #   放在建容器之前 —— 容器建不出来时（docker run 超时）再清理已经晚了。
        runner_mod.enforce_race_sandbox_cap()

        sb = SandboxContainer(
            name=f"race-{spec.challenge_id[-8:]}-{os.urandom(3).hex()}",
            image=runner_mod.image_for_profile(spec.profile),
            network_isolated=spec.network_isolated,
            network_name=network_name,
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

        async with self._submit_lock:
            seen = self._submitted.setdefault(remote_id, set())
            if stripped in seen:
                # 去重：**不发请求**。fan-out 时两个 solver 常常同时想到同一个
                # 答案，重复提交只会白白消耗配额。
                return ("同一个答案已经提交过（去重，不消耗配额）。", False)

            now = time.monotonic()
            recent = [t for t in self._wrong_at.get(remote_id, []) if now - t < 600]
            self._wrong_at[remote_id] = recent
            if recent:
                cooldown = min(30 * (2 ** (len(recent) - 1)), 600)
                remaining = cooldown - (now - recent[-1])
                if remaining > 0:
                    return (
                        f"冷却中：{int(remaining)} 秒后才能再次提交"
                        f"（连续 {len(recent)} 次答错，冷却 {int(cooldown)} 秒）。",
                        False,
                    )

            # ⚠️ 只在**平台真的收到**之后才登记去重（见下面的 add）：
            #    请求抛异常时不能把它记成"已提交"，否则一次网络抖动
            #    会让这个答案永远交不出去。
            with self._session_factory() as db:
                try:
                    outcome = await competition_mod.submit(db, remote_id, stripped)
                except Exception as exc:  # noqa: BLE001
                    return (f"提交异常：{exc}", False)

            accepted = bool(_pick(outcome, "accepted", default=False))
            solved = bool(_pick(outcome, "is_solved", "isSolved", default=False))
            msg = _pick(outcome, "message", default="") or ""
            seen.add(stripped)
            if solved:
                return (f"CORRECT：{msg}", True)
            if not accepted:
                return (f"平台未受理（不扣错误次数）：{msg}", False)
            # 真的答错了 → 记一次冷却
            self._wrong_at.setdefault(remote_id, []).append(time.monotonic())
            return (f"INCORRECT：{msg}", False)

    # ── 单题解题（给「开始解题」按钮 / 单题 worker 复用，v1.74）──────
    async def solve_one(
        self,
        remote_id: str,
        *,
        challenge_id: str | None = None,
        resume_digest: tuple[str, ...] = (),
    ) -> dict:
        """用**已证明可用**的赛跑机制解**一道**题，并把结果写回本地题目行。

        ## 为什么要有它

        「开始解题」按钮走的是旧的 CTF2 worker（`runner._run_worker` →
        `KernelClient`），它只认 CTF2 的 `practice/{id}/challenges/{cid}/...`
        形状；接配置驱动（GenericDriver）平台后，那条路会去请求**平台不存在
        的端点**，`/ctf/reset-endpoint` 收到的 `exerciseId` 也不是平台要的
        整数 → `INVALID_ID` 400。而赛跑（`CompetitionRacer.run`）早就把
        「起环境 → 给靶机地址 → 解题 → 提交」这条链在 GenericDriver 上跑通了。

        于是单题解题**直接复用** `_build_spec_for`（含 v1.73 的起环境/填
        access_urls）＋ 一次 `SolveOneAdapter` ＋ `_submit`，再把结果落回
        `KernelChallenge`（复用 `runner._advance_challenge` 的状态机）。

        ## 返回

        `{"status": ..., "flag": ..., "message": ...}` —— 给 worker 用来决定
        最终状态（其实这里已经把状态写回库了，返回只是给日志/调用方看）。
        """
        from app.ctf_race.adapter import SolveOneAdapter
        from app.ctf_race.swarm import ChallengeSwarm, SwarmConfig

        # 1) 找到本地题目行（拿 challenge_id 写回用）
        if challenge_id is None:
            with self._session_factory() as db:
                from app.kernel import service as kernel_service

                if hasattr(kernel_service, "get_challenge_by_remote_id"):
                    ch = kernel_service.get_challenge_by_remote_id(db, remote_id)
                    challenge_id = ch.id if ch is not None else None

        if not self._models:
            self._models = DEFAULT_RACE_MODELS
        model = self._models[0]

        # 2) 组装 spec（含起靶机 + 填 access_urls）
        async def spec_factory(_model: str):
            return await self._build_spec_for(remote_id, _model)

        def sandbox_factory(spec: Any):
            return self._build_sandbox(spec)

        async def submit_fn(flag: str, _model: str) -> tuple[str, bool]:
            return await self._submit(remote_id, flag, _model)

        def system_prompt_builder(spec: Any) -> str:
            from app.kernel.runner import build_system_prompt_by_spec

            return build_system_prompt_by_spec(spec)

        # 用 ChallengeSwarm 的 try_submit_flag 守住"演练不提交 / 冷却 / 去重"，
        # 但这里只跑一个模型、不做多模型协同 —— 单题解题不需要 swarm 的调度。
        swarm = ChallengeSwarm(
            challenge=remote_id,
            models=(model,),
            solve_one=None,
            config=SwarmConfig(models=(model,), max_bumps=self._max_bumps, no_submit=self._no_submit),
            submit_fn=submit_fn,
        )
        adapter = SolveOneAdapter(
            spec_factory=spec_factory,
            sandbox_factory=sandbox_factory,
            submit_fn=swarm.try_submit_flag,
            cost_tracker=self._cost,
            system_prompt_builder=system_prompt_builder,
            # ★ v1.80：续跑的上次动作纪要 → `SolveSpec.resume_digest`
            #   （首轮提示里会展开成"别重复做过的事"）。
            resume_digest=tuple(resume_digest),
            # 过程写进活动流（推理 / 工具调用 / 工具报错）。
            # 不传它，单题解题在界面上只有"开始 / 结束"两条 —— 实测
            # 2026-11-08 用户看到一片空白，原话「没有记录啊」。
            challenge_id=challenge_id,
        )

        # ★★ P2 骨架（2026-11-08）：同题 fan-out，先到先得。
        #
        # 默认 `SUPEX_CTF_FANOUT=1` —— **行为与之前逐字节一致**；配成 2/3
        # 才会同时开多个 solver。骨架阶段刻意不默认开启：多 solver 同时
        # 提交会共用平台的错误提交配额，而提交门的串行化还没做
        # （见 ADR-010 P3）。先把并发通路跑通，再补提交侧。
        _fanout = _fanout_count()
        models = list(self._models[:_fanout]) or [model]
        # 只配了一个模型但要求 fan-out > 1：**同模型双开**（不引入新成本，
        # 靠采样多样性）。要异构就把第二个模型加进 `models` 配置。
        while len(models) < _fanout:
            models.append(models[0])
        tasks: dict[Any, str] = {}
        try:
            if len(models) <= 1:
                result = await adapter(model, hint=None)
            else:
                tasks = {
                    asyncio.create_task(adapter(m, hint=None)): m for m in models
                }
                result = await _first_winner(tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await self._aclose_driver()

        # 3) 把结果写回本地题目行（复用 runner 的状态机，避免两套口径）
        flag = getattr(result, "candidate_flag", None) or getattr(result, "flag", None)
        confirmed = getattr(result, "status", "") == "flag_found" or bool(
            getattr(result, "flag", None)
        )
        msg = getattr(result, "findings_summary", "") or ""
        if challenge_id:
            await self._persist_single_result(challenge_id, flag=flag, confirmed=confirmed, message=msg)

        return {
            "status": getattr(result, "status", "unknown"),
            "flag": getattr(result, "flag", None),
            "message": msg,
            # ★ 原始 `SolveOutcome`（`SolveOneAdapter` 挂的）—— worker 的收尾段
            #   靠它写遥测/用量/题解。没有它就只能收 scan 壳。
            "outcome": (getattr(result, "extra", None) or {}).get("outcome"),
        }

    async def _persist_single_result(
        self, challenge_id: str, *, flag: str | None, confirmed: bool, message: str
    ) -> None:
        """把一次单题解题的结果落回 `KernelChallenge`。

        复用 `runner._advance_challenge` —— 它管好了 safe/auto 的判定与
        `candidate_flag`/`confirmed_flag` 的那些 CHECK 约束（两列不同时有值、
        解出才有 confirmed）。这里只负责"确认 flag"与"只留候选"两种情况。
        """
        from app.kernel.models import KernelChallenge
        from app.kernel.runner import _advance_challenge, normalize_flag

        with self._session_factory() as db:
            ch = db.get(KernelChallenge, challenge_id)
            if ch is None:
                return
            from app.core.vocab import KernelChallengeStatus
            # ★ 候选计数：与 `runner._handle_candidates` 同口径 —— 这一轮只要
            #   给出了 flag 就记一次（确认、留候选、被丢弃都算一次尝试）。
            #   这里原来完全不写，于是一道"候选待确认"的题在界面上显示
            #   「候选 0」，而 `candidate_flag` 里明明躺着一个 flag
            #   （实测 2026-11-09，`scan_01M6ZKXGWXENG1XH5MZ62B1P76`）。
            if flag:
                ch.candidate_count = int(ch.candidate_count or 0) + 1
            if confirmed and flag:
                _advance_challenge(ch, "correct", normalize_flag(flag))
            elif flag:
                # 有候选但没被平台确认（或 noSubmit）：留作**待确认**，不判错。
                #
                # ★★ 状态必须是 `candidate_ready`，不能是 `solving`
                #   （2026-11-08 修，真库实测 5 道题受害）。
                #
                #   写 `solving` 时这一轮其实**已经结束**了，但界面会说
                #   "解题中"，于是：
                #     · 用户看到一道永远不会自己结束的题；
                #     · 行内还挂着「取消」，一点就把这轮的结果判成 cancelled
                #       —— 而 cancel 只改状态、**不清候选**，最后落成
                #       "未开始 + 有一个候选 flag + 已取消。" 这种谁都读不懂的行。
                #
                #   `candidate_ready` 的语义正是"跑完了、有东西等你确认"，
                #   与 `runner._handle_candidates` 的 safe 落点一致。
                ch.candidate_flag = flag
                if ch.status != KernelChallengeStatus.SOLVED.value:
                    ch.status = KernelChallengeStatus.CANDIDATE_READY.value
                ch.last_error = None
            else:
                # 没拿到 flag：如实记一句，但不当成平台错。
                #
                # ⚠️ 不能盖掉**已经有候选**的题（fan-out 时兄弟 solver 先
                #    落了一个候选，这一路空手而归）—— 那会把"等你确认"
                #    降级成"未解出"，候选还在，界面却说没解出。
                if ch.status not in (
                    KernelChallengeStatus.SOLVED.value,
                    KernelChallengeStatus.CANDIDATE_READY.value,
                ):
                    ch.status = KernelChallengeStatus.FAILED.value
                    ch.last_error = (message or "解题未产出候选 flag")[:500]
            db.commit()


__all__ = ["CompetitionRacer", "DEFAULT_RACE_MODELS"]

def _fanout_count() -> int:
    """同题并发 solver 数（环境变量 `SUPEX_CTF_FANOUT`，默认 1，上限 3）。

    ★ 为什么默认 1：fan-out 的代价是**成本 ×N**（每个 solver 一个沙箱、
      一份 token）。它应该在"比赛愿意用钱换时间"时才打开，而不是比赛
      之外的日常也翻倍烧。上限 3 与调研一致：ctf-agent 用 5 个模型是
      在有充足额度的前提下，3 个已经是"速度/成本"的常见折中。
    """
    try:
        value = int(os.environ.get("SUPEX_CTF_FANOUT", "1"))
    except ValueError:
        return 1
    return max(1, min(value, 3))


async def _first_winner(tasks: dict[Any, Any]) -> Any:
    """等第一个**带答案**的结果；全都失败时返回最后一个完成的。

    为什么不是 `asyncio.wait(FIRST_COMPLETED)` 取第一个：先完成的很可能是
    "没解出来、提前收口"的那个。要把"先到的失败"熬过去，等真正带 flag 的。
    """
    pending = set(tasks)
    fallback: Any = None
    while pending:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task.exception() is not None:
                continue
            result = task.result()
            if getattr(result, "flag", None) or getattr(result, "candidate_flag", None):
                return result
            fallback = fallback or result
    return fallback
