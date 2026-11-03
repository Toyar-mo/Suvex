# 对话流两处误导性提示修复：裸「HTTP 409」与页脚「N 个失败」（v1.58 / v1.59）

日期：2026-11-03

## 一、「HTTP 409」裸错（v1.58）

### 现象
用户在会话「Server Host：https://pro.dasctf.c…」发消息（输入框里还留着「不用下载了，比赛结束了，清除文件」），
底部弹出**红色裸条 `HTTP 409`** + 重试按钮，看不出发生了什么。

### 根因
- 服务端日志 `journalctl -u suvex-api`：409 来自 `POST /chat/sessions/{id}/messages/stream`（普通发送路径，
  不是 interject），当会话里**已有一轮正在跑**时，`send_message_stream`（`app/chat/router.py:407`，
  入口校验 `router.py:475-481`）返回
  `ApiError.conflict("TURN_ALREADY_RUNNING", "这个对话还有一轮正在跑。想补一句话用「插话」，想让它停下或改方向用「中断」，想接手已经断掉的那一轮点「继续」——它们都会接在同一轮上。")`。
  后端文案是对的。
- 错误信封形状（`app/core/errors.py:154 to_payload`）：`{"error": {"code", "message", "details?"}}` —— message 在 **error 里面**。
- **BUG 在前端 `suvex-web/src/data/chat-stream.ts`**：`if (!response.ok)` 分支读的是**顶层** `payload.message`，
  永远 `undefined`，于是回退到裸 `HTTP ${response.status}`。全站其它地方（`src/lib/api.ts:304`）都正确读 `envelope.error.message`。

### 修复
`chat-stream.ts` 开流前失败分支改为：

```ts
const server_message = payload?.error?.message ?? payload?.message
if (server_message) detail = server_message
```

与 `lib/api.ts` 口径对齐：优先 `error.message`，兼容顶层 `message`，都没有才回退状态码。
`sendMessage` catch（`src/data/chat.ts`）走 `messageOf`→`toApiError`，所以修好后中文文案能正常上错误条。

### 验证
- 单测 `src/data/chat.test.ts` 新增：「v1.58 开流前 409 后端 error 信封 → 错误条显示中文」。红/绿都验过（旧码收 `HTTP 409`，新码收中文）。
- 部署后线上视觉：用 `page.route` 注入真实 409 信封，页面错误条渲染**完整中文**（无裸 `HTTP 409`）。
  截图 `artifacts/409-bar-crop.png`。

## 二、页脚「N 个失败」误导（v1.59）

### 现象
CTF 会话某一轮页脚红字：`14 步 · 4 个工具 · 1 个失败 · 20:57 · CTF 解题`。
但那一轮的**最终答复是完整的**（「全部工作已收尾…5 题解出 3 题」）。

### 根因（真库查证）
DB 该轮（`chat_messages` seq 992–1005，user seq991「每道题的用时」→ assistant answer seq1005）：
4 个工具中只有 seq993 `fetch_competition_challenges` 的 `tool_failed=true`
（内容「失败：拉比赛平台题库失败：…accesskey 没有交易/agent 接口的权限…」）。
模型随后用 curl 绕过、照常交付完整结论。→「1 个失败」属实但**误导**：一次工具调用失败 ≠ 这一轮失败，
把已经交付正确结论的整轮标红，用户会以为活儿没干成。

### 修复
`suvex-web/src/components/chat/AssistantTurn.tsx`：按「这一轮最终有没有答复」分流：

- 有答复（`hasAnswer`）：页脚报 **`N 次工具报错（已恢复）`**，中性灰字（`text-ink-faint`），不标红；
- 没答复：才是真的失败，保留红字（`text-signal-risk`）**`N 个失败`**，别让用户漏看。

单张工具卡的失败徽标（`ToolGroup`/`ToolCard` 上的「失败」）保留 —— 那个工具确实失败了，如实显示。

### 验证
- 单测 `AssistantTurn.test.tsx`（12 条，含两分支）+ `ToolGroup.test.tsx`（7 条）全绿：
  有答复→`1 次工具报错（已恢复）`且**无** `1 个失败`；没答复→红字 `1 个失败`。
- 部署后线上视觉（用 `page.route` 喂真实形状的完成轮 + 未完成轮）：
  - 完成轮页脚：**`6 步 · 4 个工具 · 1 次工具报错（已恢复）·20:57`**（灰，不红）。截图 `artifacts/v159-footer-recovered.png`。
  - 未完成轮页脚：**`2 步 · 1 个工具 · 1 个失败`**，命中红字 span `.text-signal-risk`。
- 全前端 `npx vitest run`、`npx tsc -b`、`npx eslint`、`npm run build` 全绿。

## 部署
`npm run build` → 打包 dist tgz → kali 上 `find … /root/suvex/suvex-web/dist -mindepth 1 -delete; cp -a …; chown -R kali:kali`。
prod `ChatPage-*.js` md5 == 本地，含 `error?.message??` 与 `次工具报错（已恢复）`。
