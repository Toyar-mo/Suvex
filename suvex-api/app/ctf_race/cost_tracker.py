"""per-model 成本/用量可观测（学 ctf-agent `cost_tracker.py`）。

## 为什么要它

「多模型赛跑」的前提是**看得清每个模型花了多少**，否则「都要」会变成
烧钱黑洞。ctf-agent 用 genai-prices 查价，并给一份 FALLBACK_PRICING。
Suvex 的接入点是自建/中转网关，价格没有现成数据源，所以这里**不依赖
外部库**，只用一张可配置的单价表（每 1M token），拿不到价就记 0 并在
`unknown_models` 里列出——**绝不假装知道价格**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: 默认单价表（每 1M token，USD）。键是模型名（可含 provider 前缀）。
#: 这张表是**运营数据，不是代码事实**：换接入点/换价目时改它，别改逻辑。
#: 拿不到价的模型会被如实标记为 unknown，不会算成 0 蒙混过关。
DEFAULT_PRICING: dict[str, dict[str, float]] = {
    "deepseek-v4.1-flash": {"input": 0.10, "cached_input": 0.02, "output": 0.40},
    "deepseek": {"input": 0.10, "cached_input": 0.02, "output": 0.40},
    "deepseek-v3.1-terminus": {"input": 0.30, "cached_input": 0.05, "output": 1.20},
    "qwen3.8-27b-fp8": {"input": 0.20, "cached_input": 0.04, "output": 0.60},
    "qwen3.8-235b": {"input": 0.30, "cached_input": 0.06, "output": 1.00},
}


def resolve_pricing(
    model: str, pricing: dict[str, dict[str, float]] | None = None
) -> dict[str, float] | None:
    """按模型名找单价：先精确，再取最长子串匹配。

    拿不到价返回 `None` —— 调用方必须把这种 token 记成"未定价"，
    不能按 0 算（那会把未知成本伪装成免费）。
    """
    table = DEFAULT_PRICING if pricing is None else pricing
    if model in table:
        return table[model]
    best_key = ""
    for key in table:
        if key and key in model and len(key) > len(best_key):
            best_key = key
    return table[best_key] if best_key else None


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


@dataclass
class AgentUsage:
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    duration_seconds: float = 0.0
    cost_usd: float = 0.0


@dataclass
class CostTracker:
    """按模型汇总 token / 花费 / 缓存命中率。

    `pricing` 可被构造时覆盖（测试/换价目）。键匹配用「模型名包含关系」：
    先精确匹配，再找 pricing 里是其前缀/子串的最长键——这样
    `openai/deepseek-v4.1-flash` 也能命中 `deepseek-v4.1-flash`。
    """

    by_model: dict[str, AgentUsage] = field(default_factory=dict)
    pricing: dict[str, dict[str, float]] = field(default_factory=lambda: dict(DEFAULT_PRICING))
    unknown_models: set[str] = field(default_factory=set)

    def _resolve_price(self, model: str) -> dict[str, float] | None:
        return resolve_pricing(model, self.pricing)

    def record(
        self,
        model: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        duration_seconds: float = 0.0,
    ) -> float:
        """记录一个模型一次运行的用量，返回这一次的花费（USD）。"""
        price = self._resolve_price(model)
        cost = 0.0
        if price is None:
            self.unknown_models.add(model)
        else:
            uncached = max(0, input_tokens - cache_read_tokens)
            cost = (
                uncached * price.get("input", 0.0)
                + cache_read_tokens * price.get("cached_input", price.get("input", 0.0))
                + output_tokens * price.get("output", 0.0)
            ) / 1_000_000

        agg = self.by_model.get(model)
        if agg is None:
            agg = self.by_model[model] = AgentUsage(model=model)
        agg.input_tokens += input_tokens
        agg.output_tokens += output_tokens
        agg.cache_read_tokens += cache_read_tokens
        agg.duration_seconds += duration_seconds
        agg.cost_usd += cost
        return cost

    @property
    def total_cost_usd(self) -> float:
        return sum(a.cost_usd for a in self.by_model.values())

    @property
    def total_tokens(self) -> int:
        return sum(a.input_tokens + a.output_tokens for a in self.by_model.values())

    @staticmethod
    def _cache_rate(a: AgentUsage) -> str:
        if a.input_tokens == 0:
            return "n/a"
        return f"{a.cache_read_tokens / a.input_tokens * 100:.0f}%"

    def format_usage(self, model: str) -> str:
        """形如「45k in / 32k cached (71% hit) / 2.1k out | $0.05 | 28.3s」。"""
        a = self.by_model.get(model)
        if a is None:
            return ""
        return (
            f"{_fmt_tokens(a.input_tokens)} in / "
            f"{_fmt_tokens(a.cache_read_tokens)} cached ({self._cache_rate(a)} hit) / "
            f"{_fmt_tokens(a.output_tokens)} out | "
            f"${a.cost_usd:.2f} | {a.duration_seconds:.1f}s"
        )

    def summary(self) -> dict[str, Any]:
        """给 API/前端用的结构化汇总（每个模型 + 合计 + 未知价模型）。"""
        models = {}
        for model, a in self.by_model.items():
            models[model] = {
                "inputTokens": a.input_tokens,
                "outputTokens": a.output_tokens,
                "cachedTokens": a.cache_read_tokens,
                "cacheHitRate": self._cache_rate(a),
                "costUsd": round(a.cost_usd, 6),
                "durationSeconds": round(a.duration_seconds, 2),
                "formatted": self.format_usage(model),
            }
        return {
            "models": models,
            "totalCostUsd": round(self.total_cost_usd, 6),
            "totalTokens": self.total_tokens,
            "unknownPricedModels": sorted(self.unknown_models),
        }


__all__ = ["AgentUsage", "CostTracker", "DEFAULT_PRICING"]
