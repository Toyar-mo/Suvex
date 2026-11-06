"""比赛级 CTF 自动托管（学 verialabs/ctf-agent 的 coordinator + swarm 架构）。

## 为什么单独一个包

Suvex 原来是「人工点一道题 → 单模型解」(`service.start_solve`)。而成熟的
比赛级系统（ctf-agent）是「coordinator 管整场 + 每题一个 solver swarm，
多模型并行赛跑、先得 flag 者胜」。这两者是**正交**的能力：

* `app/kernel/multiagent.py` = **一道题内**多 agent 纵向拆分（侦察/验证）。
* 本包 `app/ctf_race/` = **多模型打同一道题**横向冗余赛跑 + 整场调度。

本包刻意做成**驱动无关**：`swarm.py` 不认识 Suvex 的沙箱、网关、比赛平台，
它只接收一个「跑一个模型」的 async callable（`SolveOneFn`）。真正的接线
（把 Suvex 的 `solve_in_sandbox` + `competition.submit` 塞进去）在
`adapter.py`。这样核心编排可以脱离 Docker/DB 做纯单元测试 —— 这是把
ctf-agent 的成熟设计搬过来时，唯一能保证「改了不炸」的拆法。

## 六个搬过来的设计（见 `docs/ctf-agent-analysis-20261106.md`）

1. 多模型赛跑首胜（`swarm.ChallengeSwarm`）
2. bump 循环：卡住时喂兄弟洞察重跑，递增冷却、连续 3 错才弃
3. 提交安全：per-model 递增冷却 + 去重 + 锁 + ALREADY SOLVED 短路
4. quota/失败回退（本包用「换 gateway 续跑」表达，由 adapter 提供）
5. 工具级循环检测（`loop_detect.LoopDetector`）
6. per-model 成本可观测（`cost_tracker.CostTracker`）

## 与 ctf-agent 的差异（刻意）

* 不抄它的 `QUOTA_FALLBACK`/订阅/API 双后端——Suvex 只有 OpenAI/Anthropic
  兼容网关，回退=换 `LlmGateway`（adapter 决定）。
* 不抄它 Coordinator prompt 里「Cost is not a concern」「NEVER kill a
  swarm」这类关掉刹车的强断言——本包里所有预算/冷却/上限都是硬约束。
"""

from __future__ import annotations

__all__ = [
    "cost_tracker",
    "loop_detect",
    "message_bus",
    "solver_base",
    "swarm",
]
