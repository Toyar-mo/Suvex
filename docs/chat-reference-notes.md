# 对话页参考研究：metis 与 CyberStrikeAI

> 目的：为后续前端（Suvex）对话页重写提供可复核的参考。本文只记录**能在源码里核对到的事实**，并注明文件路径 + 行号；无法确证的地方写「未确认」。
>
> 两个参考仓库为浅克隆副本（克隆根：`D:/profile/metis`、`D:/profile/CyberStrikeAI`），本文正文中的文件路径均相对各自仓库根。

## 0. 研究范围与版本（复核信息）

| 项目 | 仓库目录 | 远端 | HEAD | 提交时间 |
| --- | --- | --- | --- | --- |
| metis | `metis` | https://github.com/Wholiver/metis.git | `26685fdb56687933ff7a236aada1c546e106df78` | 2026-10-29 11:47:57 +0800 |
| CyberStrikeAI | `CyberStrikeAI` | https://github.com/AIPentest/CyberStrikeAI.git | `5affb4df1b178c893ae099fcc54ba052ca4afaff` | 2026-10-28 09:45:04 +0800 |

- `git -C D:/profile/metis log -1 --oneline` → `26685fd Merge pull request #51 from Wholiver/fix/server-dirname-esm`
- `git -C D:/profile/CyberStrikeAI log -1 --oneline` → `5affb4d Merge pull request #370 from GQingL/main`
- metis 对话组件目录 `D:/profile/metis/desktop/src/components/chat/` 共 36 个 `.tsx`；样式 `D:/profile/metis/desktop/src/styles/beautifului/chat.css`（551 行 / 14976 字节）。
- CyberStrikeAI 为 Go 后端 + 原生 JS 单页前端：`D:/profile/CyberStrikeAI/web/templates/index.html`（7060 行）、`D:/profile/CyberStrikeAI/web/static/js/chat.js`（11330 行）、`D:/profile/CyberStrikeAI/web/static/js/settings.js`（4924 行）、`D:/profile/CyberStrikeAI/web/static/css/style.css`（约 47k+ 行）。
- **CyberStrikeAI 副本完整性**：工作区缺 3 个被跟踪文件（`internal/c2/payload_oneliner.go`、`knowledge_base/SQL Injection/MySQL Injection.md`、`knowledge_base/SQL Injection/SQLite Injection.md`），`git status --porcelain` 显示为 ` D`，`git diff --stat` = `3 files changed, 1140 deletions(-)`（210 / 775 / 155）。三者的文件时间戳与克隆同批，疑似被本机杀软隔离；均不在本文引用范围内，不影响结论。

---

## 1. metis 对话层级

渲染链：`MessageList`（会话滚动 + 分组）→ `AssistantTurn`（一个助手回合）→ `AssistantWork`（工作过程）→ `ToolGroup` / `ToolCard`（工具分组与单卡）→ `AgentBubble`（最终答复）→ `AssistantTurnFooter`（回合页脚）。用户侧为 `UserBubble`；输入区为 `Composer`。

### 1.1 L0 会话容器与分组（`MessageList.tsx`）

- 滚动容器：`min-h-0 flex-1 overflow-y-auto px-4 py-4 flex flex-col items-center`，`overflowAnchor: 'none'`。
- 内容列（"lane"）：`MessageList.tsx:261` `<div className="flex w-full min-w-0 max-w-[620px] flex-col" data-message-lane>` —— **对话列最大宽度 620px**，与输入区同宽。
- 分组：`renderGroups` 把**连续的 assistant 消息合并成一个 turn**（key `turn-${id}`），并携带最近一条用户消息的 `startedAt` 与 `promptText`（用于重试）；带 `compaction` tag 的消息被跳过。
- 懒加载：`INITIAL_VISIBLE_GROUPS = 25`、`LOAD_MORE_GROUPS_STEP = 25`（`MessageList.tsx:10-11`），`scrollTop < 120` 时加载更早分组，另有 `IntersectionObserver`（`rootMargin: '100px'`）。
- 底部留白：`data-composer-clearance` 高度 `calc(var(--composer-overlay-height, 100px) + 16px)`，避免被浮层输入框遮住。
- 时间分隔：11.5px / `text-ink-3` / `tabular-nums`。

### 1.2 L1 回合骨架（`AssistantTurn.tsx`）

- 布局由 `resolveAssistantTurnLayout(messages, { streaming?, failureMessage? })` 计算 → `{ workItems, finalText, finalEntry }`：**最后一条可见文本**作为最终答复，其余（思考、叙述文本、工具调用）进入 workItems；过滤内部配额提示、子代理启动提示、失败错误文本（`stripInternalQuotaNotices` / `isSubagentLaunchNotice`）。
- 外层：`AssistantTurn.tsx:322/329` `<div className="assistant-turn-segment w-full min-w-0 max-w-full" data-assistant-turn>`。
- 无工作过程时：直接 `AgentBubble` + `AssistantErrorCard` + 页脚。
- 有工作过程时：`AssistantWork` → `AssistantTurn.tsx:337` `<div className="turn-final-response after-expanded-work w-full min-w-0 max-w-full">`（最终答复）→ 页脚。**最终答复与折叠工作过程之间有 24px 固定间距**（见 2 节）。

### 1.3 L2 用户气泡（`UserBubble.tsx`）

- 行容器：`UserBubble.tsx:45` `flex max-w-[500px] flex-col items-end gap-1.5`，外层 `my-2 flex w-full justify-end`（用户消息右对齐，**气泡列最大 500px**）。
- 气泡本体：`UserBubble.tsx:79` `max-w-full rounded-[10px] border border-line bg-surface px-3.5 py-2 text-left text-[14px] font-normal leading-[1.5] text-ink whitespace-pre-wrap break-words text-pretty shadow-hairline`。
- 附件：图片 `max-h-[240px] max-w-[320px] rounded-card`；文件/视频 chip `h-12 max-w-[280px] rounded-[12px]`，文件名 12px、大小 10.5px（`text-ink-3`）。
- 复制按钮：`h-6 w-6 rounded-[7px]`，位于气泡下方右对齐。

### 1.4 L3 工作过程（`AssistantWork.tsx`）

