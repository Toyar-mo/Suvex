# 对话「服务端在跑、前端看不见」+ 中断/继续要刷新 —— 修复说明（v1.61）

## 用户报的两个症状

- 「对话还是有问题，你修复了服务端在后台跑，前端没有显示然后对话，我不知道进行的怎么样了」
- 「中断并继续按钮也是坏的，点击之后要刷新网页才行，重新发送继续没有响应，还是要刷新网页」

用户要求「照抄 CyberStrikeAI 成熟项目怎么做的」，并在确认页选了「三个都修」。

## 根因

### 症状一：服务端在跑，前端看不见

前端**只信本页握着的那条 SSE 流**：

- `sending` / `phase` 都是本地流状态。流一断（刷新页面、切会话、done 帧丢、网络抖动），
  `sending=false`、`phase=null` —— 界面认为"没在跑"。
- 而服务端从 v1.54 起**断开不取消**（`app/chat/stream.py::stream_turn` 的 `finally` 是 `pass`），
  那一轮仍在后台跑。
- 于是出现"服务端在跑、前端一片空白、用户不知道进行到哪了"。

### 症状二：中断/继续点了没反应，要刷新网页

- `interruptTurn()` 原来只 `POST /chat/sessions/{id}/interrupt` 然后 `return true`。
  这个端点**只投递一个信号**（`app/chat/interrupts.py` 写进程内字典，引擎在下一个检查点才生效），
  **不返回新消息、不改前端状态** → 界面看起来"点了没反应"，必须手动刷新才看到变化。
- `resumeTurn()` 虽然把新消息追加进 `active.messages`，但不清 `sending`、不重取轮次账目 →
  黄条/错误条状态不同步，用户感觉"没响应、要刷新"。

## 照抄的做法：resumable polling

CyberStrikeAI 的长工具用 `execution_id` 异步跑，客户端 `get_tool_execution` / `wait_tool_execution`
**可恢复地轮询**（plan 进度页 1500ms 一次）。我们没有 execution_id（一轮就是一条 `ChatTurnRun` 行），
但形态一样：**只要服务端还有 running 的轮次，前端就每 1.5s 拉一次状态，把进度画出来。**

## 改动

### 后端（已就绪，接线校验）

- `app/chat/schemas.py::ChatTurnRunOut` 增加 `roundIndex` / `toolsUsed` / `heartbeatAt`
  （驼峰，`CamelModel`）。
- `app/chat/router.py:1109` 组装时从 ORM 行取：`roundIndex=run.round_index`、
  `toolsUsed=run.tools_used`、`heartbeatAt=run.heartbeat_at`。
- 引擎每轮已经调 `turn_run_store.heartbeat(..., round_index=..., tools_used=...)`
  （`app/chat/engine.py:2430`、`:3025`）并 `commit`，所以轮询拿到的就是真实进度。

### 前端

- `src/data/chat.ts`
  - `ChatState` 新增 `serverTurn: ChatServerTurn | null`；`ChatServerTurn = { runId, roundIndex, toolsUsed, heartbeatAt }`。
  - `ChatTurnRun` 增加可选 `roundIndex?/toolsUsed?/heartbeatAt?`。
  - 新增 `pollTurnProgress(sessionId): Promise<boolean>`：拉 `/turns`，有 `running` 就写
    `serverTurn` 返回 `true`，否则清空返回 `false`；切了会话就不写旧数据。
  - `interruptTurn()`：点完立刻——`continue` 把本地 `sending` 续成 true（服务端会接着跑），
    `stop` 清掉 `sending`；两种情况都 `void refreshActiveMessages()` + `void pollTurnProgress()`。
    **不用手动刷新网页。**
  - `resumeTurn()`：返回后清 `sending`、`void pollTurnProgress()` + `void refreshActiveMessages()`。
- `src/pages/ChatPage.tsx`
  - 新增 `useEffect` 轮询（依赖 `[active?.id, sending]`，**进会话即启动**）：只要服务端还有
    running 就每 1.5s 拉一次；跑完且本页没在跑（流早断了）就把新消息接上、`clearStaleSending()`、
    `loadSessions()`、`refreshStuckTurn()` —— 界面自动收尾，不必刷新。
    用 `sawRunning` 区分"刚发出、服务端还没建 running 行"与"流断了、sending 卡死"两种情形。
  - `serverRunning` 增加 `serverTurn !== null` 判据（刷新回来立刻把长任务切成"在跑"）。
  - 消息列表底部新增**服务端进度条**（`data-server-progress`）：`serverRunning` 时显示
    「服务端仍在后台推进这一轮：第 N 轮 · 已用 M 次工具。可以在这里等，或点「中断」停下；
    进度会自动跟进，不用刷新网页。」

## 验证

- 前端：`npx vitest run` → **1161 passed（78 files）**；`tsc -b` 通过；`eslint` 0 error；
  `npm run build` 通过。新增用例：`chat.test.ts` 的「服务端轮次进度轮询（v1.61）」4 条
  （poll 有/无 running；interrupt continue 恢复 sending；interrupt stop 清 sending），
  `@/data/chat` mock 补齐 `pollTurnProgress`/`refreshActiveMessages`。
- 后端：`tests/chat/test_turns_api.py` + `test_turn_persistence.py` → **79 passed**。新增
  `test_turns_endpoint_reports_round_and_tool_progress`：心跳写 `round_index=4/tools_used=7`
  后 GET `/turns` 断言 `roundIndex==4`、`toolsUsed==7`、`heartbeatAt!=null`、`status=="running"`。

## 线上实测（Kali 192.168.44.128:8080，v1.61 已部署）

- 部署：后端 `router.py` sha `305eeb55…`、`schemas.py` sha `e219d5b5…`（prod 相等）；
  前端 dist `index-D9oxUAv3.js`；重启 `Application startup complete`、`/api/v1/health` 200。
- `GET /chat/sessions/{id}/turns` 线上返回已带 `roundIndex/toolsUsed/heartbeatAt`（camelCase）。
- **核心症状一（服务端在跑、前端看不见）实测通过**：以 viewer 身份在对话页发一个多步命令
  让轮次跑起来，**中途刷新页面**（此时本页已无 SSE），页面立刻出现进度条：
  「服务端仍在后台推进这一轮：第 8 轮 · 已用 6 次工具。可以在这里等，或点「中断」停下；
  进度会自动跟进，不用刷新网页。」截图 `artifacts/chat-v161-progress.png`。
- **自动收尾实测通过**：该轮跑完后，页面**无需再次刷新**就把最终答案渲染出来（进度条自动消失）。
  截图 `artifacts/chat-v161-base.png` / `chat-v161-final.png`。
- 中断入口：运行中点「中断」正常弹出「中断并继续 / 彻底停止」对话框；若那轮已结束再点，
  界面会**内联报错**「这个对话当前没有在跑的回复，不需要中断」（409，比过去"点了没反应"更诚实）。

## 诚实记录的已知旁支（不属本次修复范围）

实测中模型对「多步/分步」类指令有时只输出**计划句**（「好的，先第 1 条」）而不发 ACTION，
触发既有 v1.45/v1.50 的"停在下一步计划上"中断逻辑 —— 这是**另一条**模型行为/触发词调优问题，
与本次「前端看不见 / 要刷新」两个 bug 无关；本次改动不触碰它。

