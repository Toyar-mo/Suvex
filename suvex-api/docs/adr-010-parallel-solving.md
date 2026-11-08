# ADR-010：并行解题升级（一题一任务、尽量快）

- 状态：草案（2026-11-08，依据 2026-11-08 的开源调研更新）
- 背景：比赛会一次性放开大量题，一题一个任务并行跑；目标是**更快解出、解出更多**。

## 0. 调研结论：赢家不靠"不限轮数"

查了腾讯系获奖项目（BreachWeave 第 2 届线上/线下第一、Cairn 54/54 唯一 AK、sapper-v2 第 3 届冠军 85 flags/8h、CHYing、SickHackShark、tinyctfer、Muteki）与研究系实现（Naptime、XBOW、EnIGMA、Cybench、D-CIPHER、ATLANTIS、RedAmon、Decepticon、ctf-agent）：

- **没有一家靠无限轮数取胜**。Naptime 用 16–32 步 × 20–40 条独立轨迹；EnIGMA 多数解出现在前 ~20 步；RedAmon 每题 100 iter；Muteki 每题 4000s 上限并带四类预算。
- **速度来自"并行采样 + 早停 + 提交即反馈"**，不是把一道题拖长。ctf-agent 一题同时跑 5 个模型，first verified wins；Muteki 用 easy-first + near-solve 排序。
- **成本差主要来自模型分层**：sapper 1B token 打赢亚军 4.5B；Decepticon HIGH/MID/LOW 路由；XBOW 按任务选模型。
- **验证是分水岭**：XBOW 独立 validator、Muteki provenance gate、ctf-agent 提交冷却；没有验证的高分不可信。

所以本 ADR 把目标从"不限轮数"修正为：**一题一任务、每题多重预算、同题并行采样、先到先得**。

## 1. 本轮实测（2026-11-08，DASCTF Pro 四道纯附件题）

4 道题跑 60 分钟、0 候选；期间暴露并修复的工程阻塞：

| 问题 | 影响 | 状态 |
|---|---|---|
| 附件下载只认 CTF2 官方域名 | 题面文件根本没下发，白跑 20 分钟 | 已修（平台注册域 + `attachment_domains`） |
| 单题链路不写活动事件 | "在跑但没记录" | 已修 |
| 收口门要求覆盖 5 面 + 1 条发现 | CR7 4.5 小时被拒 100 次，逼模型编造 | 已修（有候选或有 tried 即放行） |
| `race-*` 沙箱泄漏 813 个 | `docker run` 超时，四题一起失败 | 已修（启动清理 + 年龄清理） |
| 并发写死 4 | 一题一任务铺不开 | 待改（P0） |

## 2. 目标架构（照抄 + 适配）

### A · 调度（照抄 Muteki `CompetitionScheduler`）

- 每题一行队列：`priority / enqueued_at / difficulty / points / solved_by / near_solve / attempt / state`
- 1–5s tick，输出 `admit / hold / defer / observe`，每次决策落审计表
- 排序键：`operator_priority → starvation → difficulty(easy first) → near_solve → points → enqueue_time`
- 失败退避 300s；连续 stuck 换轨或放弃

### B · 并发与沙箱（照抄 ctf-agent + SWE-bench 基线）

- 一 attempt 一沙箱；镜像预构建、启动走 semaphore
- 并发上限 = `min(配置, 0.75×CPU, 内存/每沙箱内存, 容器上限)`；每沙箱实测 100–200MB
- 孤儿清理按 label + 年龄（已实现年龄清理，补 label）

### C · 同题 fan-out（照抄 ctf-agent `ChallengeSwarm`）

- 每题默认 **2–3 个异构 solver**（不同模型/温度），`asyncio.wait(FIRST_COMPLETED)`
- 任一 solver 产出**已验证**候选 → 立即取消其余
- 每个 solver 独立预算；不共享可写容器

### D · 预算（照抄 Muteki `ExecutionBudget`）

四类预算同时生效，**0 不等于无限**：

1. 墙钟 deadline（monotonic + UTC 双记录；默认 60–90 分钟）
2. 步数（默认 120）
3. token / cost
4. 提交次数（平台配额）

执行/结论/收尾三段切分，最后 10% 预算强制收口。

### E · 熔断（照抄 ctf-agent `LoopDetector` + RedAmon axis lock-in）

- 滚动窗口 12 步：warn 3、break 5（同一命令/同一路径/同一 CVE）
- 状态无增量也算空转；触发后注入 sibling findings 并换轴

### F · 提交（照抄 Muteki provenance gate + ctf-agent 冷却）

- 只有**执行输出 / 产物 / 运行事件**可成为候选；operator/chat/advisor 一律拒绝
- 状态机：`candidate → awaiting_approval → approved → submitted`
- 同题同槽同值只登记一次；错答冷却 30/120/300/600s 递增
- `unknown/timeout` 只对账不重发；`wrong` 写回记忆，避免二次提交

### G · 记忆与交接（照抄 nvwabase / BreachWeave Observer）

- 外置三层：`title / summary / evidence`；默认只读摘要，按需下钻
- compact 或超时时生成 `compact_handoff.md`，由轻量 agent 写，不让做题 agent 自己写长笔记

### H · 模型分层（照抄 sapper-v2 / Decepticon）

- 强模型：规划、关键推理（格、逆算法）
- 快模型：侦察、解包、日志摘要
- 每 provider 一个 semaphore；429/quota 自动 fallback

## 3. 分阶段

| 阶段 | 内容 | 验收 |
|---|---|---|
| P0 | 并发按资源算 + 四类预算 + 容器上限联动 | 20 题同时起跑无 `docker run` 超时；单题不再跑到 500 轮 |
| P1 | 调度器（easy-first/near-solve/starvation）+ 审计表 | 同批题平均首次提交时间下降 |
| P2 | 同题 2–3 solver fan-out + first-wins | 同一批题解决率上升 |
| P3 | 提交 provenance gate + 冷却 + 模型分层 | 错答提交次数下降、token/题下降 |

## 4. 不做

- 不共享可写沙箱跨题复用（隔离）
- 不用"无限轮数"当解法（调研无先例；拖长只增成本）
- 不做无上限并发（CHYing 3 并发就撞 429；ctf-agent 50 容器已很重）
