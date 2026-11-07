---
target: 审查我前端的所有组件
total_score: 26
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 3
p2_count: 2
target_identity: "file:C:\\Users\\Administrator\\Desktop\\新项目\\suvex-web\\src\\components"
timestamp: 2026-11-07T13-13-54Z
slug: src-components
---
Method: dual-agent (A: Explore 设计审查子代理 · B: 证据核验子代理)

# Suvex 前端设计审查

审查范围：`src/components`（105 个非测试组件）+ 承载它们的关键页面与路由。真机环境 `http://192.168.44.128:8080`，1440×900 与 390×844 两档，深浅主题分别核验。

## Design Health Score

| # | 启发式 | 分 | 关键问题 |
|---|---|---:|---|
| 1 | 系统状态可见性 | 3 | 2 天前的事件仍叫「实时活动」；IR 复盘失败只藏在灰色副标题；所有路由共用同一个 `document.title` |
| 2 | 系统与真实世界匹配 | 3 | 「待处置风险 270.5 极高」无量纲、无阈值；扫描列表严重度只有彩点和数字 |
| 3 | 用户控制与自由 | 3 | 暂停/继续、回收站、两步建复盘做得好；删除仍与编辑同权重；landing 主题菜单选项点不动 |
| 4 | 一致性与标准 | 2 | 设计系统禁止的导航树/指标卡/胶囊都在线上；高危色绕过 token 写死 `#E08C82` |
| 5 | 错误预防 | 3 | 确认框与回收站有效；排序按钮 8.7×24、不显眼的删除在制造误触 |
| 6 | 识别而非回忆 | 2 | 严重度彩点、折叠侧栏、移动端图标会话入口都要求记忆；触屏没有 `title` |
| 7 | 灵活性与效率 | 2 | 搜索/排序/导出/命令面板齐全，但 121 条漏洞全量渲染、`/files` 5000 行 20,008 个按钮无虚拟化 |
| 8 | 美学与极简 | 3 | 深色克制有辨识度；浅色偏通用；Overview 把同一组严重度数说两遍 |
| 9 | 错误识别与恢复 | 3 | provider 502 文案是范本；IR 失败态没有同等级的可恢复入口 |
| 10 | 帮助与文档 | 2 | 内联解释优秀，但 StatCard 角标图标不可点、风险分无解释入口 |

**合计 26/40。** 最弱是一致性（4）与识别/帮助（6、10），最强是状态反馈与内容诚实度（1、9）。

## Deterministic Scan

`impeccable detect --json src` → `[]`，0 条命中（`src/components` 同为 `[]`）。组件没有踩到探测器认得的那类硬伤（纯黑阴影、渐变文字、滥用 pill 等）。**探测器干净不等于审美没问题**：下面所有结论都来自真机实测与源码核查。

**覆盖层**：本轮未注入用户可见的浏览器覆盖层，Assessment B 用 DOM 尺寸、computed style、WCAG 相对亮度实算与控制台日志作为证据。因此本报告不声称存在任何覆盖层。

## Design Specificity Verdict

**七分专属、三分通用后台。**

专属的部分换名字就说不通：首页把「它做什么、不做什么」写成两栏边界；漏洞详情把「为什么判断有问题」、置信度依据、反证、验证脚本放在主位；工具页搜索提示是「名字 / 中文名 / 用途 / 默认会漏什么」；技能库写明「统计口径证明『请过这一篇』，不证明『读进去了』」；报告页把纸当成另一种介质；应急响应解释为什么中间留一步给你看一眼。

通用的是外壳：14 项扁平侧栏 + 顶部状态条 + 4 张等大指标卡 + 筛选胶囊 + 表格，任何后台都能套。`/chat` 是 ChatGPT/Claude 的三栏克隆，源码注释自己写着「对标 ChatGPT / Claude」。浅色主题尤其像通用 SaaS admin；深色主题靠线场、近黑底与克制的 violet 才明显像 Suvex。

最刺眼的自相矛盾：设计系统 §0/§1 明确写「没有导航树」「没有指标卡」，线上首屏就是 14 项导航和 4 张指标卡。产品已经长成多模块控制台，外壳还停在「单列行为流」的宣言上。

## Overall Impression

内容层是一个有观点的安全工具，外壳层是一个合格的后台模板。最值得做的一件事不是换皮，而是把「证据链」这个真正的产品资产从详情抽屉里提出来，变成首屏和导航的组织原则。

## What's Working

