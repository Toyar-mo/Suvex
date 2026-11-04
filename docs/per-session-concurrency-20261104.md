# 按会话独立并发 + 历史叙述清理 + Windows 工具反放弃（v1.64.2 / v1.65）

> 2026-11-04。对应用户三条反馈：
> - m01118：「另一个对话正在进行，新开一个对话发送不出去，同时只能一个会话运行吗？对齐 CyberStrikeAI 的设计修复」
> - m01118：「模型说：需要 RouterPassView 才能解析，在 Linux 沙箱里无法解密，我主动放弃……我之前不是说先让模型主动去下载工具来用吗？」
> - m00981 截图：CTF 老会话里满屏重复「执行：」「写 ACTION：」正文噪声。

## 一、多会话并发卡在前端，不在后端（v1.65）

### 结论先行

后端**本来就支持**多会话并发：每个轮次按请求独立跑，没有全局「同时只允许一轮」
的锁。`TURN_ALREADY_RUNNING`（`suvex-api/app/chat/router.py`）是**按会话**判的
（`turn_runs.list_runs(db, session_id=session_id, limit=1)`），不是全局。
实测证据：库里同一时刻有两条 `chat_turn_runs.status='running'`。

CyberStrikeAI 也是**请求级独立并发**（Go/Gin，每个 HTTP 请求跑自己的 agent 循环，
无全局聊天轮次锁）。所以后端已对齐，差距**纯粹在前端**。

### 根因：前端用一个全局 `sending` 布尔把「同时只能发一条」写死了

`suvex-web/src/data/chat.ts` 老代码：
- `sendMessage` 入口 `if (state.sending) return false;`
- `ChatPage.tsx` `canSend = ... && !sendingElsewhere;`、`handleSend` 里
  `if (sendingElsewhere) return;`
- `sendingHere = sending && sendingSessionId === active?.id`

老设计的注释理由是「两条并发会让 seq 乱序」—— 但 **seq 是按会话编的**，跨会话
根本不会重排。这个理由不成立。

### 修法：`sendingSessionIds` 集合做唯一事实来源

`ChatState` 新增 `sendingSessionIds: string[]`（本浏览器在跑的会话集合）：

- **唯一事实来源**：某会话在不在跑，看它在不在集合里。
- `sendMessage` 判重从「全局 sending」改成「**这个会话**在不在集合里」：
  同会话重复发仍拦（不在一会话里开两轮），**不同会话各跑各的**。
- `ChatPage` 的 `sendingHere = sendingSessionIds.includes(active.id)`；
  删掉 `canSend` 里的 `!sendingElsewhere` 与 `handleSend` 里的
  `if (sendingElsewhere) return` —— 别的会话在跑只给一句**提示**，不拦发送。
- `sending`（全局布尔）保留，但语义改为「集合非空（有任意会话在跑）」，
  仅用于少量与之等价的场景；并发闸一律改用集合。
- 展示字段（`phase` / `streamingText` / `pendingUserContent` / `interjections`）
  只描述「用户此刻正在看的那个会话的流」：`sendMessage` 的 `finally` 里，
  **只有这一轮正是被观看的会话时**才清这些字段，避免把另一会话刚画上的
  进度/正文误清空。
- 轮询 effect 里判「本会话在不在跑」也改用
  `getChatSnapshot().sendingSessionIds.includes(sessionId)`（不再用全局 `sending`）。
- `interruptTurn` / `resumeTurn` / `pollTurnProgress` / `clearStaleSending`
  同步维护集合（加/摘/按剩余算 `sending`）。

### 顺带修的 UX

- 欢迎屏「可以这样问」推荐问法按钮：原来 `disabled={sending}`（别的会话在跑就
  变灰 → 新会话发不出去）。改成本地 `pickingStarter` 防连点，与全局 sending 解耦。
- 附件条 `disabled` 从全局 `sending` 改成 `sendingHere`（只在本会话跑着时禁）。
- 重试/继续按钮 `disabled={resuming || sending}` → `{resuming || sendingHere}`。
- 输入框 placeholder：别的会话在跑时从「等它跑完再发」改为
  「另一个对话正在后台跑（不影响这里）—— Enter 发送，可同时进行」。

### 验证

- 前端 `tsc -b` 0 错；`eslint` 仅 2 条预存 react-refresh warning（0 error）；
  `npm run build` ✓（`index-9lWk3vlb.js`）。
- `npx vitest run` **1166 passed（78 files）**。新增/改写：
  - `src/data/chat.test.ts` 新增 describe「按会话独立并发（v1.65）」3 条
    （异会话可并发 / 同会话重复发被拦 / 集合收尾时 `sending` 正确收敛）。
  - `src/pages/ChatPage.test.tsx`：`chatState()` 夹具自动把老写法
    `sending:true + sendingSessionId` 迁移成 `sendingSessionIds`；
    原 v1.24「别的会话在跑 → 禁止发」用例**改写**为
    「别的会话在跑 → 仍能发（按钮可点、发得出去）」。

## 二、历史污染：老会话里的「写 ACTION：」正文噪声（v1.64.2）

1580 行的老会话（`chat_01M6H1JNW3S...`，1100 条消息）历史里，是文本协议时代
模型自己写下的「执行：写 ACTION：」残渣。原生 tool-calling 下工具调用本身是
干净的，但模型把这些**旧正文**回喂时会**照抄自己的旧写法**，于是正文出现满屏
「写 ACTION：」。

- 决定性探测（干净 native prompt + 工具）：`finish=tool_calls`，`tool_calls`
  干净 `{"command": "echo hello > a.txt && cat a.txt"}`，正文仅一句旁白，
  **无「写 ACTION」**。证明新代码/网关干净，问题只在历史。
- 修法：`engine.py` 新增 `_strip_legacy_action_narration(text)` +
  `_LEGACY_NARRATION_STUBS`，在 `_render_history` 的 assistant 分支里，**只**
  清洗喂给模型的历史（逐行删「整行纯占位」的行，压连续空行），**不动落库/界面**。
- 真实 DB 消息：298 字符 → 163 字符，真实句子保留，含 ACTION 的占位行全删。

## 三、Windows 专用工具不许直接放弃（v1.65 提示词）

`prompts.py` 在「缺工具就自己装」后新增一节：**Windows 专用工具？找 Linux 的
等价方案，别一句「环境不支持」就放弃**。给出至少三条要试的路径：
1. 找 Linux 原生等价工具（apt/pip 试名字）；
2. 自己写解析器（多数「Windows 工具」本质是按某格式解密一段字节，格式常公开）；
3. 用公开资料/web_search 反推算法。
并明确把用户遇到的 RouterPassView 当**反面案例**写进提示词：跑不了那个 GUI
从来不是终点，正确做法是 binwalk 拆包 → 认出格式 → Python 解密。

测试：`tests/chat/test_download_guidance.py` 新增
`test_windows_only_tool_fallback_guidance_present`（断言含 RouterPassView 反例 +
「自己写解析器」替代路径）。

## 四、测试总账

- 后端 `tests/chat/`：**461 passed, 1 skipped**（skip = 需密钥的 live E2E）。
- 前端全量：**1166 passed**。
- 后端 `tests/kernel/test_runner.py` 与既有全绿未受影响（本次未触及 runner）。
