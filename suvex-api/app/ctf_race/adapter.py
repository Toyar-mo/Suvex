"""把 Suvex 的解题引擎接到 `swarm` 的驱动无关编排上（胶水层）。

## 分工

* `swarm.py`（纯逻辑）只认 `solve_one(model, *, hint) -> SolverResult`。
* 本模块负责把「跑一个模型」实现成：**建 spec → 起沙箱 → `solve_in_sandbox`
  → 把 `SolveOutcome` 翻译成 `SolverResult`**。
* 提交走 `competition.submit`（已剥外壳 `strip_flag_shell`）。

## 为什么用「注入工厂」而不是在这里硬编码建沙箱

真实建沙箱要用 `SandboxContainer(...)` + 镜像名 + 附件目录 + 工作区目录 +
`LlmGateway`——这些在测试里跑不起来（要 Docker）。所以本模块的
`build_solve_one()` 接收两个工厂：

    spec_factory(model) -> SolveSpec      # adapter 默认由 competition 题构造
    sandbox_factory(spec) -> SandboxContainer

测试注入假工厂即可验证**翻译逻辑**（SolveOutcome→SolverResult 的状态映射、
candidate 提取、token 记账），不需要真的起容器。生产由
`competition_racer.py` 提供真工厂。

## 状态映射（`SolveOutcome` → `SolverStatus`）

| SolveOutcome 情况 | SolverResult.status |
|---|---|
| 有被平台确认的 candidate（submitted 里有 accepted&solved） | FLAG_FOUND |
| error 非空（且是额度类） | QUOTA_ERROR |
| error 非空（其它） | ERROR |
| 没胜、无 error、有 turns | GAVE_UP |
| 没胜、无 error、0 turns（没真跑） | GAVE_UP（is_broken 由 step/cost 判） |

⚠️ `SolveOutcome` **不认识**「平台确认」这件事本身——`submitted` 只是候选
flag 文本列表，`_handle_candidates` 才真正提交。所以本 adapter 的 `solve_one`
**自己**负责提交（通过 `submit_fn`），并把提交结果带回 `SolverResult`。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.ctf_race.solver_base import (
    ERROR,
    FLAG_FOUND,
    GAVE_UP,
    QUOTA_ERROR,
    SolverResult,
)

logger = logging.getLogger(__name__)

#: 建 spec 的工厂：给定模型标识，返回一个 `SolveSpec`（runner.SolveSpec）。
SpecFactory = Callable[[str], Any]
#: 起沙箱的工厂：给定 spec，返回一个已 start 的 `SandboxContainer`。
SandboxFactory = Callable[[Any], Any]
#: 提交函数：(flag, model) -> (给模型看的回执, 是否确认)。
SubmitFn = Callable[[str, str], Awaitable[tuple[str, bool]]]

#: 额度类错误的特征串——命中则判 QUOTA_ERROR，让 adapter 可换网关重试。
_QUOTA_MARKERS = (
    "quota", "rate limit", "rate_limit", "429", "insufficient", "额度",
    "余额", "no capacity", "overloaded", "exceeded",
)


def _looks_quota(err: str) -> bool:
    low = (err or "").lower()
    return any(m in low for m in _QUOTA_MARKERS)


@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0

    def add(self, prompt: int, completion: int) -> None:
        self.input_tokens += prompt
        self.output_tokens += completion


def _collect_usage(outcome: Any) -> _Usage:
    """从 `SolveOutcome.usages`（(turn, model, label, TokenUsage)）汇总。"""
    usage = _Usage()
    for item in getattr(outcome, "usages", []) or []:
        # 兼容 (turn, model, label, TokenUsage) 四元组
        tk = item[3] if len(item) >= 4 else None
        if tk is None:
            continue
        usage.add(getattr(tk, "prompt_tokens", 0), getattr(tk, "completion_tokens", 0))
    return usage


def _extract_candidate(outcome: Any) -> str | None:
    """从 outcome 取一个「看起来像 flag 的候选」。

    优先级：`candidates` 里第一个非空 `value`；退而取 `submitted` 里最后一个。
    ⚠️ 这里**只提取、不判定**——是否是平台的 flag 由 `submit_fn` 说了算。
    """
    for cand in getattr(outcome, "candidates", []) or []:
        val = getattr(cand, "value", None) or getattr(cand, "flag", None)
        if val:
            return str(val).strip()
    submitted = getattr(outcome, "submitted", []) or []
    for s in reversed(submitted):
        if s and str(s).strip():
            return str(s).strip()
    return None


def _summarize(outcome: Any) -> str:
    """把 outcome 浓缩成一段给兄弟模型看的 findings（负结果/进展都算洞察）。"""
    bits: list[str] = []
    err = getattr(outcome, "error", None)
    if err:
        bits.append(f"已尝试但出错：{str(err)[:200]}")
    turns = getattr(outcome, "turns", 0) or 0
    bits.append(f"跑了 {turns} 轮")
    rejected = getattr(outcome, "rejected", []) or []
    if rejected:
        bits.append("被平台判错的候选：" + "、".join(str(r)[:40] for r in rejected[:5]))
    transcript = getattr(outcome, "transcript", "") or ""
    if transcript:
        tail = transcript.strip().splitlines()[-6:]
        bits.append("最近过程：\n" + "\n".join(tail))
    return "\n".join(bits)[:1500]


class SolveOneAdapter:
    """把「跑一个模型」实现成 spec→sandbox→solve_in_sandbox→result 的调用。

    注入：
      * `spec_factory(model)` —— 建 SolveSpec；
      * `sandbox_factory(spec)` —— 起沙箱（返回已 start 的对象，有 `.stop()`）；
      * `submit_fn(flag, model)` —— 提交（通常来自 swarm 的 try_submit_flag 包装）；
      * `runner` —— async 的 `solve_in_sandbox`（默认就地 import，测试可替换）；
      * `cost_tracker` —— 记账（可选）；
      * `system_prompt_builder(spec)` —— 生成系统提示词。
    """

    def __init__(
        self,
        *,
        spec_factory: SpecFactory,
        sandbox_factory: SandboxFactory,
        submit_fn: SubmitFn | None = None,
        runner: Callable[..., Awaitable[Any]] | None = None,
        cost_tracker: Any = None,
        system_prompt_builder: Callable[[Any], str] | None = None,
    ) -> None:
        self._spec_factory = spec_factory
        self._sandbox_factory = sandbox_factory
        self._submit_fn = submit_fn
        self._runner = runner
        self._cost_tracker = cost_tracker
        self._system_prompt_builder = system_prompt_builder

    async def _run(self, spec: Any, sandbox: Any, gateway: Any, system_prompt: str) -> Any:
        """跑解题循环；runner 未注入时就地 import 真实的。"""
        if self._runner is not None:
            return await self._runner(spec, sandbox=sandbox, gateway=gateway, system_prompt=system_prompt)
        from app.kernel.runner import solve_in_sandbox

        return await solve_in_sandbox(spec, sandbox=sandbox, gateway=gateway, system_prompt=system_prompt)

    async def __call__(self, model: str, *, hint: str | None) -> SolverResult:
        spec = self._spec_factory(model)
        # hint 作为一次人类线索注入：套进已有的 hint_provider 机制最省。
        if hint:
            existing_brief = getattr(spec, "brief", "") or ""
            note = (
                "【其它模型（兄弟）的进展/提示，供你参考、别重复死路：】\n" + hint
            )
            try:
                spec.brief = (existing_brief + "\n\n" + note).strip() if existing_brief else note
            except Exception:  # spec 可能是 frozen dataclass 之类
                logger.debug("无法把 hint 写入 spec.brief（忽略）")

        sandbox = None
        try:
            sandbox = self._sandbox_factory(spec)
            gateway = self._make_gateway(spec)
            system_prompt = (
                self._system_prompt_builder(spec)
                if self._system_prompt_builder
                else getattr(spec, "system_prompt", "") or ""
            )
            outcome = await self._run(spec, sandbox, gateway, system_prompt)
            return await self._translate(outcome, model)
        except Exception as exc:  # 单模型崩溃不该拖垮整场
            logger.warning("[ctf_race] model=%s 求解异常: %s", model, exc)
            err = str(exc)
            return SolverResult(
                model=model,
                status=QUOTA_ERROR if _looks_quota(err) else ERROR,
                findings_summary=f"求解异常：{err[:300]}",
            )
        finally:
            if sandbox is not None and hasattr(sandbox, "stop"):
                try:
                    sandbox.stop()
                except Exception:  # noqa: BLE001
                    logger.debug("沙箱停止失败（忽略）", exc_info=True)

    def _make_gateway(self, spec: Any) -> Any:
        from app.kernel.runner import LlmGateway

        return LlmGateway(
            model=spec.llm_model,
            api_key=spec.llm_api_key,
            base_url=spec.llm_api_base,
            label=getattr(spec, "llm_label", ""),
            kind=getattr(spec, "llm_kind", "cloud"),
        )

    async def _translate(self, outcome: Any, model: str) -> SolverResult:
        """把 SolveOutcome 翻译成 SolverResult（含提交）。"""
        usage = _collect_usage(outcome)
        turns = int(getattr(outcome, "turns", 0) or 0)
        error = getattr(outcome, "error", None)

        cost = 0.0
        if getattr(self._cost_tracker, "record", None) is not None:
            cost = self._cost_tracker.record(
                getattr(outcome, "_model_label", None) or model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_tokens,
            )

        # 先试提交候选 flag（如果配了 submit_fn）
        candidate = _extract_candidate(outcome)
        if candidate and self._submit_fn is not None and not error:
            display, confirmed = await self._submit_fn(candidate, model)
            if confirmed:
                return SolverResult(
                    model=model,
                    status=FLAG_FOUND,
                    flag=candidate,
                    findings_summary=f"确认 flag：{candidate}",
                    step_count=turns,
                    cost_usd=cost,
                    candidate_flag=candidate,
                )

        # 没确认（或没候选）：按错误/正常判
        if error:
            status = QUOTA_ERROR if _looks_quota(str(error)) else ERROR
        else:
            status = GAVE_UP
        return SolverResult(
            model=model,
            status=status,
            flag=None,
            findings_summary=_summarize(outcome),
            step_count=turns,
            cost_usd=cost,
            candidate_flag=candidate,
        )


__all__ = ["SolveOneAdapter", "SandboxFactory", "SpecFactory", "SubmitFn"]