1. **产品首页**（`src/pages/LandingPage.tsx`）：大标题只说行为和边界，一个 violet 实心 CTA，其余是描边与文字；页脚明说首页不读取运行数据。这是全站最像「有主张的产品」的一屏。
2. **漏洞详情抽屉**（`src/components/findings/FindingDetail.tsx`）：严重度、验证状态、PoC 状态三徽标并列，元数据等宽，证据与反证摊开，处置按钮常驻。它把证据链当主内容，是 Suvex 最不可替代的界面。
3. **诚实文案与状态设计**：Tools 的「默认会漏什么」、Skills 的「不证明读进去了」、provider 502 的「这不是你的配置问题」。这些让界面可信，也是竞品抄不走的。

## Priority Issues

### [P1] 颜色 token 失控：浅色主题的「高危」只有 2.34:1

**What**：`src/data/findings.ts:57` 把 high 文字写死成 `text-[#E08C82]`。实测浅色 `rgb(224,140,130)` on `rgb(244,245,247)` = **2.34:1**（11px）。「未指派」用 `text-ink-ghost`（`FindingsTable.tsx:196`），浅色 **1.89:1**、深色 **2.17:1**，两个主题都不合格，而 `ink-ghost` 在设计系统里写明「仅装饰/禁用，禁止承载信息」。
**Why it matters**：高危是最需要一眼看到的一级；默认浅色主题下它最读不清。这也是唯一绕过 token 的地方——同一个红色在别处走 `text-signal-risk`。
**Fix**：把 high 的文字色改成 `text-signal-risk`，`#C2544A` 只留给圆点与图表填充；新增 `--suvex-signal-high` 文字 token 并在两套主题按实测值定义。「未指派」改用 `ink-faint`，全站再扫一遍 `ink-ghost` 承载信息的地方（`CompareDialog.tsx:198` 的「没有」同样 1.89:1）。
**Suggested command**: `/impeccable colorize`

### [P1] 手机端多个路由整页横向溢出，触控目标 95% 不达标

**What**：390px 下 `scrollWidth` 超出 380 的布局视口：`/tools` **+258**、`/kernel` **+209**、`/files` **+168**、`/overview` **+105**。根因分别是「参数：command（必填…）」不换行的 `<p>`、kernel 卡片实宽 565 塞进 332 的 grid 列、overview 活动流摘要里的长命令（`span.truncate` 内容宽 4846）。
触控目标（宽或高 <32px）占全站 **20,254 / 21,232 = 95.4%**：排序 ↓ **8.7×24**、kernel「复制答案」**15×15**、files 行内按钮 **24×24**、findings 筛选胶囊 **73.7×28**。
**Why it matters**：整页可左右拖动不是视觉瑕疵，是手机上坏掉；8.7px 宽的排序箭头基本点不中。
**Fix**：给长文本容器统一 `min-w-0 break-words [overflow-wrap:anywhere]`；kernel 网格加 `min-w-0`；把 24px 级图标按钮提到 32px 可视 + 44px 命中区（`after:absolute after:-inset-2` 之类），排序按钮加 `shrink-0`。补 390/320 两档回归断言：`scrollWidth <= clientWidth`。
**Suggested command**: `/impeccable adapt`

### [P1] `/files` 5000 行全量渲染：文档高 234,855px，20,008 个按钮

**What**：`/files` 未虚拟化，5,000 行产生 **20,008** 个 button、**20,000** 个 <32px 控件，文档高 **234,855px**。
**Why it matters**：这是全站最重的 DOM，滚动、筛选、Tab 遍历都被它拖垮；键盘用户要经过两万个停靠点。
**Fix**：接虚拟列表（或分页 + 服务端筛选），行内动作收进一个「更多」菜单，键盘只在可视区可达。同类问题在 `/findings`（30,129px、121 行）上也存在，只是量级小。
**Suggested command**: `/impeccable optimize`

### [P2] 导航与首屏层级：14 项无分组，核心活动流在首屏之外

**What**：`src/navigation.ts` 14 个一级入口平铺无分组；1440px 下进入 `main` 要 Tab 过 **17** 个可聚焦元素，全站 **0** 个 skip link。`/overview` 顺序是 4 张指标卡 → 该做什么 → 可用能力 → 活动流，实测活动流标题 1440 下 `y=859`、390 下 `y=1570`；0 个扫描运行时它展示的是 2 天前的事件却仍叫「实时」。
**Why it matters**：产品承诺「看它每一步在想什么」，控制台第一屏却是一张静态报表；第一印象和定位对不上。
**Fix**：导航加 3–4 个分组标题（运行 / 资产与产出 / 自动化 / 能力与配置），低频项收进「更多」；`AppShell` 的 nav 前加 skip link，`main` 加 `id="main"`；Overview 把 ActivityFeed 提到指标卡之前，4 张卡压成一行 strip，标题按是否有运行中任务切换「实时活动 / 最近活动」。
**Suggested command**: `/impeccable layout`