- 结构：`AssistantWork.tsx:164` `<section className="cot-container" data-assistant-work>` → `.cot-content-body` → `.cot-content-inner`（`data-slot="session-turn-assistant-content"`，纵向 flex，`gap: var(--work-item-gap, 12px)`）。
- 内容项装配：`groupAssistantWorkItems`（`AssistantWork.tsx:82`）过滤掉 thinking、空文本，以及 `update_plan` 工具调用（`:87`，计划由独立卡片/组件呈现，不混进工具分组）；随后把**连续**的 ContextToolGroup（read/list/glob/grep）与 CommandToolGroup 工具合并为 `contextGroup` / `commandGroup`。
- 标题文案：`assistantWorkTitle(streaming, elapsedMs, durationMs)` → `Working…`（<1000ms）/ `Working for X` / `Worked for X` / `Worked`。
- 叙述文本用 `MarkdownContent className="cot-text"`（14px / 1.6）；思考用 `ThinkingBlock`（有宽度上限，见 2 节）；未分组的单个工具用 `ToolCard`。
- 实时状态另有 `WorkProgressIndicator.tsx`：`role="status"` + `aria-live="polite"`，`data-progress-phase/status/marker`，`desktop/src/index.css:343-345`（`.assistant-turn-segment > .work-progress-indicator { margin-top: var(--work-item-gap); }`）给它与上方条目同样的 12px 间距。
- 注：`chat.css` 定义了 `.cot-header-bar` / `.cot-title`（工作过程标题栏），但当前版本 `AssistantWork.tsx` 只渲染 `.cot-content-body`，**未渲染 header bar**；`grep -rn cot-header-bar desktop/src/components/` 零命中 —— **组件层无任何使用者**，仅样式表保留（属残留样式）。

### 1.5 L4 工具分组（`ToolGroup.tsx`）与工具卡（`ToolCard.tsx`）

- 分组头是按钮：`ToolGroup.tsx:158` `<button className="tool-group-header" aria-expanded={expanded}>`，内含 `.tool-group-summary`（动词短语摘要）+ Chevron 图标。
- 摘要文案按工具类型生成（`formatToolGroupSummary`）：read → `Reading files` / `Read files`；edit → `Editing files` / `Edited files`；command → `Running commands` / `Ran commands`；search → `Searching code` / `Searched code`；agent → `Coordinating agents`；adapt → `Adapting architecture`；other → `Using tools`；多类以 ` · ` 连接，兜底 `Used tools`。
- 折叠行为：默认 `expanded = streaming`，流式期间自动展开、结束后自动收起，除非用户手动点过（`userOverrideRef`）。折叠动画用 `grid-template-rows: 1fr → 0fr` + opacity（见 2 节）。
- 分组体：`.tool-group-body` > `.tool-group-collapse-wrapper` > 可滚动 `.tool-group-list`（`data-tool-group-scroll`），列表**最大高度 168px**，溢出时出现上下 24px 渐变遮罩（`has-overflow` / `scrolled-from-top` / `scrolled-to-bottom` 类）。
- 行：`.tool-group-row`（最小高 24px），标签形如 `Ran <cmd>` / `Read <basename>` / `Edited <basename>`，失败时用工具显示名；附带 diff 统计（+N / -N，绿 `#16a34a` / 红 `#ef4444`，mono 11.5px）；流式时**每 80ms 逐行揭示**。
- 单个工具卡：`ToolCard.tsx:25` `ToolStatus = 'Running' | 'Pending' | 'Completed' | 'Error' | 'Denied'`；映射（`:29-38`）：progress running → Running、failed → Error、`result.isError` 且输出匹配 `/denied|rejected|cancel/` → Denied，否则 Error，正常 → Completed，无 progress 时流式 → Running、否则 Pending。子代理调用渲染为 `.tool-card.task-tool-chip` 小 chip（`task-tool-surface`），不展开完整卡片。

### 1.6 L5 最终答复（`AgentBubble.tsx`）

- `AgentBubble.tsx:33` `data-message-role="assistant"`；`:39` `w-full min-w-0 max-w-full py-0.5 text-[14px] font-normal leading-[1.6] text-ink`（**正文 14px / 行高 1.6**）。
- 若内容含 `<proposed_plan>` 块则渲染 `PlanPreview`，否则 `PacedMarkdown`。

### 1.7 L6 回合页脚（`AssistantTurnFooter.tsx`）

- `AssistantTurnFooter.tsx:54` `mt-1 flex min-h-6 items-center justify-start gap-2.5`（`data-assistant-turn-footer`）。
- 复制按钮 `h-6 w-6 rounded-[7px] text-ink-3 hover:bg-hover`，图标 `h-3.5`，复制后 2000ms 复位。
- 元信息：`:75` `cursor-default select-none text-[12px] leading-none text-ink-3`，内容 = 协作模式（`Plan` / `Build`）+ 模型名，以 ` · ` 连接。
- 仅在非流式且存在（复制文本 / 模式 / 模型）时显示。

### 1.8 输入区（`Composer.tsx` + `ComposerUsageFooter.tsx`）

- 浮层：`pointer-events-none absolute z-20 flex w-full flex-col items-center px-4`；首页空态 `inset-0 justify-center pb-8 pt-6`，会话态 `inset-x-0 bottom-0 justify-center pb-3 pt-1`；上方有渐变遮罩（`-top-7`）。
- 内列同样 `max-w-[620px]`（`Composer.tsx:402/560/566/592`），PromptBar `rounded-[14px]`（带附件/宽态 `rounded-[24px]`），位置在 `desktop/src/components/primitives/PromptBar.tsx:603`。
- 工具条：`ModelSwitcher` + `ModeSwitcher` + `Plan` 模式 pill（橙色 14% 背景、`h-7 rounded-full`、13px）。
- 用量页脚：`ComposerUsageFooter.tsx:49` `mt-2 flex w-full max-w-[620px] items-center justify-between gap-3 px-1 text-[11px] leading-none text-ink-3 tabular-nums`；左 = OAuth 配额 `5h {pct}% · 7d {pct}%` 或 `$cost · tokens`；右 = `{used} / {contextWindow}`（默认 256_000）。**与消息同一条 620px 轨道**。

### 1.9 L? 计划层（跨层：最终答复 `PlanPreview` + 输入区 `WorkflowPlanCard`）

计划在 metis 里**不在工作过程内**：`groupAssistantWorkItems` 装配工作项时过滤掉 `update_plan` 工具调用（`AssistantWork.tsx:87`：`item.type !== 'toolCall' || item.name !== 'update_plan'`）。该过滤没有注释，从计划的两处独立呈现可推断目的是「同一份计划不在工具分组里出现第二次」（推断，非源码注释）。计划只有两个入口：

**a) 最终答复内的 `<proposed_plan>` 预览**

