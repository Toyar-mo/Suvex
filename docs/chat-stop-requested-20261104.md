# 对话「彻底停止」的诚实回执（v1.62）

日期：2026-11-04 ｜ 类型：前端 UX 修复（纯前端，无后端改动）

## 用户报的问题

用户 m07789：`修，把对话的bug都修了，照抄`。承接连夜修的 v1.61，继续扫对话页的真实 bug。

线上实测（Playwright，viewer `zzviewer88`，会话 `chat_01M6JD8D19B7VXJ9R193YZYZGY`）复现的这一条：

1. 让模型跑 `sleep 90; echo done999`（长任务）；
2. 任务跑到一半点「中断」→ 弹「中断并继续 / 彻底停止」对话框；
3. 点「彻底停止」；
4. **屏幕上又冒出「服务端仍在后台推进这一轮……可以在这里等，或点『中断』停下」** ——
   用户刚点了停止，界面却像没收到，还劝他再点一次中断。

## 根因

`stop` 的服务端语义是「下一个检查点停」——如果这一轮正卡在一个**跑着的工具**里，
子进程没法中途打断（见 `suvex-api/app/chat/interrupts.py` 的「时间点」说明：
工具执行中无法打断，等工具返回才生效）。

而前端在点完「彻底停止」后，立刻把本地 `sending` 清成 `false`（"真的停了"的乐观假设）。
可服务端这一轮仍是 `running`（工具还在跑），v1.61 的轮询 `pollTurnProgress` 随后发现
`serverTurn` 仍 `running`，于是把刚才那条「服务端仍在后台推进……点『中断』停下」的蓝条
**又画了回来**。状态其实是自洽的（服务端正确会等工具返回再停），但**界面没承认"停止
请求已收到"**，观感像停止按钮坏了。

## 修法（照抄 CyberStrikeAI 的 cancel 语义：cancelling）

给 store 加一个明确的「已请求停止」标记 `stopRequested`，在"停止请求已发出、
但这一轮还没真正停下"的窗口期里，把进度条从「催你再点中断」改成「已请求停止，等当前工具返回」。

### `suvex-web/src/data/chat.ts`

- `ChatState` 新增 `stopRequested: boolean`（附长注释说明为什么需要它）；
- `EMPTY` 加 `stopRequested: false`；
- `interruptTurn(sessionId, 'stop')` → `patch({ sending:false, sendingSessionId:null, phase:null, stopRequested:true })`
  （不再假装"立即已停"，而是承认"已请求停止、等工具返回"）；
- `interruptTurn(..., 'continue')` → `patch({ sending:true, ..., stopRequested:false })`（改方向后作废停止标记）；
- `pollTurnProgress` 发现**没有 running 轮次**时 → `patch({ serverTurn:null, stopRequested:false })`
  （服务端真的停下来了，停止确认该收了）；
- `sendMessage` 起始 patch 加 `stopRequested:false`（新一轮开始，旧标记作废）；
- `clearStaleSending` patch 加 `stopRequested:false`（清卡死状态时一并收掉红条）；
- `resumeTurn` 成功 patch 加 `stopRequested:false`。

### `suvex-web/src/pages/ChatPage.tsx`

进度条（`data-server-progress`）按 `stopRequested` 分两套皮肤/文案：

- `stopRequested=false`（v1.61 原样，蓝系 accent）：
  「服务端仍在后台推进这一轮：第 N 轮 · 已用 M 次工具。可以在这里等，或点「中断」停下；进度会自动跟进，不用刷新网页。」
- `stopRequested=true`（红系 `signal-risk`）：
  「已请求停止（第 N 轮 · 已用 M 次工具）：当前正在跑的工具没法中途打断，等它返回后这一轮就会结束。**不用再点中断。**」
  并加 `data-stop-requested="true|false"` 便于断言/测试。

轮询 effect 不用改：`stopRequested=true` 且服务端仍 `running` 时，`pollTurnProgress` 返回
`true`，维持轮询、红条常驻；工具返回、服务端不再 `running` 时，`pollTurnProgress` 顺手清掉
`stopRequested`，effect 走"接尾巴"分支，界面自动收尾（v1.61 已保证无需刷新）。

## 验证

### 单元/类型/构建

- `npx vitest run` → **1163 passed（78 files）**（v1.61 的 1161 + 本次新增 2）。
  新增两条（`src/data/chat.test.ts` 的 describe「服务端轮次进度轮询（v1.61）」内）：
  - `interruptTurn(stop) 之后 stopRequested 置 true（承认"已请求停止"）`
  - `pollTurnProgress 发现已无 running 轮次时把 stopRequested 清掉（停止真的落地了）`
- `npx tsc -b` → 通过（EXIT 0）。
- `npx eslint`（4 个改动文件）→ 0 error，仅 2 条**预存** react-refresh warning。
- `npm run build` → 成功（`index-Bgc8U8zl.js`）。

### 线上真实验证（Playwright，viewer `zzviewer88`）

部署 dist `index-Bgc8U8zl.js` 到 Kali 后，在同一会话里：

1. 发 `用 run_shell 执行：sleep 120; echo done123`，等工具开跑（页面显示「第 1 轮 · 正在执行 run_shell…」，中断按钮可见）；
2. 点「中断」→「彻底停止」；
3. **立刻**检查 DOM：`[data-server-progress]` 存在、`data-stop-requested="true"`、
   文案 = 「已请求停止（第 1 轮）：当前正在跑的工具没法中途打断，等它返回后这一轮就会结束。不用再点中断。」
   → 这正是要修的：**不再**回退成"服务端仍在后台推进……点中断停下"。
   截图 `artifacts/chat-v162-stop-requested.png`（998×938，已 read_image 复核：红条 + 文案正确）。
4. 等工具返回（约 120s）后**无需刷新网页**：进度条消失、中断按钮消失、输入框恢复正常、
   页脚显示「2 步 · 1 个工具 · 12:44」；
5. `GET /turns` 该轮终态 = `cancelled`（round 1 / tools 1）—— 证实 stop 在工具返回后
   如约生效（正是界面现在如实告诉用户的那件事）。

## 限制

- 这是**诚实告知**，不是"立即秒停"：一个已在跑的子进程没法被真正打断（服务端
  `interrupts.py` 的设计），任何前端都只能等它返回。本改动只是让界面在这一窗口期
  说真话、不误导重复点击。
- 纯前端改动，后端零改动。
