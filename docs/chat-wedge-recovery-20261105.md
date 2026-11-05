# 对话卡死（僵尸轮次）根因修复（v1.67）

> 用户报的「好像停下了，会话还在转」：侧栏显示某会话「运行中」，但模型早就不再推进，
> 一直转圈。下面记录根因、对齐 CyberStrikeAI 的取舍、修复与验证。

## 一、根因（DB 实锤）

线上有两轮对话卡在 `chat_turn_runs`：`status='running'` 但 `heartbeat_at` 冻在原地
**49~53 分钟**，`resume_attempts=0`（从未被接手）：

| 会话 | 轮次 | round | tools | 心跳停于 |
|---|---|---|---|---|
| 帮我加一个 MCP 工具连接 | `turn_01M6MS6NP9NX…` | 786 | 634 | 08:42 UTC |
| ctf | `turn_01M6N2JEY1FZ9HERVADHSQHVZ3` | 712 | 467 | 08:46 UTC |

且心跳时间**恰好等于**该轮最后一条消息的 `created_at` —— 说明这轮在写出最后一条
`process` 消息后就再没有任何进展。

**不是卡在工具**：工具执行有 `runner.DEFAULT_SHELL_TIMEOUT=300`（5 分钟）会超时。
真正卡住的是**模型流**：`engine._call_model` 里的
`async for delta in gateway.chat_stream(...)` **没有任何空闲/总超时**。当上游把
HTTP 连接挂住、只周期性发 SSE 注释行 `: keepalive`（`runner._iter_sse` 会跳过注释行、
不 yield 正文）时，`async for` 永远等下去 —— 协程不返回，也不会抛错。

**为什么后台自愈没救它**：卡死的协程仍占着**进程内** `_ACTIVE` 租约
（`turn_runs.is_active()` 恒 True）。而 resumer 的安全网 `stale_runs()` /
`claim()` 都用 `is_active()` 把「本进程在跑的」排除掉（v1.33.1 为防慢工具被误抢而
加）。于是这轮被自己的安全网挡在门外：**永远没人接手**。

## 二、对齐 CyberStrikeAI 的做法

用户要求「先看 CyberStrikeAI 怎么做的」。它的核心只有一句：
**每一次阻塞等待都必须有界（bounded agent waits），绝不允许 no-limit。**

- 调研报告 #9：blocking MCP/tool calls run in workers with **bounded agent waits**,
  resumable polling, cancellation, per-server **circuit breakers**。
- `tools_en2.json`：`agent.tool_timeout_minutes`（默认 10）到点自动停并释放资源；
  「Increase only when needed; **avoid 0 (no limit)**」；「Stop task」取消整轮。

据此设计两层（Layer 1 主修 + Layer 2 纵深），全部是「给等待加界」，没有引入新协议。

## 三、修复

### Layer 1（主修）：模型流空闲超时

`engine.py` 新增常量 `STREAM_IDLE_SECONDS`（默认 **180s**，env
`SUPEX_CHAT_STREAM_IDLE_SECONDS`，设 0 关闭仅供测试）。

把 `_call_model` 里裸的 `async for delta in gateway.chat_stream(...)` 改成手动驱动
迭代器、给**每次** `__anext__` 套 `asyncio.wait_for(..., STREAM_IDLE_SECONDS)`：

- 每个**正文**增量都会重置计时 → 真在吐字的慢模型不会被误杀。
- 连续 `STREAM_IDLE_SECONDS` 秒无正文（上游只发 keepalive 的死连接）→ 抛
  `ChatEngineError("模型流 180s 无正文增量（上游卡死）")`（`retryable=True`）。
- 抛错后：若还没吐过字 → 退回非流式（既有兜底，非流式有 httpx
  `DEFAULT_LLM_TIMEOUT=180` 兜底）；已吐过字 → 如实抛出，被 `_run_guarded_active`
  接住 → `finish_from_error` → 该轮落 `interrupted`、交给 resumer 接手。
- `finally` 里 `stream_iter.aclose()`，超时/异常时也关掉底层生成器，避免 httpx
  SSE 连接泄漏。

