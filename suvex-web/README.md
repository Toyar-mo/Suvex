# suvex-web

Suvex —— 一个自治 AI 安全测试 agent 的 Web 控制台。

> **渊源**：本项目最初是为开源项目 [strix](https://github.com/usestrix/strix)
> （自治 AI 渗透测试 agent，Python + Textual CLI）做的 Web 前端，
> 现已作为独立品牌 **Suvex** 继续演进。上面那个仓库是**第三方**上游，
> 与本项目的代码、命名无关 —— 引用它是因为"这类 agent 的形态"来自那里。
> 如果你在代码或文档里看到 `strix` 字样，先确认它指的是上游工具
> （如 `usestrix/strix` 这类外部标识）还是我们的历史命名残留。
>
> **改名范围**（2026-02 由 strix → Suvex）：品牌文案、包名、CSS 变量、
> storage key、镜像名、CLI 示例、API token 前缀（`svx_`）已全部改完；
> 上游仓库地址与我们**不拥有**的第三方标识保持原样。

> **当前进度：六个功能页 + CTF2 解题页 + 产品首页均已实现并接真实接口。**
> 数据层**不再有 mock** —— `src/data/` 下的 store 全部走 `lib/api.ts`，
> 登录页、路由守卫、错误边界、主题切换都已就位。

## 这个前端现在是什么状态

| 项 | 实际数字 |
| --- | --- |
| 代码规模 | **41,941 行** TS/TSX，151 个文件 |
| 页面 | 总览 / 扫描任务 / 智能体活动 / 漏洞发现 / CTF2 解题 / 报告 / 设置 + 产品首页 |
| 数据层 | 全部接真实 API，**0 个 mock 数据文件** |
| 类型安全 | **0 个 `@ts-ignore`；3 处 `any`**（都在 `components/effects/DotGrid.tsx` 的节流函数，上游基线代码 —— 见根 README 的更正）（`tsc -b` 与 `eslint --max-warnings 0` 全绿） |
| 契约校验 | `verify:all` 三项全绿（字段对账 / 端点覆盖度 / 报告一致性） |
| 单元测试 | **148 个**（vitest，8 个文件），覆盖 5 个纯逻辑模块 + 路由守卫 + 401 拦截 + 错误态 |

### 测试覆盖了什么

| 模块 | 用例 | 盯的是什么 |
| --- | --- | --- |
| `hooks/useActivityStream` | 22 | 批量合并、按 id 去重、上限截断、暂停缓冲、agent 派生、状态映射 |
| `lib/validate-target` | 26 | 目标校验与归一化（含 `service` 的粘贴识别、`artifact` 的空值语义） |
| `lib/finding-query` | 22 | 筛选（维度内"或"、维度间"与"）、**facet 角标只忽略自己那一维**、导出映射不静默丢弃 |
| `lib/route-guard` | 25 | **三个路由守卫的决策表**（5 个 phase × 3 个入口）+ 三条跨守卫约束（不能形成重定向死循环） |
| `lib/unauthorized-flow` | 17 | **401 链路**：`apiRequest` 与 `apiText` **两条**路径都触发 handler、`skipAuthRedirect` 真的跳过、`handleUnauthorized` 幂等 |
| `components/ui/ErrorState` | 14 | **"连不上"与"后端说不行"两条分支**（文案、错误码显示与否、重试按钮的有无） |
| `data/users-store` | 12 | **防自锁判断**（不能删最后一个管理员 / 不能删自己 / 不能降级最后一个） |
| `data/scans-store` | 10 | in-flight 去重的复用规则表（含两条"刻意不合"的格子） |

**没有覆盖的**：所有页面组件本身、表单交互、主题切换。

> ⚠️ **2026-10-14 补测时的两个坑**（后续写组件测试会再遇到）：
>
> 1. **必须手动 `cleanup()`** —— `@testing-library/react` 的自动清理挂在
>    全局 `afterEach` 上，而它只在 `globals: true` 时注册；本项目刻意关了
>    globals（见 `vitest.config.ts`），于是 DOM 会在用例间累积，
>    报错是 `Found multiple elements`（看起来像组件渲染了多个，其实是上一个用例没清）。
> 2. **没有 `toHaveTextContent`** —— 它来自 `@testing-library/jest-dom`，
>    而本项目没装。用原生 `textContent` + `toContain` 断言。
>
> 另外：`lib/api.ts` 里有**两个** 401 处理点（`apiRequest` 与 `apiText`）。
> 第一版测试只覆盖了前者 —— **变异验证当场抓到**（把后者的处理改成
> `if (false)`，测试照样全绿）。所以现在两条路径都有用例。
也就是说，一个渲染期的崩溃仍然只有人工点开页面才能发现。

```bash
npm run test          # 跑一次
npm run test:watch    # 开发时盯着
npm run test:coverage # 带覆盖率
```

> **为什么先补这五个模块**：它们是纯逻辑、不依赖 DOM，而且**正是出过
> bug 的地方**。2026-10-10 修的"批次内重复漏网"就在 `useActivityStream`
> 里 —— 那处缺陷 `tsc` 与 `eslint` 全绿，`verify:*` 也全绿。
>
> 这套测试做过**突变验证**（把关键逻辑逐个破坏，确认对应测试变红，
> 而不是"永远为真的空测试"）：`useActivityStream` 8 处、
> `scans-store` 6 处、`validate-target` 与 `finding-query` 6 处，
> 全部被捕获。
>
> ⚠️ 突变验证还**补出过两个测试盲区**：
>
> 1. `facetCounts` 那组用例里，断言恰好与"忽略所有维度"的错误实现
>    在数值上一致 —— 于是改成 `emptyFilter()` 时没有任何测试变红。
>    补了一条"搜索词确实排除部分数据"的用例才逼出它。
>    **断言本身没错，但它没把实现逼到会出错的那条路径上。**
> 2. 后端的"用户不存在也跑一次 argon2"（时序侧信道防线）——
>    既有测试只比对**文案**，没测耗时。补了一条直接量耗时的用例
>    （实测 argon2 单次 44ms、纯比较 0.2µs，差 22 万倍）。
>
> **这类盲区只有突变验证能发现** —— 靠 review 断言是看不出来的
> （每条断言单看都对）。

## 跑起来

```bash
npm install
npm run dev          # http://localhost:5173
```

| 命令                      | 说明                                   |
| ------------------------- | -------------------------------------- |
| `npm run dev`             | 开发服务器（HMR）                      |
| `npm run build`           | 类型检查 + 生产构建                    |
| `npm run preview`         | 预览构建产物                           |
| `npm run typecheck`       | 只跑 `tsc`                             |
| `npm run test`            | 单元测试（vitest，单次跑）             |
| `npm run test:watch`      | 单元测试（watch 模式）                 |
| `npm run lint`            | ESLint（`--max-warnings 0`）           |
| `npm run format`          | Prettier 写入                          |
| `npm run verify:all`      | 全部校验（契约 + 覆盖度 + 端点 + 报告） |
| `npm run verify:contract` | 契约字段 ↔ 源码类型 对账（17 个枚举）  |
| `npm run verify:coverage` | 契约端点覆盖度（对照需求清单）         |
| `npm run verify:endpoints`| **反向**端点对账（后端代码 → 契约）    |
| `npm run verify:report`   | 报告预览 = 导出 一致性                 |

> `verify:*` 系列不是可选的锦上添花：它们把"文档与实现一致"这种
> 靠人保证就会漂移的事变成可复跑的命令。改契约或改类型定义后请跑一次。
>
> ⚠️ **`verify:contract` 有一个已知弱点**：它按**子串**在整个契约文档里
> 找枚举取值，而像 `service` 这样的词在别处出现过 —— 于是
> `TargetKind` 缺了 `service` 这个取值**一直没被报出来**。
> 看到 `✓` 不等于该项真的被独立验证过。

## 技术栈

Vite 8 · React 19 · TypeScript 6 · Tailwind CSS 3 · ReactBits（源码拷贝，非 npm 依赖）

## 设计方向

**先读 [`src/design-system.md`](src/design-system.md)** —— 配色 / 字体 / 间距 / 动效的
决策与理由都在那里，也是 review 时的判定依据。

一句话差异点：CyberStrikeAI 的 Web 界面叙事单位是「一次渗透任务」（侧栏会话 +
聊天 + 文件 diff），Suvex 的叙事单位是「agent 此刻在做什么」——界面是一条持续
流动的行为流，任务和报告都是这条流的产物。

## 后端接入

**契约先读 [`docs/api-contract.md`](docs/api-contract.md)** —— 全部 REST 端点、
实时事件协议、统一错误格式、鉴权级别与高风险操作的授权确认设计。
后端按它实现，前端按它接入。

> **三项已定**（契约 §0 有完整理由）：
>
> | 议题     | 决策                                                                                                          |
> | -------- | ------------------------------------------------------------------------------------------------------------- |
> | 实时通道 | **SSE**（事件流单向、断线补发内建、前端已是此形状）                                                           |
> | 登录     | **要**：单管理员 + 会话 Cookie + 机器令牌。理由是**CSRF** —— 无鉴权的 JSON API 会让任意网页替用户发起攻击扫描 |
> | 主键     | **字符串**（前端 `id` 全是 `string`）                                                                         |
>
> 字段名与枚举取值是**从 `src/data/*.ts` 逐字提取**的，并有脚本做机器化对账
> （`npm run verify:all`，含"决策无矛盾"检查）。

> ⚠️ **加登录意味着多出一块前端工作**（合集里没列）：`/auth/state` 启动查询、
> 初始化页、登录页、路由守卫、401 拦截、`credentials: 'include'`。
> 清单见契约 §8。

## 文案语言

**界面文案统一中文**，后续新增页面与组件同样适用。

译句子但不译标识符：路由路径、端点、工具名、模型名、ID、严重度代码
（`high`/`critical`）一律保持原文——判断标准是「这个字符串会不会被复制到别处使用」。

字体栈已配好中文回退（Geist → Noto Sans SC），字阶与行高按中文调过
（无负字距、行高更松、≤13px 提到 450 字重）。详见 design-system.md §3。

## 目录

```
src/
  App.tsx                    路由表（根路径 = 产品首页）
  design-system.md           设计决策与理由 —— 动手前先读
                             §5.1 动效规范 / §15 taste 复查记录
  index.css                  token 的 CSS 变量 + 浏览器默认件主题化 + 微反馈原子
  navigation.ts              导航信息架构单一事实来源
  components/effects/        背景动效（含 ReactBits 拷贝件，见其 README）
  components/landing/        产品首页的 Hero（标题动效 / 背景）
  components/layout/         布局壳（AppShell / Sidebar / PageTransition 等）
  components/{overview,activity,scans,findings,reports,settings}/
                             各功能页的组件
  components/ui/             无状态基础件（StatusDot / CodeBlock / Drawer）
  data/                      数据模型与数据源（**全部走真实 API**，见 lib/api.ts）
  pages/                     页面（LandingPage 同步引入，其余走 lazy 分包）
  hooks/                     媒体查询 / 减少动效 / 首帧信号 / 状态变化信号
  lib/                       纯函数工具（格式化 / 查询 / 报告 IR）
```

## 环境变量与凭据

按项目安全规范：**任何 token / 私钥 / 密码一律走环境变量，不写进代码、不进仓库。**
本地用 `.env.local`（已在 `.gitignore` 中排除），仓库里只提交 `.env.example`。

## 第三方组件

`src/components/effects/` 下有 ReactBits 组件的源码拷贝（MIT + Commons Clause），
出处、许可证和本地改动逐条记录在该目录的 [`README.md`](src/components/effects/README.md)。
它们是拷贝件而非依赖，升级时对着上游 commit 做 diff。

> 任务 8 移除了 `AnimatedContent.tsx`（原因是它"先隐藏、等 JS 放行"的失败模式，
> 以及会把 gsap 钉进主 chunk）。恢复方式见该 README。