- 解析：`AgentBubble.tsx:20` `extractProposedPlan(message.content, Boolean(message.streaming))`；标签常量 `<proposed_plan>` / `</proposed_plan>` 在 `lib/plan-preview.ts:8-9`，解析函数 `:24`（`allowPartial` 用于流式半截标签）。
- 渲染：`AgentBubble.tsx:43-59` —— 有计划块时依次渲染 `proposedPlan.before` → `PacedMarkdown className="mb-2"`（`:46`）、`<PlanPreview …>`（`:48-53`）、`proposedPlan.after` → `PacedMarkdown className="mt-2"`（`:55`）；没有计划块则退回单个 `PacedMarkdown`（`:59`）。即**计划前后仍保留叙述文本，各留 8px 间距**。
- `current` 高亮：`AgentBubble.tsx:21-26`，条件为「非 partial 且 `workflowProposal.markdown.trim() === proposedPlan.plan.trim()`」—— 草稿计划与已批准计划文本一致时标为当前。
- 卡片：`PlanPreview.tsx:26-45`，根 `div.plan-preview my-2.5 w-full max-w-full`，带 `data-plan-preview` / `data-plan-current` / `data-plan-partial`；内容全部交给 `RecommendationCard`（`:36-44`）：标题走 `splitPlanTitle`（`lib/plan-preview.ts:50`，取首行标题、其余为正文）、正文 `MarkdownContent className="plan-preview-markdown"`、折叠态摘要用 `summarizePlanPreview`（`:59`）作为 `collapsedBody`、动作按钮 `Open plan in inspector`、`className="border-0"`（去掉卡片边框，贴合正文流）。partial 时另有 sr-only `role="status"`（`:33-35`）。
- 折叠语义在 `desktop/src/components/primitives/RecommendationCard.tsx:76`（`const isLong = Boolean(collapsedBody) || (typeof rawBody === 'string' ? rawBody.trim().length > 120 || rawBody.includes('\n') : Boolean(rawBody));`）：有 `collapsedBody`，或正文 >120 字符 / 含换行 → 视为长文默认折叠；展开后渲染完整 `body`（同一文件 `:95-103`）。**注意该文件不在 `components/chat/` 下**——`components/chat/` 内无任何 `RecommendationCard*` 文件，`find desktop/src/components -name "RecommendationCard*"` 只命中 `./primitives/RecommendationCard.tsx`。

**b) 输入区的执行计划卡（`WorkflowPlanCard.tsx`，169 行）**

- 出现条件：`Composer.tsx:399` `showPlanStack = collaborationMode === 'build' && Boolean(workflowPlan)` —— 仅 Build 模式且已有工作流计划。
- 结构：`<section>`（`rounded-[12px]`、`data-workflow-plan-card`、`data-plan-expanded / data-plan-complete / data-plan-interrupted`，`WorkflowPlanCard.tsx:78-87`）→ header 按钮（`min-h-9`、`aria-expanded`、`aria-controls="workflow-plan-body"`、`data-workflow-plan-header`，`:88-107`）→ 折叠体（`:109-119`）。
- 进度文案 `:59-71`：`reading_proposal` → `Reading the approved proposal…`；`creating_checklist` → `Creating the execution checklist…`；有任务 → `{completed}/{total}`；只有 legacy markdown → `Legacy execution plan`；否则空态文案。中断时前置 `Interrupted · can continue · `（`:69-71`）。header 文字 12.5px / 500 / `tabular-nums` / `text-ink-2` / 单行省略（`:97`），Chevron 300ms 内 `rotate-0` ↔ `-rotate-90`（`:100-106`）。
- 默认展开态：`:49` `useState(!complete)`，并在 `plan.updatedAt + taskId` 变化时重置（`:52-54`）—— 未完成默认展开、完成后默认收起。
- 折叠动效：`grid-template-rows: 1fr ↔ 0fr` + opacity、`duration-300`、timing `cubic-bezier(0.23, 1, 0.32, 1)`（`:111-115`）。缓动与工具分组/工具卡的 `cubic-bezier(0.2, 0.8, 0.2, 1)` **不同**（收尾更快），复用动效时需注意。
- body：解释 12px/1.45/ink-3（`:121-125`）；仅有 legacy 且无任务时渲染 `<pre>`（12px、`max-h-40`、`:127-131`）；步骤 `<ul>` 每行 `min-h-8`、`gap-3`、`data-plan-step-status`（`:133-162`），状态图标为 15px 方框（完成 ✓ / 进行中 5px 实心块 / 待办空框，`:17-39`），步骤文字 13px/1.45（完成 `line-through text-ink-3`、进行中 `font-semibold text-ink`、待办 `text-ink-2`，`:148-157`）。
- **与输入框的叠加规则**（`Composer.tsx:590-603`）：外层 stack `relative flex w-full max-w-[620px] flex-col`，带 `data-composer-stack`（仅 `showPlanStack` 时输出）/ `data-composer-dock-stack`（`:590-595`）；先渲染计划槽 `data-composer-plan-slot`（`:596-600`），再渲染输入层 `data-composer-input-layer`，`showPlanStack` 时输入层加 **`z-10 -mt-[14px]`**（`:601`）—— 输入框**上提 14px 压住计划卡下缘**，视觉上合并为一个连续栈，而不是「卡片 + 间隙 + 输入框」。

---

## 2. metis 关键样式数值

### 2.1 尺寸与间距

| 项 | 值 | 出处 |
| --- | --- | --- |
| 对话/输入列宽 | `max-w-[620px]` | `MessageList.tsx:261`，`Composer.tsx:402/560/566/592` |
| 用户气泡列宽 | `max-w-[500px]` | `UserBubble.tsx:45` |
| 用户气泡内边距/圆角 | `px-3.5 py-2` / `rounded-[10px]` | `UserBubble.tsx:79` |
| 工作项间距 | `--work-item-gap: 12px` | 变量在 `index.css:66`；消费它的 `.cot-content-inner { gap: var(--work-item-gap, 12px) }` 在 `chat.css:101` |
| 最终答复间距 | `--work-final-gap: 24px` | `index.css:67`；`chat.css:181`；`opencode-session.css:379-382` |
| 回合末间距 | `--work-turn-end-gap: 20px` | `index.css:68` |
| 工作过程顶部间距 | `.cot-container { margin: 12px 0 0 }`（`opencode-session.css:360-363` 覆写为 `margin: 0; width: 100%`） | `chat.css:9-15` |
| 工具分组列表 | `max-height: 168px`，`padding: 0 4px 6px 0` | `chat.css:263-275` |
| 工具分组行高 | `min-height: 24px`，`gap: 8px` | `chat.css:279-285` |
| 工具卡圆角 | `12px`（透明背景） | `chat.css:319` |
| 工具详情块 | `margin: 4px 0 5px`，`padding: 10px 12px`，`radius 12px`，`bg color-mix(ink 3%)` | `chat.css:363-374` |
| 工具输出区最大高 | `260px`（`opencode-session.css:399-410` 对 tool output 为 `240px`） | `chat.css:389` |
| 溢出渐变遮罩高 | `--work-overflow-fade-height: 24px` | `index.css:70`，`chat.css:231-261` |

### 2.2 字号与字重