### [P2] Findings 手机端：筛选栅格塞进了动作按钮，主操作被压成竖排

**What**：`FindingsPage.tsx:913` 的栅格把「未删除 / 回收站 / 导出全部 / SARIF」当筛选字段排，390px 下第三行断成「未删 / 除」「回 / 收 / 站」「导 / 出 / 全 / 部」，首屏 60% 被筛选占掉，首条数据在 615px。121 条列表 30,129px，无分页，筛选条不 sticky。
**Why it matters**：最常用的处置页在手机上主操作不可读、不可点，改一次筛选要滚回三万像素的顶部。
**Fix**：栅格只留筛选字段，范围切换与导出拆到独立 action 行或「更多」菜单；筛选条 sticky；列表分页或虚拟化。
**Suggested command**: `/impeccable layout`

## Persona Red Flags

**Jordan（第一次用）**
- 控制台首屏只有「0 / 3 / 72 / 17 分 29 秒」，没告诉他现在该做什么；「该做什么」在手机上要滚到 896px。
- 「实时活动」显示 2 天前的命令，他会以为 agent 正在跑。
- 14 个侧栏项没有分组，也没有一句「从扫描任务开始」的引导。
- 移动端会话列表只是一个 38×32 的图标按钮，第一次用不知道历史对话在那。

**Sam（键盘 / 无障碍）**
- 1440 下 17 个 Tab 才到正文，无 skip link；Findings 有 278 个可聚焦元素，121 行全在 Tab 顺序里。
- 浅色「高危」2.34:1、「未指派」1.89:1；深色「未指派」2.17:1。
- Scans 的「●1 ●2 ●1」圆点 `aria-hidden`，数字只有颜色，读屏器只念一串数字。
- landing 主题菜单的三个选项被 `<main>` 拦截，鼠标和触屏都点不到（键盘可到达）。

**Casey（手机）**
- Overview 横向可滚 105px，长命令把整页撑宽，右侧被裁。
- `/files` 234,855px 高、两万个按钮；Findings 30,129px 无分页。
- 排序箭头 8.7×24、视图切换 44×26、筛选按钮 50×26，普遍低于 44px 触控目标。

## Minor Observations

- **报告页主操作在首屏之外**：「生成并导出」`y=1002`（视口 900），而 tab 叫「生成报告」，两个词像同一个动作。
- **landing 主题菜单是死控件**：header 与 main 都是 `relative z-10`，header 在前，内部 `z-30` 越不过 main；`elementFromPoint` 三个选项都命中 MAIN。
- **同一个模型名显示两次**：顶部条与 Settings 行都是 `deepseek-v4.1-flash deepseek-v4.1-flash`，Overview token 表写成括号重复。
- **Assets 表头在中文词中间断行**：「未处置」变「未处 / 置」；给列加 `whitespace-nowrap`。
- **`/assets` 直接访问 404**：nginx 的 `/assets/` 静态目录抢了路由（301→404），客户端跳转正常。
- **Chat 空标题会话**：`ChatPage.tsx:2189` 没有 fallback，线上出现只有「0 条」没有标题的行。
- **所有路由浏览器标题相同**：`index.html:19` 固定一句，无按路由更新。
- **风险分 270.5 与进度条不自洽**：分数夹到 100% 当条宽，没有下一档阈值。
- **138 处非 token 字号**（`text-[11px]/[12px]/[13px]`）集中在 files / schedules / skills / tools / kernel / CommandPalette，对话页是唯一干净的一处；用户气泡 `rounded-[10px]` 也不在 6/8/12 刻度上。
- **减少动效做对了**：`prefers-reduced-motion: reduce` 下所有 animation 压到 1e-06s，无残留过渡。
- **控制台干净**：/overview、/chat、/findings、/scans、/reports、/tools 均 0 error，全 session 仅 `/assets/` 404 两条。

## Questions to Consider

1. 如果核心承诺是「看 agent 每一步在想什么」，为什么控制台第一屏是静态指标看板、活动流在首屏之外？是重排 Overview，还是承认产品已经是控制台、改写首页承诺？
2. 设计系统禁止导航树、指标卡、胶囊，线上却全都有。接下来以哪边为准：修订设计系统，还是把界面收回去？两套说法并存，评审和实现会继续分叉。
3. Chat 是全站最通用的表面。对渗透测试 agent，chat 真该是主入口，还是让「行为流」做主入口、chat 退成命令面板？
4. 「270.5 极高」要改变用户的什么决定？如果它不能回答「先修哪一条」，也许该换成按严重度排序的行动队列，而不是一个更漂亮的仪表盘。
