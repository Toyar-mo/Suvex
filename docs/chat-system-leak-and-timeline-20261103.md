# 对话页 `<system>` 刷屏 + 时间线重排（v1.56 / v1.57）

## 一、问题

用户截图：会话「Server Host: https://pro.dasctf.c…」（851 条）正文满是
`<system>ACTION.</system>` / `<system>执行.</system>` / `<system>先.</system>`
刷屏。模型自创了第 4 套 XML 标签 `<system>…</system>`（与此前
v1.41/v1.44/v1.49/v1.52 的 `<action>`/`<result>`/`<｜DSML｜>` 同一类"自创协议"
坑），既不在任何已知标签集合里，也不会执行、不会隐藏 —— 于是原样落在对话流里。

同时用户反馈整个对话页的**版式**不理想：「对话流就抄 CyberStrikeAI」。
病根：当前页把全部过程（推理/工具）收进顶上一个「过程详情」折叠块、结论另起
一块贴在最后 —— 用户读到的顺序和实际发生顺序相反。

## 二、根因（两条，分开修）

### 1. 写入口不认 `<system>`（v1.56a → v1.57 扩族）

`app/chat/engine.py` 的 `_normalize_xml_actions` 只在"检测到已知协议标签"时
动手；`<system>` 不在 `_XML_ANY_PROTOCOL_TAG_RE` 里，所以整段 XML 原样穿过
归一化，最终落库进正文。

### 2. 读出口不重跑归一化（v1.56b，本次审计新发现）

`app/chat/router.py:129` 的 `get_session` 直接
`ChatMessageOut.model_validate(m)` —— **读时不重跑归一化**。所以即使写入口已修，
**修复前已落库的历史消息**（那次会话 146 条正文带 `<system>`）仍会原样回放。
用户截图正是历史会话，写入口修复对已有的刷屏完全无效。

→ 必须在 `ChatMessageOut` 加 `content` 的 `field_validator`，在读出口再兜一层。

### 3. 整族 `system*`（v1.57，审计再深入发现）

用真实库枚举 `content LIKE '%<system%'` 的标签变体，得到：

| 标签 | 出现次数 | 内容 | 处理 |
| --- | --- | --- | --- |
| `<system>…</system>` | 2760 / 2755 | 碎片 / 真 `ACTION: {…}` | 含 ACTION 的展开，其余整块删 |
| `<system_warning>…</system_warning>` | 48 / 48 | 「你没写 ACTION，请补上。」纯纠偏提示 | 整块删 |
| `<system_welcome/>` | 2 | 空自闭合 | 删 |
| `<system_call>` + 裸 `{"tool":…, "args":…}` | 9 | 模型想发的**真动作**（用错标签名，引擎认不出→那轮空转→平台回灌 `<system_warning>你没写 ACTION`） | JSON 合法则展开成 ACTION，否则删 |

v1.56 的正则用 `system\b`，而 `\b` 卡在 `system_warning` 的 `_`（`_` 是词字符），
**一个变体都没匹配到** —— 线上实测 33 条里 31 条照样原样回放给前端。

修法：标签名正则从 `system\b` 扩成 `system[a-zA-Z_]*`（用 `(?![A-Za-z_])` 收边界），
并单独处理"只有开标签、后跟裸 `{"tool":…}` JSON"的 `<system_call>` 形态。

## 三、修复（写入口 + 读出口，两处同构）

### 写入口 `app/chat/engine.py`
- `_XML_STRAY_TAG_RE` / `_XML_ANY_PROTOCOL_TAG_RE` 的标签名扩成 `system[a-zA-Z_]*`。
- `_XML_SYSTEM_BLOCK_RE` 同步扩族；新增 `_XML_SYSTEM_OPEN_CALL_RE`（开标签 + 裸 JSON）
  与 `_XML_BARE_TOOL_JSON_RE`。
- 新增 `_is_tool_json(raw)`：只有能解析成 dict 且含非空 `"tool"` 才认作可执行动作。
- `_normalize_xml_actions` 里：`<system…>` 块内命中 `ACTION: {…}` 或合法裸 tool-JSON →
  展开成 `\nACTION: {…}\n`；否则整块删。再处理 `<system_call>{json}` 开标签形态。

