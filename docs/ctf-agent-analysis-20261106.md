# verialabs/ctf-agent 深度分析 + Suvex 可借鉴清单（v1.72 调研）

日期：2026-11-06
分析对象：Kali `/home/kali/ctf-agent`（git origin `https://github.com/verialabs/ctf-agent.git`，HEAD `3366d56`，Python 3.14+）

---

## 一、它是什么（一句话）

**一个「比赛级」CTF 自动解题系统**：一个 **Coordinator LLM** 坐镇管理整场比赛，为每道未解出的题拉起一个 **Solver Swarm**（同一道题、**多个不同厂商的模型同时赛跑**，谁先拿到 flag 谁赢），每个 solver 在隔离 Docker 沙箱里自带全套 CTF 工具。它不是"一个 agent 解一道题"，而是"一队 agent 抢一整个赛场"。

整体框架：pydantic-ai（Pydantic AI，Python agent 框架）+ Claude Agent SDK + OpenAI Codex 三套后端 + aiodocker（原生 async Docker）。

---

## 二、架构总览

```
CTFd 平台
   │  ← 每 5s 轮询（poller.py）：发现新题 / 新解出
   ▼
Coordinator LLM（claude_coordinator.py / codex_coordinator.py）
   工具：fetch_challenges / get_solve_status / spawn_swarm / check_swarm_status /
        submit_flag / kill_swarm / bump_agent / broadcast / read_solver_trace
   职责：按 solve 数排优先级、给卡住的 solver 写"针对性 bump"、广播跨 solver 洞察
   │
   ├─ Swarm: challenge-1   （ChallengeSwarm，swarm.py）
   │     ├─ Solver: claude-opus-4-6/medium   ── Docker 沙箱
   │     ├─ Solver: claude-opus-4-6/max
   │     ├─ Solver: gpt-5.4
   │     ├─ Solver: gpt-5.4-mini
   │     └─ Solver: gpt-5.3-codex
   ├─ Swarm: challenge-2 ...
   └─ Swarm: challenge-N ...（max_concurrent_challenges=10）
```

后端文件（`backend/`，共 4754 行）：
| 文件 | 行 | 职责 |
|---|---|---|
| `agents/swarm.py` | 348 | **每题一个 swarm**：N 模型并行赛跑、bump 循环、提交冷却、quota 回退 |
| `agents/solver.py` | 304 | pydantic-ai solver（API 后端：bedrock/azure/zen/google） |
| `agents/claude_solver.py` | 385 | Claude Agent SDK solver（订阅制，bash-only） |
| `agents/codex_solver.py` | 557 | Codex App Server solver |
| `agents/coordinator_loop.py` | 280 | **共享事件循环**：poller 事件 → coordinator turn；自动开/杀 swarm；operator 消息端点 |
| `agents/coordinator_core.py` | 180 | coordinator 工具实现（spawn/trace/bump/broadcast…） |
| `agents/claude_coordinator.py` | 177 | Claude 版 coordinator（MCP server + allowlist hook） |
| `agents/codex_coordinator.py` | 351 | Codex 版 coordinator |
| `message_bus.py` | 54 | **跨 solver 共享 findings**（append-only + per-model cursor） |
| `loop_detect.py` | 59 | **循环检测**：同一 tool_call 签名 deque(window) 上重复 ≥3 warn / ≥5 break |
| `cost_tracker.py` | 232 | **按模型/agent 的 token + $ + 缓存命中率**统计（genai-prices） |
| `solver_base.py` | 39 | 状态常量 + `SolverResult` + `SolverProtocol` |
| `poller.py` | 125 | CTFd 5s 轮询，出 `new_challenge` / `challenge_solved` 事件 |
| `sandbox.py` | 295 | aiodocker 沙箱（每条命令独立 exec、并发信号量、孤儿容器清理） |
| `tools/core.py` | 231 | SDK 无关工具逻辑（bash/read/write/submit/web_fetch/webhook/vision） |
| `prompts.py` | 181 | solver 系统提示词（按类别/连接方式/附件动态拼） |
| `models.py` | 157 | 模型解析（bedrock/azure/zen/google）+ 上下文窗口/vision 表 |

---

## 三、最值得学的 6 个设计（含它踩过的坑）