| 用途 | 值 | 出处 |
| --- | --- | --- |
| token 尺度表 | meta 11 / caption 12 / secondary 13 / ui 14 / body 14 / title-sm 15 / title 16 (px) | `index.css:34-40` |
| 正文（助手 + 用户） | 14px / 1.5（用户）、14px / 1.6（助手） | `UserBubble.tsx:79`、`AgentBubble.tsx:39` |
| 工作过程叙述 `.cot-text` | 14px / 1.6 | `chat.css:170-178` |
| 工具分组摘要 | 14px，单行省略 | `chat.css:201-215` |
| 工作过程标题 `.cot-title` | 13px / 440 / `tabular-nums` / `letter-spacing -0.04px` | `chat.css:36-44` |
| 回合页脚元信息 | 12px / `leading-none` | `AssistantTurnFooter.tsx:75` |
| 用量页脚 | 11px / `leading-none` / `tabular-nums` | `ComposerUsageFooter.tsx:49` |
| 工具详情 section 标题 | 11px / 530 / 大写 | `chat.css:387-388` |
| diff 统计 | mono 11.5px | `chat.css:287-297` |
| 时间分隔 | 11.5px / `tabular-nums` | `MessageList.tsx` 时间分隔块 |

### 2.3 颜色 token

- 正文/次要文本：`text-ink` / `text-ink-3`；`--ink-2` 为 body copy 色，定义在 `index.css:62`（`--body-copy-color: var(--ink-2)`；同组 `:61` 是 `--focus`）。
- 线/面：`border-line`、`bg-surface`、`hover:bg-hover`。
- 中性灰（工具区）：`#737373`（摘要/行），标题 `#424647`（暗色 `#94a3b8`），见 `chat.css:18-34 / 187-199 / 279-285`。
- 语义色：新增 `#16a34a`、删除 `#ef4444`（`chat.css:296-297`）；`Plan` pill 橙色 14% 背景。

### 2.4 折叠/动效

- 折叠实现：`grid-template-rows: 1fr ↔ 0fr` + opacity（`chat.css:68-91`）。四个 wrapper（`.cot-collapse-wrapper` / `.thinking-collapse-wrapper` / `.tool-group-collapse-wrapper` / `.tool-card-collapse-wrapper`）共用同一规则块：`display: grid` 与 `grid-template-rows: 1fr` 在 `:72-73`、`opacity: 1` 在 `:75`、`transition-property: grid-template-rows, opacity` 在 `:77`、`transition-timing-function: cubic-bezier(0.2, 0.8, 0.2, 1), ease` 在 `:78`；`.collapsed` 收起态 `grid-template-rows: 0fr` + `opacity: 0` 在 `:89-90`（`:85-88` 是四条 `.collapsed` 选择器）。
- 时长（`chat.css:81-83`）：cot 350/280ms、tool-group 280/220ms、tool-card 240/180ms——**每行两个值的前者作用于 `grid-template-rows`、后者作用于 `opacity`**（由 `:77` 的 property 顺序决定）。
- Chevron：`.cot-chevron` 14px，展开 `rotate(90deg)`、收起 `rotate(0)`（`chat.css:48-58`）；`.tool-group-chevron` 14px、160ms（`chat.css:217-226`）。
- 流式打字/等待：shimmer 渐变动画 1.4s（`chat.css:529-538`；`animation: shimmer-text 1.4s linear infinite;` 实测在 `:537`，`:529-531` 是 `.cot-title.working-shimmer` / `.tool-group-row-label.shimmering` / `.tool-name.shimmering` 三个选择器，`:532-536` 是 `color: transparent` + 三段渐变 + `background-clip: text`）；思考骨架 max-width 540px、12px 行、1.8s（`chat.css:147-160`）。
- 无障碍：`prefers-reduced-motion: reduce` 关闭 shimmer/动画（`chat.css:536-548`）；`WorkProgressIndicator` 使用 `role="status" aria-live="polite"`。
- 思考块宽度上限 `min(100%, 720px)`（`chat.css:113`）。

---

## 3. CyberStrikeAI 参数呈现

技术形态：Go（Eino）后端 + **单文件 `index.html` + 原生 JS**（无组件框架）。对话消息由 `static/js/chat.js` 组装 DOM；参数在设置页（同一 `index.html` 内的 AI 通道编辑器）与输入区快捷入口两处呈现。

### 3.1 配置模型（`config.example.yaml`）

- `ai.default_channel: qwen-max`（`:101`），`ai.channels.<id>` 每个通道必填 `api_key / base_url / model`（`:93`）。
- 单通道字段（`qwen-max` 示例，`:103-117`）：
  - `name`（`Qwen Max`，`:104`）
  - `provider: openai_compatible`（`:105`；可选 `openai_compatible` | `claude`，见 `:99`）
  - `base_url`（`:106`）、`api_key`（`:107`）、`model: qwen3-max`（`:108`）
  - `max_total_tokens: 120000`（`:109`）
  - `max_completion_tokens: 32768`（`:110`）
  - `reasoning`（`:112-117`）：`mode: on`（auto|on|off）、`effort: high`（low|medium|high|max|xhigh）、`allow_client_reasoning: true`、`profile: openai_compat`（auto|deepseek_compat|openai_compat|output_config_effort）、可选 `extra_request_fields`。
- 相关预算参数：`agent.max_iterations: 12000`（`:160`）、`context.reduction_max_tokens_for_clear: 60000`（`:349`）、`summarization_trigger_ratio: 0.8`（`:353`）、`model_retry_*` / `failover_*`（`:366-369`）。
- 语义（来自 UI 提示文案）：`max_total_tokens` 是**内存压缩与攻击链构建共用的上下文预算**，不是模型窗口本身；`max_completion_tokens` 是单次回复输出上限。

### 3.2 设置页参数区（`web/templates/index.html`，约 `:3700-3800`）

- 「连接信息」：`#ai-channel-name`、`#openai-provider`（下拉 `openai_compatible` / `claude`）、`#openai-base-url`（必填）、`#openai-api-key`（password，必填）、`#openai-model` + `#openai-model-select` + 「获取列表」（`fetchModelList`）、「测试连接」`#test-openai-btn`。
- 「额度设置」：
  - `#openai-max-total-tokens`（`number`，`:3759`，placeholder `120000`，min 1000，step 1000，提示「内存压缩和攻击链构建共用此配置，默认 120000」）。
  - `#openai-max-completion-tokens`（`:3764`，placeholder `16384`，min 1，step 256，提示「单次模型回复的输出上限，默认 16384」）。
  - **注意数值不一致**：配置文件与 JS 读取默认值都是 `32768`，但输入框 placeholder 与提示文案写 `16384`；哪个是"正确"默认值未确认。
- 「推理设置」（折叠 details）：`#openai-reasoning-mode`（auto/on/off）、`#openai-reasoning-effort`（''/low/medium/high/xhigh/max）、`#openai-reasoning-profile`。
- JS 侧（`web/static/js/settings.js`）：
  - `:2649-2667` `ensureAIConfigShape` 兼容旧 `openai` 块：`max_total_tokens: oa.max_total_tokens || 120000` 在 `:2661`、`max_completion_tokens: oa.max_completion_tokens || 0` 在 `:2662`。
  - `:2669-2690` `readAIChannelFromMainForm`：`parseInt(...) || 120000` / `|| 32768`。
  - `:2691` `writeAIChannelToMainForm` 回填同样默认：`max_total_tokens` 兜底 `120000` 在 `:2710`、`max_completion_tokens` 兜底 `32768` 在 `:2712`；其后是推理段 `:2713-2730`（mode / effort / profile / allow_client_reasoning）与三个 sync 调用 `:2731-2733`，函数体止于 `:2734`（评审建议的 `:2691-2720` 实测只覆盖到推理 mode 分支开头，本版按函数边界记为 `:2691-2734`）。