### Layer 2（纵深）：僵尸租约回收

`turn_runs.py` 新增 `HARD_STALE_SECONDS`（默认 **900s = 15 分钟**，远大于单轮合法
上限「一次模型调用 + 一次工具 300s」）+ 私有 `_is_hard_stale(run, now)`。

- `is_resumable` / `stale_runs` / `claim`：本进程活跃的轮次**默认**仍跳过（守住
  v1.33.1「慢工具期间别误抢」）；**但心跳超过 `HARD_STALE_SECONDS` 时放行** ——
  没有合法单步会 15 分钟不心跳，超过必是僵尸。
- `claim()` 仍用条件 UPDATE 保原子。
- 语义不变处：仅超过 `STALE_SECONDS(90)` 但未到 `HARD_STALE_SECONDS` 的活跃轮次
  依旧不可抢（`test_hard_stale_threshold_refuses_reclaim_just_below_it` 钉死）。

真正的止血是 Layer 1（不再产生僵尸）；Layer 2 是防御纵深：即便将来某个 await 点忘了
加超时，僵尸租约也会在 15 分钟后被回收。

## 四、测试（全绿）

- `tests/chat/test_turn_persistence.py` 新增 2 条：
  `test_hard_stale_reclaims_a_zombie_turn_active_in_this_process`（硬过期 + active
  → 可接手）、`test_hard_stale_threshold_refuses_reclaim_just_below_it`（仅过
  STALE 未到 HARD 的 active 仍拒）。
- `tests/chat/test_native_tool_calls.py` 新增 3 条：
  `test_stream_idle_timeout_raises_when_upstream_hangs`（吐了一点点后挂死 → 抛
  ChatEngineError 且 retryable）、`..._does_not_trip_on_slow_but_progressing_stream`
  （每帧 50ms < 阈值不误杀）、`..._falls_back_to_nonstream_when_nothing_pushed`
  （还没吐字就超时 → 退回非流式成功）。
- 全量 `tests/chat/`：**468 passed, 1 skipped**（原 461 基线，+7 新用例之外的既有
  用例全过）。

## 五、部署与线上验证

- 改动两文件 `engine.py`（sha `f06f44d5…b60c`）、`turn_runs.py`（sha `29005b19…a900`），
  `install -m 644` 进 prod `/root/suvex/suvex-api/app/chat/`，prod sha 与本地逐字节一致。
- 重启后 prod 模块 `STREAM_IDLE_SECONDS=180`、`HARD_STALE_SECONDS=900` 已生效；
  日志 `有 2 轮对话在上次进程退出时未完成，已标记为可接手` →
  `接手中断的一轮：turn_01M6MS6NP9NX…（第 1 次）`。
- **真机验证**：重启后原本冻死 49 分钟的「帮我加一个 MCP 工具连接」被 resumer 接手，
  `round_index` 0→1→6→10 持续前进、`tools_used` 635→642，日志显示它在真实调用
  CTF2 接口（`CTF2 解法侧门…`/`限流重试`）—— **不再是僵尸，而是活的**。
  截图 `artifacts/v167-sidebar.png`（侧栏该会话转圈点 + 「运行中 · 2854 条」）。
- **无回归**：新开会话跑「在沙箱里执行 echo v167-ok 并读回」→ 干净完成
  （`run_shell` 工具卡 + 单条结论「任务已经完成：run_shell 在沙箱容器里执行了
  echo v167-ok…」），截图 `artifacts/v167-answer.png`。

## 六、诚实旁支（未修，非本次范围）

真机验证时发现一个**既有**（v1.45 起）的误判：一句**完整**的自我介绍
「我是 Suvex 平台的对话助手，能…创建扫描任务…」被 `nudge.looks_like_plan` 判成
「计划句」，于是界面多出一条「（这一轮停在了下一步上…）」+「继续这一轮」黄条。
这与本次「模型流卡死」是两码事、也不是 v1.67 引入（`nudge.py` 一行未改）；纯属
计划句启发式对「能…做…」这种能力清单的误判。可作为后续独立小修（给 `looks_like_plan`
补「完整陈述/能力介绍」守卫）。
