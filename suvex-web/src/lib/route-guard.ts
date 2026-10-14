import type { AuthPhase } from '@/data/auth-store'

/*
  ─────────────────────────────────────────────────────────────
  路由守卫的**决策**（纯函数，2026-10-14 抽自 App.tsx）

  ## 为什么把这三个判断抽出来

  它们此前是 `App.tsx` 里的三段 `if`（`RequireAuth` / `LoginRoute` /
  `SetupRoute`），**没有任何测试**。而它们决定的是"一个未登录的人
  能看见什么" —— 判断写错就是**把受保护页面放给未登录用户**，
  或者反过来把已登录的人永远卡在登录页。这是渲染期崩溃之外
  第二类"只有人工点开页面才能发现"的问题（README 缺口第 1 条）。

  抽成纯函数之后，"5 个 phase × 3 个入口"变成一张可以表驱动断言的表。

  ## ★ 这是**重构**，不是改功能

  抽取时逐条对齐了原来三段 `if` 的**顺序**（顺序在这里是有意义的：
  先判 loading，再判该入口特有的重定向，最后才落回渲染）。
  测试里每一条都对着原代码抄了一遍期望值，**没有顺手"优化"任何行为**。

  ## 为什么这里**不** import `DEFAULT_PATH`

  它返回的是 `'default'` 这个**语义目标**，由 `App.tsx` 映射成
  `DEFAULT_PATH`。这样本模块只依赖一个**类型**（`AuthPhase`，
  编译后消失），测试就不必把 `navigation.ts`（连带 `lucide-react`）
  整条依赖链拉进来 —— 测试只验判断逻辑，不该被图标库拖慢。

  ## 为什么用可辨识联合而不是返回字符串

  三种结果对应的 JSX 完全不同（全屏兜底 / `<Navigate>` / 渲染自己
  的内容）。返回 `'boot' | 'setup' | 'login' | 'ready'` 这种字符串会让
  调用方必须再写一次 `phase` 到"渲染什么"的映射 —— 那等于把刚抽走的
  判断又抄回去一份，两处迟早漂移。
  ─────────────────────────────────────────────────────────────
*/

/**
 * 一个守卫该做什么。
 *
 * - `boot`     —— 还在问 `/auth/state`，显示全屏兜底（避免闪一下登录页）
 * - `redirect` —— 跳走。`to` 是**语义目标**，由调用方映射成真实路径
 * - `render`   —— 放行，渲染该守卫自己的内容（`Outlet` / 登录页 / 初始化页）
 */
export type RouteDecision =
  | { kind: 'boot' }
  | { kind: 'redirect'; to: 'setup' | 'login' | 'default' }
  | { kind: 'render' }

/**
 * 受保护区（六个 app 页面）。**`/` 首页刻意不在保护范围内** ——
 * 理由见 `App.tsx` 里 `RequireAuth` 上那段注释（首页正是给未登录的人看的）。
 */
export function decideRequireAuth(phase: AuthPhase): RouteDecision {
  if (phase === 'loading') return { kind: 'boot' }
  if (phase === 'setup') return { kind: 'redirect', to: 'setup' }
  if (phase === 'login' || phase === 'offline') return { kind: 'redirect', to: 'login' }
  return { kind: 'render' }
}

/**
 * `/login` —— 已登录的人不该停在这里。
 *
 * ⚠️ `setup` 也送去初始化页：**还没初始化就没有账号可对**，
 * 让用户对着一个登不进去的表单试是纯浪费。
 */
export function decideLoginRoute(phase: AuthPhase): RouteDecision {
  if (phase === 'loading') return { kind: 'boot' }
  if (phase === 'setup') return { kind: 'redirect', to: 'setup' }
  if (phase === 'ready') return { kind: 'redirect', to: 'default' }
  return { kind: 'render' }
}

/** `/setup` —— 已初始化后这个入口就不该再可达。 */
export function decideSetupRoute(phase: AuthPhase): RouteDecision {
  if (phase === 'loading') return { kind: 'boot' }
  if (phase === 'ready') return { kind: 'redirect', to: 'default' }
  if (phase === 'login' || phase === 'offline') return { kind: 'redirect', to: 'login' }
  return { kind: 'render' }
}