### 3.3 输入区形态（`index.html:1146-1400`）

- 外层 `#chat-input-container` → `.chat-composer-context`（项目选择 `#chat-project-btn`、角色选择 `#role-selector-btn`、隐藏的 agent 模式 `#agent-mode-btn`：`eino_single / deep / plan_execute / supervisor`）+ HITL 审批 dock。
- 主输入面 `.chat-input-primary-row.chat-composer-surface`：`flex column`，`min-height 118px`，`border 1px rgba(15,23,42,0.12)`，`border-radius 22px`，`background #fff`，双层阴影；聚焦态边框 `rgba(0,102,255,0.38)` + 3px 蓝色 ring（`style.css:45012-45042`）。
- `textarea#chat-input`：`height/min-height 58px`，`max-height 180px`，`padding 17px 22px 6px`，`radius 22px 22px 8px 8px`，`font-size 15px` / weight 450 / `line-height 1.55`，placeholder「输入测试目标或命令…  @ 选择工具」（`style.css:45056-45080`，`index.html:1272`）。
- 底部工具条 `.chat-composer-footer`（`min-height 50px`，`padding 2px 12px 9px 14px`，`gap 16px`，`style.css:45086-45093`）：
  - 左：隐藏 file input + `.chat-upload-btn`（36×36，radius 10）+ `#chat-hitl-shortcut`（「Agent 审查：关闭」）。
  - 右：默认隐藏的 `.chat-ai-session-state` 内 `select#chat-ai-channel-select`（「跟随默认通道」；选项文本在 `chat.js:1033-1037`，`:1036` 为 `opt.textContent = (ch.name || id) + (ch.model ? ' · ' + ch.model : '')`，即「通道名 · model」且 model 缺省时只有通道名）、`select#chat-reasoning-mode`、`select#chat-reasoning-effort`；`#chat-model-shortcut` 按钮（显示通道文本 + effort 徽标）打开 `#chat-system-model-menu` 对话框，行项为 **AI 通道 / 模型 / 推理模式 / 推理强度**，次级列表 `#chat-system-model-list`；发送按钮 `#chat-send-btn`。
  - `.chat-session-shortcut` 尺寸：`height 36px`、`padding 0 10px`、`radius 10px`、13px / 520 / `#767d87`（`style.css:45127-45145`）。

### 3.4 消息流与过程呈现（`static/js/chat.js`）

- 消息 DOM（`chat.js:3568-3605`）：`div.message.<role>` > `.message-content`（`:3570`）> `.message-bubble`（`:3574`）；assistant 走 `formatMarkdownToHtml(..., { profile: 'chat' })`（`:3600`），user 文本转义并保留换行。
- 版式（`style.css`）：`.chat-messages` 规则块从 `:3500` 开始（`flex: 1` 在 `:3501`），`padding: 24px 24px 24px 64px` 在 `:3504`、`background: #f5f7fa` 在 `:3505`；`.message.user` 右对齐（`:3821`）；`.message-content` `max-width 70%`（`:3830-3839`）；用户 `min(78%, 760px)`（`:3841-3846`）；助手 `min(100%, 920px)`（`:3848-3853`）；`.message-bubble` `padding 12px 16px`、`radius 8px`、`line-height 1.6`（`:3861-3866`）。
- 过程呈现（文案在 `monitor.js`，容器样式在 `style.css`）：
  - 分级前缀：轮次/步骤标题 `monitor.js:3066-3082` → `主代理 · 第 N 轮` / `Plan-Execute · 第 N 轮 · <phase>` / `子代理 · <agent> · 第 N 步`；思考与推理链 `monitor.js:3284-3290` → `🤔 AI思考` / `🔗 推理过程`；规划类 `📝`（`monitor.js:191-199`）；工具调用行 `🔧 <toolCallTitle>`（`monitor.js:3630`）；结果行状态图标 `✅/❌/🛡/⏳`（`monitor.js:3662`）。
  - **整段过程可折叠**：一条消息只有**一个**折叠容器 `.process-details-container`（`style.css:4415`）→ `.process-details-content` → `.progress-timeline`。默认收起 `.progress-timeline { max-height: 0; overflow: hidden; transition: max-height 0.3s ease }`（`:7669-7673`），展开态 `.progress-timeline.expanded { max-height: 2000px; overflow-y: auto }`（`:7675-7679`），流式期间改用 `min(64vh, 720px)`（`:7682-7690`，值在 `:7684`）。切换函数 `toggleProcessDetails` 在 `monitor.js:2575-2621`（加/去 `expanded` 类，首次展开时懒加载过程详情，并回写摘要栏 `syncAssistantTurnSummary`）。
  - 入口是折叠摘要栏：按钮 `.mcp-call-label.turn-process-summary`（`chat.js:5208-5222`，`onclick → toggleProcessDetails(null, messageId)` 在 `:5219-5222`）；文案由 `syncAssistantTurnSummary` 写入（`chat.js:5146-5192`）：状态点 + 文案 + token chip + chevron，文案按状态为 `耗时 X` / `已处理 X` / `已中断 · 耗时 X` / `已超时 · 耗时 X` / `执行失败 · 耗时 X`（`:5174-5185`），token chip 由 `formatAssistantTurnTokenUsageLabel` 生成（`:5170`，函数在 `:5046`）。
  - 摘要栏样式：`.turn-process-summary` `min-height 44px`、`font-size .91rem`、底部 1px 分隔线（`style.css:45837-45859`）；`.turn-process-token-chip` opacity .72、`::before` 补 `· `（`:45898-45916`）；`.is-expanded` 时 chevron `rotate(90deg)`（`:45931-45933`）。
  - **没有按工具类型分组**：时间线是平铺的 `.timeline-item` 列表（单行样式 `style.css:7692-7698`：`padding 12px`、`margin-bottom 8px`、`border-left 3px`、`background var(--bg-secondary)`；长列表优化 `:7948` 只给了 `content-visibility: auto` / `contain-intrinsic-size: auto 72px`），每条是 emoji + 文本 + 内容块，**没有分组头、也没有聚合式动词短语摘要**。
  - **但 tool_call 行另有互斥的逐行详情折叠**：每条工具调用行都会被加上 `.tool-detail-collapsible` / `.tool-call-collapsible`（`monitor.js:6821-6829` 在 `tool_call` 分支的状态判断之后无条件调用 `setToolCallDetailState`，`:6036-6042` 落类并刷新标签），行头 `::after` 通过 `data-tool-detail-label` 显示「查看详情 ▾ / 收起 ▴」（`:6055-6061`；样式 `style.css:46145-46147` `content: attr(data-tool-detail-label)`，可点击态 `:8274-8283`）；点击走 document 级委托（`:6172-6185`）→ `renderToolCallDetailContent` 增删 `.timeline-item-content.tool-call-detail-content` 并切换 `tool-call-detail-expanded`（`:6087-6170`）；同一 `.progress-timeline` 内打开一行会自动收起其它行（`:6090` 调 `collapseOtherToolCallDetails`，函数在 `:6075-6085`）。窄屏 `@media (max-width: 760px)` 下该标签退化为 `›`（`style.css:46646-46650`）。默认态是全部收起，所以视觉上仍是一根平铺长列表，只是每行可点开。
  - **只有 thinking / reasoning 行是纯平铺**：`thinking` / `reasoning_chain` / `planning` 走 `monitor.js:6794` 分支，从不调用 `setToolCallDetailState`，没有逐行的详情折叠、也没有逐行摘要。另有独立的 `.mcp-tool-list` 工具 chip 平铺列表（默认 `display: none`，`.expanded` 才 `display: flex`，`style.css:4344-4354`），同样不分组。
