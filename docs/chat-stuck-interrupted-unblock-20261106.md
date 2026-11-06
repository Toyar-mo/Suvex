# 会话被「死掉的 interrupted 轮次」永久卡住 —— 修复（v1.68.4）

日期：2026-11-06
报告人：用户（截图 2026-11-05，会话「千千」/ `chat_01M6P1TAYTASS6P64P85PED8MB`）

## 现象

用户看到：一轮 CTF 跑到墙钟上限 60 分钟停下（v1.68 的 `TURN_WALL_SECONDS` 正常触发，
提示「这一轮已经连续跑了超过 60 分钟……点『继续』我再接着干」），但底部有一条**红色横幅**：

> 这个对话还有一轮正在跑。想补一句话用「插话」，想让它停下或改方向用「中断」，
> 想接手已经断掉的那一轮点「继续」——它们都会接在同一轮上。

用户在输入框打「继续」再发送，被这条 409 挡住，**发不出去**；而「继续」按钮也没有
（因为这条轮次已不可接手）。于是会话彻底卡死：既不能发消息，也不能继续。

## 根因

1. 那条轮次的真实状态是 `interrupted`（不是 `running`）：
   `turn_01M6P86CPVE9ATJC6XETXZ6R6F`，round 127 / tools 437，`resume_attempts=3`。
2. 背景 resumer 接手 3 次仍失败（每次都撞上限），`resume_attempts` 到顶
   （`MAX_RESUME_ATTEMPTS=3`）→ `stale_runs` 按 `resume_attempts < max` 过滤，
   **永远不再接手**；`is_resumable` 也返回 False（没有「继续」按钮）。
3. 但它仍是 `interrupted`，落在 `OPEN_STATUSES=("running","interrupted")` 里。
   `router.send_message` 的 `TURN_ALREADY_RUNNING` 闸只看「最近一条是否是 open 状态」，
   于是把这条**已死**的轮次当成「还在跑」，用 409 永久堵住会话。

一句话：**一条已经不可能再被接手的死轮次，依然占着「正在跑」的名额把用户挡在门外。**

## 修法

纯后端（`suvex-api/app/chat/turn_runs.py` + `router.py`），不动前端。

1. 新增 `turn_runs.is_blocking_new_turn(run, *, now)`：**只有真正在跑**（`status=="running"`
   且心跳未硬过期 `HARD_STALE_SECONDS=900`）才返回 True 拦截。
   - `interrupted` 一律**不挡**——它本来就没在跑，只是「没答完、等接手」。
2. 新增 `turn_runs.is_abandoned_open(run, *, now)`：`interrupted` 且
   `resume_attempts >= MAX_RESUME_ATTEMPTS`（重试额度用尽 = 死透）。
3. `router.send_message` 的并发检查改为：
   - 是 `is_blocking_new_turn` → 仍 409 `TURN_ALREADY_RUNNING`（真正在跑时保护并发）。
   - 是 `is_abandoned_open` → 调 `turn_runs.give_up(...)` 把死轮收尾成 `failed`，
     commit 后放行新消息（新消息另起一轮）。
4. **并发检查移到内容校验之前**：它判的是会话状态、与消息内容无关；死轮收尾也不该
   因为用户一次空输入而跳过。

`give_up` 的语义本就是「重试额度用尽 → 落 failed」，这里只是把「额度已尽却没人收尾」
的漏网轮次补收尾掉。

## 测试

- `suvex-api/tests/chat/test_turns_api.py` 新增 2 条：
  - `test_abandoned_interrupted_turn_does_not_block_a_new_message`：造一条
    `interrupted + resume_attempts=3` 的死轮 → 发消息**不被 409 挡**（落到 400 空内容），
    且该轮被收尾成 `failed`。
  - `test_genuinely_running_turn_still_blocks_a_new_message`：真在跑（`running`、心跳新鲜）
    仍 409 —— 保护真正的并发。
- 原 `test_..._409`（`turn_runs.start()` 造 fresh running 轮）不变仍绿。
- 全量 `tests/chat/` **473 passed, 2 skipped**（418s）。

## 部署与线上验证

- 部署 `router.py`（sha `ea1c32a5…1e9cc0`）、`turn_runs.py`（sha `53f8650f…e316b8e`）到
  prod，prod sha 与本地逐字节一致；重启 health 200。
- 线上实测（admin，session `chat_01M6P1TAYTASS6P64P85PED8MB`）：
  - 修复前：`turn_01M6P86CPVE9…` `interrupted / resume_attempts=3`，发消息 409。
  - 修复后：发「你好」→ **不再 409**，死轮被置 `failed`（`turn_01M6P86CPVE9…~failed~127~3`），
    新轮 `turn_01M6QJDTVP4K…` 起来 `running` 并正常推进（SSE 正常出字）。
  - 浏览器（`artifacts/v1684-unblocked.png`，已 read_image 复核）：红色横幅消失、
    输入框可用、「你好」消息正常发出、模型继续执行 run_shell 步骤、那一轮结束后
    「0 运行中」、无残留转圈点。

## 影响面 / 诚实说明

- 只放宽「死掉的 interrupted 轮次」这一种情况的拦截；真正 `running` 的并发保护**原样保留**
  （有专门回归用例守着）。
- 一个 `interrupted` 但**仍可接手**（`resume_attempts < max`）的轮次，现在发消息也放行：
  新消息另起一轮，旧轮由 resumer/stale 回收处理——不再用 409 把用户按在原地。
  这在语义上是「用户主动发话了，就别再拿旧轮的 409 拦他」。
- 不改数据库 schema、不改前端契约。
