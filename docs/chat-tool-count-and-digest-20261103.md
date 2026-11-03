# 对话页：live 阶段行「已 N 次工具」+ 工具参数摘要不再硬切（v1.30）

日期：2026-11-03

## 背景
两处「看着像卡住、其实没问题」的界面在误导用户：

1. **live 阶段行没有工具计数**：一轮跑起来时，用户只能看到「第 N 轮 · 正在
   执行 run_shell…」，不知道这一轮已经跑了几次工具——无法判断它是在推进还是
   原地打转。
2. **工具参数被硬切 80 字符、什么都不说**：折叠态那行把命令 `slice(0, 80)`，
   用户看到一条断在中间的命令（`export KEY=… ; H=https://pro.dasctf.com; c`），
   第一反应是「命令写了一半 / 卡住了」，实际后端数据完好、点开就是全文。

## 修法

### 1. 阶段行工具计数（ChatPage.tsx）
- 新增 `export function turnToolCount(items: ChatMessage[]): number`：**只数
  `role === 'tool'`**。旁白（`surface: 'process'`）也代表"动过"，但每轮开头
  模型必先写一句旁白，把旁白算进去会让计数在真正执行第一个工具之前就从 1
  开始，反而看不出工具推进。
- 给 `<AssistantTurn progress={...}>` 接线：
  - 主回合列表里 `progress={isLive ? { tools: turnToolCount(turn.items) } : null}`；
  - 乐观气泡阶段（`liveTurnIndex < 0`）不冒充，`liveLooseToolCount` 返回
    `undefined`，`AssistantTurn` 就不显示计数。
- ★ 关键：HEAD 版 `AssistantTurn.tsx` **早就有** `progress?: {steps?, tools?}`
  属性和 `progressLabel(progress)` 渲染，但**没有任何已提交代码给它传
  `progress`** —— 那套计数逻辑一直是死接线。本次把 ChatPage 这半接上，
  计数才真正显示。

### 2. 工具参数摘要（toolCardDigest.ts）
- `digestToolArgs` 从 `slice(0, 80)` 改为：
  - 短单行（≤80 字符、无换行）原样返回；
  - 否则返回 `首行…（共 N 字符）`，`N` 是**整段命令的总长**（不是首行）。
    判据用总长而非首行超没超：多行脚本首行往往很短，但整段几十行，那才是
    用户"需要点开看"的理由。
- 视觉省略交给 CSS `truncate`；"还有内容"这件事由 `（共 N 字符）` 说出来。
- ★ 纯 helper 抽到独立文件 `toolCardDigest.ts`（不留在组件文件 `ToolCard.tsx`
  里）：从组件文件额外 `export` 纯函数会触发 `react-refresh/only-export-
  components` 警告（本仓库 lint 是 `--max-warnings 0`），搬到独立模块既不破坏
  Fast Refresh，也能被单测直接 import。

## 验证
- **单测**：
  - `src/components/chat/ToolCard.test.tsx`（新建，7 条）：短命令原样、80 字符
    边界、超长单行 `共 N 字符`、多行总长、空白折叠、白名单顺序、空值不崩。
  - `src/pages/ChatPage.test.tsx`：`turnToolCount` 2 条（只数 tool、空回合=0）。
  - `src/components/chat/AssistantTurn.test.tsx`：`progress` 3 条（live 显示
    「（已 3 次工具）」、tools=0 不显示、没传不显示）。
- **全量**：vitest **76 files / 1133 tests passed**；`tsc -b` exit0；
  `eslint` 改动文件无新增 warning（ToolCard 抽模块后归零）；`npm run build` 成功。
- **部署**：dist 打包 → Kali prod `find dist -mindepth 1 -delete; cp -a`；
  `index.html` → `index-BsLKl99a.js`；prod bundle `ChatPage-Qkx_ZTqY.js` 命中
  `次工具`（×2）与 `共 ${t} 字符` —— 两个特性确在线上 bundle 里。
- **实机视觉**：`artifacts/v130-toolcard-chat-page.png`（部署后截图，read_image
  复核）：对话页正常渲染，工具行显示新摘要格式 + 页脚「3 步 · 1 个工具」，
  无回归。

## 诚实边界
- 阶段行计数只在 live（这一轮正在跑）时显示——这是设计，非缺陷：轮次结束后
  页脚已有「N 步 · N 个工具」，live 期间才需要实时进度。
- 计数来源是**已落库的 `turn.items`**（随每次工具结果刷新），不是后端新推
  字段；乐观气泡尚未归入回合时不显示计数（不冒充）。

## 提交
`a38387e8` feat(chat): live 阶段行显示「已 N 次工具」+ 工具参数摘要不再硬切（v1.30）