- 计划进度：`static/js/chat-plan-progress.js`，识别 `taskcreate/taskupdate/tasklist/taskget` 工具，`deriveProgress` 得 `{ tasks, total, completed, activeStep, allCompleted }`，活跃时 1500ms 轮询、4000ms 重试、完成后保持 2400ms。
- 回合用量（归一化 `chat.js:4940-4979`；提取 `eino_usage_summary` 在 `:4981-4990`（判断在 `:4985`）；写入 `data-turn-*` 在 `:4992-5023`）：字段为 `data-turn-model-calls / turn-prompt-tokens / turn-completion-tokens / turn-total-tokens / turn-cached-tokens / turn-cache-write-tokens / turn-reasoning-tokens / turn-model`；展示标签由 `formatAssistantTurnTokenUsageLabel`（`:5046`）生成，如 `123K tokens`（K/M，1 位小数），`title` 由 `formatAssistantTurnTokenUsageTitle`（`:5053-5068`）展开 prompt/completion/cached/cacheWrite/reasoning/calls/model。

---

## 4. 对 Suvex 的落地建议

### 4.1 直接沿用的版式基线（来源：metis）

1. **单列 620px 轨道**：消息 lane、输入框、用量页脚三者共用 620px，视觉上形成一条竖线 —— 建议 Suvex 用同一个容器宽度常量（如 `--chat-rail: 620px`）。
2. **用户气泡 500px 上限、10px 圆角、`14px/1.5`**：与助手 `14px/1.6` 形成轻微层级差；颜色用 border + surface，不填满色块。
3. **工作过程与最终答复的间距差**：工作项之间 12px，工作过程 → 最终答复 24px，回合末 20px。三个间距分别表达"同一过程内的条目 / 过程与结论的分界 / 回合之间"。
4. **折叠动效统一为 `grid-template-rows 1fr ↔ 0fr` + opacity，缓动 `cubic-bezier(0.2,0.8,0.2,1)`**，工具分组 280ms、工具卡 240ms；并保留 `prefers-reduced-motion` 关闭分支。
5. **工具分组用动词短语摘要**（`Read files` / `Ran commands`）+ 折叠体 `max-height 168px` 滚动 + 流式期 80ms 逐行揭示 + 上下 24px 渐变遮罩；行高 24px、摘要 14px、diff 统计 mono 11.5px。这套组合在窄列下信息密度最高。
6. **回合页脚只放"可复制 + 模式 · 模型"，12px / ink-3**；不要塞时间戳（时间放消息分隔线）。
7. **输入区浮层 + 底部留白**：浮层绝对定位，消息列表底部留 `composer高度 + 16px`；离开"输入框推挤列表"的老式布局。

### 4.2 参数呈现（来源：CyberStrikeAI）

1. **参数分两层**：设置页（完整编辑，含 provider / base_url / api_key / model / 预算 / 推理）与**会话内快捷入口**（只读选择 + 覆盖）。若未来扩展多 provider，可参考"会话内按钮 → 弹出四行菜单（通道 / 模型 / 推理模式 / 推理强度）"的形态（本轮不做）。
2. **两个预算字段必须带一句话语义提示**：`max_total_tokens` = 上下文预算（与压缩/链式构建共用），`max_completion_tokens` = 单次输出上限。仅给数字不给语义，用户必然填错。同时**不要出现让 placeholder 与默认值不一致的情况**（CyberStrikeAI 现存 `16384` vs `32768` 的歧义），Suvex 应把默认值与提示文案抽成同一常量。
3. **模型输入用「文本框 + 拉取列表 + 测试连接」三件套**：手填容错、列表降低拼错、测试连接即时校验 key/base_url。
4. **provider 用枚举下拉**（`openai_compatible` / `claude` 原生），推理参数（mode / effort / profile）放在折叠的"高级"区，避免首屏噪音。
5. **每回合 token 用量作为数据结构而非文案**：把 prompt / completion / cached / reasoning / calls 写成 `data-*` 属性，标签只做 `12.3K tokens` 这类缩写，详细值放 `title`/popover —— 便于后续做统计面板。
6. **本轮 Suvex 的复用边界**：只借鉴版式、文案与参数分层（设置页完整编辑 + 会话内只读展示），**不新增配置字段**；`max_total_tokens` / `max_completion_tokens` / provider / reasoning 等仍是 CyberStrikeAI 侧事实，Suvex 现有 人格 / 接入点 / 审批 / 技能 / MCP / 项目 六类参数保持不变。

### 4.3 明确不照搬的部分

- CyberStrikeAI 的过程时间线**整体可折叠**（单容器 `.progress-timeline`，`max-height` 0.3s 过渡，见 3.4），折叠态摘要栏已经能给出「耗时 X · N tokens」这种收敛信息；`tool_call` 行本身还带**互斥的逐行详情折叠**（行头「查看详情 ▾ / 收起 ▴」，点开一行收起其它行，见 3.4）。真正没做到、也不该照搬的只有两条：**没有按工具类型分组**、**没有聚合式动词短语摘要** —— 一条消息里几十次工具调用按发生顺序排成一根长列表，参数与结果要逐条点开才看得到，折叠态下每行只剩一行标题。Suvex 应采用 metis 的「按工具类型分组折叠 + 动词短语摘要 + 流式逐行揭示」，并可把 CyberStrikeAI 的「折叠栏显示耗时/token」思路挪到分组头或回合页脚。
- CyberStrikeAI 助手气泡宽到 920px、消息区左内边距 64px，属于宽屏阅读取向；若 Suvex 以中文正文为主，620px 轨道更稳。
- metis 的 `Plan` / `Build` 模式 pill 与 `update_plan` 过滤逻辑值得借鉴：**计划类工具调用不进工具分组**，由独立计划卡呈现，避免同一信息出现两次。