### 1. 多模型赛跑（swarm）—— 最核心的差异化
`ChallengeSwarm.run()`：每道题拉起 N 个模型各自的 solver，`asyncio.wait(FIRST_COMPLETED)`，**任一模型找到 flag → `cancel_event.set()` 取消其余**。
- 价值：不同模型在不同题型上强弱不同（Opus 强逆向、GPT 强 web），赛跑把"选哪个模型"的赌博变成"都要"。
- `SolverResult` 带 `status`（flag_found/gave_up/cancelled/error/quota_error）区分结局。

### 2. Bump 循环（不放弃 + 喂 sibling 洞察）
`_run_solver_loop`：solver 若 `gave_up/error`，**不是直接判死**，而是：
- 收集其它模型的 findings（`_gather_sibling_insights`），`solver.bump(insights)` 注入一段"上一轮没找到，这是别的 agent 的洞察，换个思路别重复"；
- **递增冷却**：`min(bump_count*30, 300)` 秒再 bump，避免空转烧钱；
- **连续 3 次 ERROR 才真放弃**；`0 step 且 $0` 视为 broken（环境坏了）不 bump。
- 收益：单模型"撞墙就停"的问题，被"兄弟的进展 + 冷却后再来"救活。

### 3. 提交安全（escalating cooldown + 去重 + 锁）
`try_submit_flag`：**per-model 递增冷却** `[0,30,120,300,600]` 秒（猜错越多等越久）、全局 `_submitted_flags` 去重、`_flag_lock` 串行、`ALREADY SOLVED` 短路。
- 价值：CTF 平台对错误提交有次数限制（很多题只有 3~50 次），**瞎猜 flag 会烧掉真机会**。这是"多模型并行"场景下必须的刹车片。

### 4. Quota 回退（订阅耗尽 → API 兜底，复用沙箱）
`QUOTA_FALLBACK`：`claude-sdk/opus` → `bedrock/opus`、`codex/gpt-5.4` → `azure/gpt-5.4`。solver 遇 `QUOTA_ERROR` 时**保留沙箱**、换 API 后端重开一个 pydantic solver 续跑（`owns_sandbox=True` 防旧 solver.stop 拆掉容器）。
- 价值：订阅制（Claude Max / ChatGPT 订阅）跑比赛会撞速率墙，秒切 API 不丢已侦察的沙箱状态。

### 5. 循环检测（tool 签名）
`LoopDetector`：把 `tool_name + json(args)[:500]` 压成签名，deque(window=12)，**同一签名出现 ≥3 次 warn（往结果里塞警告文案）、≥5 次 break（直接拦掉这次调用、返回警告）**。
- 价值：与 Suvex 已有的「重复动作」闸同源，但它是**在工具层直接拦截并注入"换个思路"文案**（含具体建议："grep 换成 python 脚本"），比我方只在轮次收尾判重更早、更细。

### 6. 成本可观测（per-model token/$/缓存命中）
`CostTracker`：genai-prices 算价 + FALLBACK_PRICING（按 1M token），`format_usage` 出「45k in / 32k cached (71% hit) / 2.1k out | $0.05 | 28.3s」，按 model 聚合。
- 价值：多模型赛跑**必须**能看清"哪个模型花了多少钱"，否则"都要"会变成烧钱黑洞。

### 它踩的坑（我方要避开）
- **Claude SDK solver 是 bash-only，没有 `submit_flag` 具名工具** → trace 里模型在 bash 里猜 `submit_flag 'flag{test}'` 命令、满世界 `find submit_flag`（真事故）。教训：**具名工具 vs bash-only 要显式对齐**，否则模型会幻觉命令行工具。它用 `has_named_tools` 参数在 prompt 里分两套说法来救。
- **Coordinator 极端 prompt**：「NEVER kill a swarm」「Cost is not a concern」——等于关掉成本闸。我方不能照抄这种强断言（会烧穿预算）。

---

## 四、Suvex 现状（已有什么，缺什么）