### 读出口 `app/chat/schemas.py`
- `ChatMessageOut` 的 `@field_validator("content")` 调 `strip_system_wrappers`。
- `strip_system_wrappers` 与引擎同构：`_SYSTEM_BLOCK_RE` / `_SYSTEM_OPEN_CALL_RE`
  扩族 + `_is_tool_json` 判定；块内合法 tool-JSON 展开成可读 `ACTION: {…}`（人还能
  读到模型想做什么），其余整块删。

## 四、前端：对话页按事件时间线重排（v1.56b）

对照 CyberStrikeAI（事件驱动单一时间线：SSE 按
`progress`/`iteration`/`thinking`/`tool_call`/`tool_result`/最终答复逐条推，
前端按到达顺序逐条追加渲染）。

`suvex-web/src/components/chat/AssistantTurn.tsx`：
- 删掉旧的「finalMessages 与 workItems 分离、work 折叠块在顶、结论另起带 pt-3」。
- 新增 `buildTimelineSegments(items)` → `{kind:'tools'|'narration'|'answer'}` 段：
  答复自成段、pending 审批单独 tools 段、连续 tool 合一段、连续旁白合一段，
  **只合并连续同类、不跨类型重排**。
- 逐个渲染：answer→`<MessageRow>`；tools→同段多张 MessageRow；narration→原位折叠条。
- 根节点加 `data-timeline`；保留 turn 页脚（步数/工具数/失败数/时间/meta）与
  live phase 指示（`progress` prop 默认 null，向后兼容）。

效果：推理/工具/结果/结论各在**它发生的那个位置**逐条铺下来，而不是堆在顶上、
结论贴在最后。

## 五、验证（真跑真库真页面）

### 后端单测
- `tests/chat/test_xml_actions.py` v1.56 7 条 + v1.57 6 条；
  `tests/chat/test_message_out_system_cleanup.py` v1.56 8 条 + v1.57 6 条。
- 两文件合跑 **58 passed**；`tests/chat` 全量回归通过。

### 线上 prod（Kali，非 git 复制部署）
- `engine.py` sha=`53e584cc…`、`schemas.py` sha=`26d2240a…`，prod == 本地。
- 重启 `Application startup complete`、`/api/v1/health` = 200。

### 端到端（部署代码真跑真库）
用 prod venv + prod 代码，遍历两个带 `<system>` 的历史会话：
- `chat_01M6GAVGDD7JTENZ2ZANK7CT4W`：`rows_with_system_in_db=146` →
  `serialized_still_with_system=0`。
- `chat_01M6BWQ69MZJNBAB7C7QMVXPV2`：`rows_with_system_in_db=33` →
  `serialized_still_with_system=0`（v1.56 时这里是 31 条残留，v1.57 扩族后归零）。

样例对照（`strip_system_wrappers` 前 → 后）：
- `<system_warning>工具调用 run_shell 被取消</system_warning>\n\n我重新开始…`
  → `\n\n我重新开始…`（提示块删净，正文保留）
- `<system_welcome/>\n\n我已经把静态分析…` → `\n\n我已经把静态分析…`
- `<system_call>\n{"tool":"run_shell","args":{"command":"ls -la /work"}}`
  → `\nACTION: {"tool":"run_shell","args":{"command":"ls -la /work"}}\n`（真动作展开，可读）

### 前端真实页面（Playwright + read_image 复核）
- 会话页 DOM：`hasSystemTag=false`、`systemSnippets=[]`（零残留）。
- 时间线：`[data-timeline]` 两个 turn 根；turn2 子节点顺序
  `[过程详情, run_shell pwd, answer段1, answer段2, footer]` —— 答复夹在工具之后、
  页脚之前，位置保真。
- 截图：`artifacts/chat-timeline-v157-final.png`（已视觉复核）。

## 六、遗留

- `<system_call>` 里的 JSON 是"模型当时想发、但引擎没认、所以从未执行"的历史动作。
  现在读出口把它们展开成可读的 `ACTION: {…}` 文本（让用户能看懂它当时想做什么），
  但**不重放执行**——历史回合就是历史，不补跑。