---

## 附录 A：自检

- 文件存在且非空：`docs/chat-reference-notes.md`
- 含四节标题：`1. metis 对话层级`、`2. metis 关键样式数值`、`3. CyberStrikeAI 参数呈现`、`4. 对 Suvex 的落地建议`
- 仓库复核输出见第 0 节；未修改任何仓库源码。

## 附录 B：引用抽查

以下 16 条均在参考仓库对应位置实测核对（命令形如 `awk 'NR==<行>{print NR": "$0}' <文件>` 或 `sed -n '<行>' <文件>`），`路径:行号 → 关键片段`：

1. `metis/desktop/src/components/chat/MessageList.tsx:261` → `className={\`flex w-full min-w-0 max-w-[620px] flex-col ...\`}` 且 `data-message-lane=""` —— 对话列 620px 成立。
2. `metis/desktop/src/components/chat/UserBubble.tsx:45` → `<div className="flex max-w-[500px] flex-col items-end gap-1.5">` —— 用户气泡列 500px 成立。
3. `metis/desktop/src/styles/beautifului/chat.css:268` → `max-height: 168px;`（同块 269 行 `padding: 0 4px 6px 0;`、271-272 行 `overflow-y: auto; overflow-anchor: none;`）—— 工具分组列表高度成立。
4. `metis/desktop/src/components/chat/AssistantTurnFooter.tsx:75` → `className="cursor-default select-none text-[12px] leading-none text-ink-3"` —— 回合页脚 12px 成立。
5. `metis/desktop/src/components/chat/AgentBubble.tsx:39` → `className="w-full min-w-0 max-w-full py-0.5 text-[14px] font-normal leading-[1.6] text-ink"` —— 助手正文 14px/1.6 成立。
6. `metis/desktop/src/index.css:66-70` → `--work-item-gap: 12px; --work-final-gap: 24px; --work-turn-end-gap: 20px; ... --work-overflow-fade-height: 24px;` —— 三级间距成立。
7. `metis/desktop/src/styles/beautifului/chat.css:181` → `.turn-final-response.after-expanded-work { margin-top: var(--work-final-gap); }` —— 24px 分界成立。
8. `metis/desktop/src/styles/beautifului/opencode-session.css:379-382` → `.turn-final-response.after-expanded-work { /* ... margin-top: 24px */ margin-top: 24px; }` —— 覆写值一致。
9. `metis/desktop/src/styles/beautifului/chat.css:113` → `.cot-thinking { width: min(100%, 720px); min-width: 0; }` —— 思考块宽度上限成立。
10. `CyberStrikeAI/config.example.yaml:109-110` → `max_total_tokens: 120000` / `max_completion_tokens: 32768` —— 两预算字段默认值成立。
11. `CyberStrikeAI/web/templates/index.html:3759` → `<input type="number" id="openai-max-total-tokens" ... placeholder="120000" min="1000" step="1000" />`；`:3764` → `<input type="number" id="openai-max-completion-tokens" ... placeholder="16384" min="1" step="256" />` —— placeholder 与配置默认值的 120000 一致、16384/32768 不一致的歧义成立。
12. `CyberStrikeAI/web/static/css/style.css:45012` → `.chat-composer-surface.chat-input-primary-row { ... }`，其 `border-radius: 22px` 在 `:45025`，textarea 变体 `border-radius: 22px 22px 8px 8px` 在 `:45063` —— composer 22px 圆角成立。
13. `CyberStrikeAI/web/static/js/chat.js:5046` → `function formatAssistantTurnTokenUsageLabel(usage) {`，`:5047` 取 `usage.totalTokens` 后走 `window.t('chat.turnTokenUsageLabel', { tokens })` 兜底 `tokens + ' tokens'` —— token 标签函数成立。

抽查命令（原始执行）：

```text
git -C D:/profile/metis log -1 --oneline          → 26685fd Merge pull request #51 from Wholiver/fix/server-dirname-esm
git -C D:/profile/CyberStrikeAI log -1 --oneline  → 5affb4d Merge pull request #370 from GQingL/main
```

14. `CyberStrikeAI/web/static/js/monitor.js:6821-6829` → `setToolCallDetailState(item, {`（位于 `:6799` 的 `else if (type === 'tool_call' && options.data)` 分支内、`:6811-6819` 状态判断之后，**无条件执行**）→ 函数体 `:6036-6042`：`item.classList.add('tool-detail-collapsible');` / `item.classList.add('tool-call-collapsible');`，`:6041` `updateToolDetailToggleLabel(item);`；`:6060` → `header.setAttribute('data-tool-detail-label', toolDetailToggleText(expanded) + (expanded ? ' ▴' : ' ▾'));` —— 每条工具调用行可逐行折叠成立。
15. `CyberStrikeAI/web/static/js/monitor.js:6172-6185` → `document.addEventListener('click', function (event) {` → `target.closest('.timeline-item.tool-detail-collapsible')` → `renderToolCallDetailContent(item);`；`:6092-6098` 若已存在 `.timeline-item-content.tool-call-detail-content` 则移除并 `item.classList.remove('tool-call-detail-expanded')`，`:6101` / `:6167-6168` 反之创建并展开；`:6090` 先 `collapseOtherToolCallDetails(item)`，`:6075-6085` 遍历 `timeline.querySelectorAll('.timeline-item.tool-detail-collapsible.tool-call-detail-expanded')` 并 `other.classList.remove('tool-call-detail-expanded')` + `content.remove()` —— 互斥展开成立。
16. 样式侧：`CyberStrikeAI/web/static/css/style.css:46145-46147` `.message.assistant-turn-with-process .timeline-item.tool-detail-collapsible .timeline-item-header::after { content: attr(data-tool-detail-label);`、`:8274-8283` `cursor: pointer`（行与行头）；`:46646-46650` 在 `@media (max-width: 760px)`（`:46620`）内改 `content: "›";`。`chat.js:5233` → `messageElement.classList.add('assistant-turn-with-process');` 是上述 `.assistant-turn-with-process` 前缀选择器的应用前提。

结论：16 条抽查全部与正文一致，无冲突项。

---

## 附录 C：第三轮回源码复核记录（评审意见修复）

本轮所有行号均重新回源码实测，命令在 `D:/profile/*` 下执行（`sed -n '<起>,<止>p'` / `grep -n`），原始输出摘要：

