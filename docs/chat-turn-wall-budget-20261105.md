# 单轮墙钟预算：让「合法但极难的长任务」也有界（v1.68）

> 承接 v1.67（模型流空闲超时 + 僵尸租约回收）。v1.67 治好了「卡死冻结」；
> v1.68 治「**没卡死但一直不停**」——一个合法但极难的任务会在没有任何总上限的
> 情况下烧几个小时，用户点「中断并继续」也只是让它接着无限跑。

## 一、现象与根因

线上会话 `ctf` 的轮次 `turn_01M6N2JEY1FZ9HERVADHSQHVZ3`：
- `started_at == heartbeat_at` 差 **22732 秒 ≈ 6.3 小时**，`updated_at` 只落后 8 秒
  —— 也就是它**没卡死**，一直在跑，但一轮跑了 6 个多小时。
- 累计 **214 轮 / 629 次工具 / 269 次纠偏**。
- 逐步看消息，每步都是**不同**的真实 `run_shell`（dump.py、读 GOT/`__free_hook`、
  堆布局、试 `system`）—— 是在正经做一道极难的堆 pwn，不是原地打转。

于是既有的三道闸全都管不住它：
- `MAX_REPEATED_ACTIONS=5`：只抓「同一条命令连续重复」，这里每步都变，不触发。
- `MAX_TOOL_NUDGES=300`：管的是「跑过工具却只写人话」的**纠偏**次数，不管**动作**
  次数。
- `MAX_TOOL_ROUNDS=100000`：实际等于没上界。

结果：**一个合法任务可以在无人管的情况下烧到底**。用户观感「一直不停」，
点「中断并继续」反而只是又给一段（同样无界的）续跑。

## 二、对齐 CyberStrikeAI

CyberStrikeAI 的 `agent.tool_timeout_minutes`（默认 **10 分钟**）对一次任务设
**总时长上界**，到点自动停并释放资源，明确写着「**avoid 0 (no limit)**」。
这正是缺的那一层：不是限制「单次等待」（v1.67 的 STREAM_IDLE 已做），而是限制
**整轮任务的总墙钟**。

## 三、修复

`engine.py` 新增常量 `TURN_WALL_SECONDS`（默认 **3600s = 1 小时**，env
`SUPEX_CHAT_TURN_WALL_SECONDS`，设 0 关闭仅供测试/特殊部署）。

在 `run_turn` 与 `resume_approved` 的**每轮循环开头**设一道闸：
- 本调用记录 `turn_wall_start = time.monotonic()`；
- 每轮开始判 `time.monotonic() - turn_wall_start >= TURN_WALL_SECONDS` 且
  `tools_used > 0`（纯问答不受影响）；
- 命中就**优雅收尾**：落一句 `render_turn_wall_note(...)`（"这一轮已经连续跑了
  超过 N 分钟，为不让它无上限地一直跑，我先停下、释放了环境。任务还没结束的话，
  点「继续」我再接着干——已经做过的步骤不会重来。"），落 `interrupted`（
  `needs_continue=True`，可接手/可续跑），`return`。

设计取舍：
- **按调用计**（每次 `run_turn`/`resume_approved` 从 0 起算）：用户点「继续」是
  新的一次调用，拿一整段新预算。这样既不失控，也不一刀切死真正的长任务——由人
  决定要不要继续给时间，而不是机器替它一直烧下去。
- **不硬砍**：落一句可见的实话、把控制权交回用户，而不是静默中断或假装是结论。
- **3600s 而非 CyberStrikeAI 的 10 分钟**：CTF 里的活用 10 分钟往往真不够；
  1 小时既能兜住"忘了停"，又不至于把正常的长活正常地腰斩。可 env 调。

## 四、测试

`suvex-api/tests/chat/test_native_tool_calls.py`：
- `test_render_turn_wall_note_mentions_minutes_and_continue`：提示含「N 分钟」
  「继续」，且最小不低于 1 分钟。
- `test_run_turn_stops_gracefully_when_wall_budget_exceeded`：网关每轮都发一个
  可执行 ACTION（复刻 ctf「每步都不同」形态），`TURN_WALL_SECONDS=1` + 假
  `monotonic` 每调 +10s → 断言 `needs_continue=True`、`hit_tool_limit=True`、
  末条消息含「分钟/继续」、且 `gateway.calls <= 3`（**证明不是跑满 300 轮，而是
  到点立刻收手**）。

全量 `tests/chat/`：**470 passed, 1 skipped**（原 468 基线 + 2 条 v1.68 用例）。

## 五、部署与验证

- 改动两文件 `engine.py`（sha `bb67bc7a…2510`）、`prompts.py`（sha `a4b65b03…1913`），
  `install -m 644` 进 prod `/root/suvex/suvex-api/app/chat/`，prod sha 与本地逐字节一致。
- 重启后 health 200；prod `.venv` import 确认 `TURN_WALL_SECONDS=3600`、
  `STREAM_IDLE_SECONDS=180` 已生效。
- 重启把原本已跑 6+ 小时的那轮（`turn_01M6N2JEY…`，重启时已到 round 340/tools 722）
  标为 `interrupted`（启动日志「有 2 轮对话在上次进程退出时未完成，已标记为可接手」）
  —— 它不再无界自跑；用户点「继续」会拿一整段新的 1 小时预算。
- **无回归**：新会话发「1+1 不要调工具」→ `done` / round 0 / tools 0 / 答「2。」，
  纯问答不受墙钟闸影响。
- **闸本身的行为**由单测 `test_run_turn_stops_gracefully_when_wall_budget_exceeded`
  确定性验证（每轮都发 ACTION 的形态下，到点即收手、`needs_continue=True`）。
  生产 1 小时闸不可能在验证窗口内自然触发，故以单测钉死其语义。