**已有（不重复造）**：
- `app/kernel/multiagent.py`（3167 行）：**单题内** root + N child 多 agent 编排（学 strix），共享 notes/coverage、`agent_finish` 结构化汇总。这是"**一道题拆多个子任务并行**"。
- `runner.solve_in_sandbox`：单模型 ReAct 循环 + 可选 multi_agent（`enable_multi_agent` 开关，默认关）。
- 重复动作闸（v1.69 同族）、wall-clock 上限、`max_wrong_submissions` 平台次数限制、`KernelSolveUsage` 用量轨迹、`LlmGateway.failover` 换接入点。
- 原生 tool-calling（v1.64 起）。

**缺口（ctf-agent 恰好补上）**：
| 能力 | Suvex 现状 | ctf-agent | 价值 |
|---|---|---|---|
| **多模型赛跑同题** | ❌ 单模型（换模靠 failover 串行） | ✅ N 模型并行首胜 | 高（不同模型强项互补） |
| **sibling 洞察注入** | ❌ 无跨 agent findings 总线 | ✅ message_bus + bump | 中 |
| **per-model 成本追踪** | ⚠️ 有 usage 轨迹但无「按模型 $ 汇总」 | ✅ CostTracker | 中高（赛跑前提） |
| **递增提交冷却** | ⚠️ 只按平台剩余次数，无"猜错越久等" | ✅ [0,30,120,300,600] | 中（防烧机会） |
| **quota→API 回退** | ⚠️ 只有 failover 换点，无"订阅耗尽切 API 续沙箱" | ✅ QUOTA_FALLBACK | 中 |
| **工具层循环拦截** | ⚠️ 轮次级判重 | ✅ 工具级 warn/break + 具体建议 | 低中 |

**关键区别（别搞混）**：
- Suvex `multiagent` = **一道题内**多 agent（纵向拆分：侦察/验证A/验证B）。
- ctf-agent swarm = **多模型打同一道题**（横向冗余：不同模型赛跑首胜）。
- 二者**正交**，可叠加：一道题既可以多模型赛跑、每个模型内又可用多 agent 拆分。

---

## 五、可迁移方案（按价值/风险分档）—— 待用户选范围

### 方案 A（推荐，中等工作量，纯增量）：**多模型赛跑 + per-model 成本**
- 新增 `app/kernel/race.py`：`race_models(challenge, model_specs, ...)` — 对同一 `ChallengeSolveSpec` 拉 N 个 `solve_in_sandbox`（各自 `LlmGateway`），`asyncio.wait(FIRST_COMPLETED)`，首胜取消其余。
- `KernelSolveUsage` 已有按 `provider_label` 聚合 → 前端显示"每个模型花了多少/谁先找到"。
- 不改现有单模型路径；只在 `enable_multi_agent` 之外新增 `enable_model_race` 开关（默认关，用户开）。
- 复用现有 sandbox、submit、failover；风险最低。

### 方案 B（高价值，中高工作量）：A + **sibling 洞察总线 + bump 循环**
- 加 `message_bus`（append-only findings + cursor）与 `bump`：某模型 gave_up 时把兄弟进展注入重跑，递增冷却、连续 3 错才弃。
- 需在 `solve_in_sandbox` 暴露"注入一段外部提示再续"的入口（现有 `resume` 机制可复用）。

### 方案 C（纯健壮性，低工作量）：**工具级循环拦截 + 递增提交冷却**
- 把 ctf-agent 的 `LoopDetector`（warn/break + 具体建议文案）移植到 `runner.dispatch_tool` 外层；把递增提交冷却加到自动提交路径。
- 与现有重复动作闸互补，不改架构。

### 方案 D（重构级，高风险，暂不建议）：coordinator + swarm 的"打整场"模式
- 完整照搬 poller+coordinator+swarm。Suvex 的 `start_solve` 是"人工点一道题"，比赛驱动的"自动开全赛场"是另一套产品形态，工作量最大、最易引入新 bug。**除非用户明确要「赛事自动托管」**，否则不碰。

---

## 六、结论

ctf-agent 最有价值、Suvex 最缺、且与现有架构正交的，是**「多模型赛跑同一道题」+「per-model 成本追踪」**（方案 A），其次是把它的**循环拦截细节 + 递增提交冷却**（方案 C）作为健壮性补丁吸收。B 是 A 的自然延伸。D 是另一套产品，不建议现在做。

落地时坚持 Suvex 既有纪律：原生 tool-calling 不退回文本协议、per-model 成本必须可见、不抄「Cost is not a concern」这类关掉刹车的强断言。