1. `grep -n "^.progress-timeline\|^.progress-timeline.expanded" web/static/css/style.css` → `7669:` / `7675:` —— 过程时间线单容器折叠成立（`max-height: 0` ↔ `2000px`，`transition: max-height 0.3s ease`）。
2. `grep -n "mcp-call-label turn-process-summary" web/static/js/chat.js` → `5218:`；`:5219-5222` 绑定 `toggleProcessDetails`；函数定义 `monitor.js:2575`。
3. `grep -n "function syncAssistantTurnSummary" web/static/js/chat.js` → `5146:`；`turn-process-token-chip` → `5170:`；`'耗时 ' + duration` 兜底 → `5184:`。
4. `grep -n "proposedPlan\|PlanPreview" components/chat/AgentBubble.tsx` → `20:`（`extractProposedPlan`）、`48:`（`<PlanPreview`）。
5. `grep -n "data-composer-plan-slot\|showPlanStack" components/chat/Composer.tsx` → `399` / `593` / `596` / `597` / `601`。
6. `grep -rn "cot-header-bar" desktop/src/components/` → 无输出（exit 1），组件层零使用者。
7. `grep -n "^\\.chat-messages\|^\\.message\\.user {" web/static/css/style.css` → `3500:` / `3821:`。
8. `git -C D:/profile/CyberStrikeAI diff --stat` → `3 files changed, 1140 deletions(-)`。

两处与评审建议数值不一致，按实测记录（已在正文对应位置写明）：`web/static/js/chat.js` 的选项文本行实测是 `:1036`（评审建议改 `:1037`；`:1036` 内容为 `opt.textContent = (ch.name || id) + (ch.model ? ' · ' + ch.model : '')`）；`.chat-messages` 规则块实测起于 `:3500`、止于 `:3511`（评审建议 `3501-3512`），故正文改为按属性定位（`:3504` padding / `:3505` background），避免整块区间误指。

9. **第四轮（G5 复核意见修复）**：P1 已补 §4.2 第 6 条「本轮 Suvex 的复用边界」（**不新增配置字段**，六类现有参数不变），并把同节第 1 条改为「若未来扩展多 provider，可参考…（本轮不做）」；P2 的 4 处锚点全部回源码实测后修正：shimmer `chat.css:529-538`（值在 `:537`）、折叠 `chat.css:68-91`（`1fr` `:73` / timing `:78` / 时长 `:81-83` / `0fr`+opacity `:89-90`）、`settings.js:2649-2667` 与 `:2691-2734`（`120000`/`0` 在 `:2661-2662`，`32768` 在 `:2712`）、`primitives/RecommendationCard.tsx:76`（并注明不在 `components/chat/` 下）。

本轮复核命令与关键输出（在 `D:/profile/metis/desktop/src/...` 与 `D:/profile/CyberStrikeAI/web/static/js` 下执行）：

```text
awk 'NR==537' chat.css
  →   animation: shimmer-text 1.4s linear infinite;
awk 'NR==73||NR==78||NR==89||NR==90' chat.css
  →   grid-template-rows: 1fr;
      transition-timing-function: cubic-bezier(0.2, 0.8, 0.2, 1), ease;
      grid-template-rows: 0fr;
      opacity: 0;
grep -n "function writeAIChannelToMainForm\|function ensureAIConfigShape" settings.js
  →   2649:function ensureAIConfigShape(cfg) {
      2691:function writeAIChannelToMainForm(id) {
grep -n "const isLong" primitives/RecommendationCard.tsx
  →   76:  const isLong = Boolean(collapsedBody) || (typeof rawBody === 'string' ? ... );
git -C D:/profile/metis status --porcelain
  →   （空输出）
```

结论：P1 边界与 P2 四处行号均已在源码核对后落笔，两个参考仓库零改动，`docs/chat-reference-notes.md` 为唯一修改文件。

10. **第五轮（G5 复核意见修复：「逐行折叠」误判）**：原文 §3.4 的三条否定断言中，第三条（逐行折叠）为**假**——实测每条 `tool_call` 行都有互斥的逐行详情折叠。本轮按下列链路回源码复核后改写 §3.4 与 §4.3，并在附录 B 补入第 14/15/16 条行级锚点。

```text
cd D:/profile/CyberStrikeAI/web/static/js
awk 'NR>=6821 && NR<=6821' monitor.js   →         setToolCallDetailState(item, {
awk 'NR>=6799 && NR<=6799' monitor.js   →      } else if (type === 'tool_call' && options.data) {
awk 'NR>=6039 && NR<=6041' monitor.js   →         item.classList.add('tool-detail-collapsible');
                                            item.classList.add('tool-call-collapsible');
                                            updateToolDetailToggleLabel(item);
awk 'NR>=6060 && NR<=6060' monitor.js   →         header.setAttribute('data-tool-detail-label', toolDetailToggleText(expanded) + (expanded ? ' ▴' : ' ▾'));
awk 'NR>=6090 && NR<=6090' monitor.js   →         collapseOtherToolCallDetails(item);
awk 'NR>=6174 && NR<=6183' monitor.js   →     document.addEventListener('click', function (event) {
                                            ... target.closest('.timeline-item.tool-detail-collapsible')
                                            ... renderToolCallDetailContent(item);
awk 'NR>=6101 && NR<=6101' monitor.js   →     content.className = 'timeline-item-content tool-call-detail-content';
awk 'NR>=6167 && NR<=6168' monitor.js   →     item.appendChild(content);
                                            item.classList.add('tool-call-detail-expanded');
cd ../css
awk 'NR>=46145 && NR<=46147' style.css → .message.assistant-turn-with-process .timeline-item.tool-detail-collapsible .timeline-item-header::after,
                                        .message.progress-message ... {
                                            content: attr(data-tool-detail-label);
awk 'NR>=8274 && NR<=8275' style.css   → .timeline-item.tool-detail-collapsible {
                                            cursor: pointer;
awk 'NR>=46646 && NR<=46648' style.css → （@media (max-width: 760px) 内）content: "›";
cd ../js
awk 'NR==5233' chat.js                 →     messageElement.classList.add('assistant-turn-with-process');
grep -n "setToolCallDetailState(" monitor.js → 4476/4561/4806/6192/6228/6532/6821/6865 等，全部位于 tool_call / tool_result 路径；thinking 分支（:6794）无调用
```

据此 §3.4 的否定断言收窄为两条真实差异（**没有分组头 / 没有聚合式摘要**），并明确「只有 thinking / reasoning 行是纯平铺」。本轮同样只改 `docs/chat-reference-notes.md`，`D:/profile` 两个仓库未产生新改动。

文末自检（在仓库根 `C:/Users/Administrator/Desktop/新项目` 执行）：

```text
grep -n "逐行详情折叠\|聚合式" docs/chat-reference-notes.md                          → 5 行（:212 / :213 / :243 / :326 / :355）
grep -n "6821-6829\|6036-6042\|6055-6061\|6075-6085\|6172-6185\|46145-46147" ...     → 4 行（:213 / :280 / :281 / :282）
grep -c '^## ' docs/chat-reference-notes.md                                          → 8
git -C D:/profile/metis status --porcelain                                           → （空输出）
git -C D:/profile/CyberStrikeAI status --porcelain                                   → 仍为既有 3 个 ` D`（上一轮之前就存在，未新增）
```
