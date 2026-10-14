import Layout from '@/components/layout/Layout'
import PageFallback from '@/components/layout/PageFallback'
import PageTransition from '@/components/layout/PageTransition'
import ErrorBoundary from '@/components/ui/ErrorBoundary'
import ToastHost from '@/components/ui/Toast'
import { bootstrapAuth, handleUnauthorized, useAuth } from '@/data/auth-store'
import { setUnauthorizedHandler } from '@/lib/api'
import {
  decideLoginRoute,
  decideRequireAuth,
  decideSetupRoute,
  type RouteDecision,
} from '@/lib/route-guard'
import { DEFAULT_PATH, NAV_ITEMS } from '@/navigation'
import {
  ActivityPage,
  Ctf2Page,
  FindingsPage,
  LandingPage,
  OverviewPage,
  ReportsPage,
  ScansPage,
  SettingsPage,
} from '@/pages'
import LoginPage from '@/pages/LoginPage'
import SetupPage from '@/pages/SetupPage'
import { Suspense, useEffect } from 'react'
import { BrowserRouter, Navigate, Outlet, Route, Routes, useLocation } from 'react-router-dom'
import type { ComponentType, ReactNode } from 'react'

/**
 * 路由表。6 个一级入口全部来自 navigation.ts，路径不在这里二次硬编码 ——
 * 靠 map 遍历生成，避免「加了导航项但忘了加路由」。
 *
 * 六个功能页走 `lazy`（见 pages/index.tsx 的分包说明），
 * 所以这里必须套 Suspense；设置页是同步组件，包在里面也无害。
 */

/**
 * 类型用 ComponentType 而不是 `() => ReactNode`：
 * lazy() 返回的是 LazyExoticComponent（一个惰性组件类型），
 * 用"无参函数返回 ReactNode"去描述它不准确，也会在挂载 <Page /> 时丢失 props 类型。
 */
const PAGE_COMPONENTS: Record<string, ComponentType> = {
  overview: OverviewPage,
  scans: ScansPage,
  activity: ActivityPage,
  findings: FindingsPage,
  reports: ReportsPage,
  ctf2: Ctf2Page,
  settings: SettingsPage,
}

/**
 * 路由切换时回到页首。
 *
 * 必须处理：从长页面滚到底再点导航，新页面会停在同样的滚动位置，
 * 用户看到的是页面中段，会以为点错了。
 * 只在 pathname 变化时触发，带 hash 的锚点跳转不受影响。
 *
 * ★ 任务 8 改：`behavior: 'smooth'` → `'auto'`（瞬时）。
 *
 *   上一版写的是 `reduced ? 'auto' : 'smooth'`，理由是"平滑一点更好看"。
 *   但项目自己在 design-system §12 里已经记过这个坑：**平滑滚动会在某些
 *   环境里静默 no-op** ——
 *
 *     window.scrollBy({ top: 176, behavior: 'smooth' }) → scrollY 0    （没动）
 *     window.scrollBy({ top: 176, behavior: 'auto' })   → scrollY 176  （正常）
 *
 *   无头浏览器、后台标签页、部分 webview 都会这样：只要合成器没有帧在跑，
 *   平滑动画就不推进。而它失败的方式最坏 —— 不报错、不滚动，用户以为点错了。
 *
 *   把内容带进视野是**功能性动作**，不是装饰。功能性动作不能依赖动画能不能
 *   播出来。代价是失去一点平滑感，换可预期 —— 这笔交易在这里很划算，
 *   因为"点导航后停在中段"正是这段代码存在的唯一目的。
 */
function ScrollToTop() {
  const { pathname } = useLocation()

  useEffect(() => {
    window.scrollTo({ top: 0, behavior: 'auto' })
  }, [pathname])

  return null
}

/**
 * 启动时的全屏兜底。
 *
 * 与 `PageFallback` 分开是有理由的：那个块必须在 AppShell 的 flex 高度链里
 * （`flex min-h-0 flex-1`）才能不掐断内部滚动区；而这一屏出现时
 * **AppShell 还没挂载**，用它的类名会得到一个高度塌成 0 的空白页。
 */
function BootingFallback() {
  return (
    <div
      className="flex min-h-dvh items-center justify-center bg-canvas"
      role="status"
      aria-live="polite"
      aria-busy="true"
    >
      <span className="sr-only">正在检查登录状态</span>
      <div className="h-4 w-40 animate-skeleton rounded-xs bg-surface-raised" />
    </div>
  )
}

/**
 * 受保护区：未登录一律去登录页（契约 §8）。
 *
 * ⚠️ **`/`（产品首页）刻意不在保护范围内** —— 这是对契约 §8
 * 「未登录一律重定向到登录页」的一处收窄，理由：
 *
 *   LandingPage 讲的是"Suvex 是什么"，而**它正是给还没登录的人看的**。
 *   把它也挡住，那个页面就只剩已登录用户能看到 —— 而他们已经知道
 *   这是什么了，于是那一页永远没有观众。契约 §7 自己也记着
 *   「第一次打开的人直接落在一个装满演示数据的看板上，
 *   没有任何地方说过 Suvex 是什么」，那说的就是未登录的用户。
 *
 *   所以保护的是**六个 app 页面**（会读到真实数据、会发包的那些），
 *   首页作为公开入口放行。
 *
 * ★ 2026-10-14：判断抽到 `lib/route-guard.ts`（纯函数 + 表驱动测试）。
 *   抽出的动机是这三段 `if` 决定"未登录的人能看见什么"却零测试 ——
 *   判宽了是安全问题、判严了是功能不可用，而两种都不会让构建失败。
 *   这里只剩"决策 → JSX"的映射，判断本身不在这份文件里了。
 */
function decisionToElement(
  decision: RouteDecision,
  render: () => ReactNode,
): ReactNode {
  if (decision.kind === 'boot') return <BootingFallback />
  if (decision.kind === 'redirect') {
    // `to` 是**语义目标**，真实路径只在这里映射一次 ——
    // 守卫模块因此不必 import navigation.ts（测试也就不必拉进图标库）。
    const target =
      decision.to === 'setup' ? '/setup' : decision.to === 'login' ? '/login' : DEFAULT_PATH
    return <Navigate to={target} replace />
  }
  return render()
}

function RequireAuth() {
  const { phase } = useAuth()
  return decisionToElement(decideRequireAuth(phase), () => <Outlet />)
}

/** `/login` —— 已登录的人不该停在这里 */
function LoginRoute() {
  const { phase } = useAuth()
  // `login` 与 `offline` 都渲染 LoginPage，由它自己按 phase 决定显示表单还是错误
  return decisionToElement(decideLoginRoute(phase), () => <LoginPage />)
}

/** `/setup` —— 已初始化后这个入口就不该再可达 */
function SetupRoute() {
  const { phase } = useAuth()
  return decisionToElement(decideSetupRoute(phase), () => <SetupPage />)
}

function AppRoutes() {
  return (
    <Routes>
      {/*
        ★ 根路径 = 产品首页，放在 Layout **外面**（也不在 RequireAuth 里）。
        它不套 AppShell（没有侧栏/状态条/移动底栏），理由见 LandingPage 的注释：
        首屏只该讲清"这是什么"，六个导航入口会立刻分散注意力。
        这也是全站唯一同步引入（非 lazy）的页面 —— 见 pages/index.tsx。

        注意这里不再把 `/` redirect 到 /overview：那正是任务 8 要修的问题 ——
        第一次打开的人直接落在一个装满演示数据的看板上，没有任何地方说过
        "Suvex 是什么"。
      */}
      <Route path="/" element={<LandingPage />} />

      {/*
        认证两页也在 Layout 外面：它们没有侧栏可导航 ——
        未登录时能点的每一个入口都会被守卫弹回来，给一个点不动的侧栏
        比不给更糟。
      */}
      <Route path="/login" element={<LoginRoute />} />
      <Route path="/setup" element={<SetupRoute />} />

      <Route element={<RequireAuth />}>
        <Route element={<Layout />}>
          {NAV_ITEMS.map((item) => {
            const Page = PAGE_COMPONENTS[item.id]
            if (!Page) return null
            return (
              <Route
                key={item.id}
                path={item.path}
                element={
                  <PageTransition>
                    {/*
                      ★ 错误边界放在**页面这一层**（任务 10 P6 补）。

                      它接住的是"某一个页面渲染炸了"，而外壳（侧栏、状态条、
                      导航）**仍然可用** —— 用户可以自己走去别的页面，
                      而不是面对一片白屏。

                      ⚠️ 它必须包在 `Suspense` **外面**：页面组件是 lazy 的，
                      渲染期的异常要能被接住；否则异常会冒到顶层，
                      那时连导航都没了。

                      ⚠️ 它**不套任何 DOM 包装**（`render` 在无错时直接返回
                      `children`）—— 套一层 div 会掐断 `PageFallback` 依赖的
                      `flex min-h-0 flex-1` 高度链，内部滚动区就坏了。
                      `PageTransition` 用 `key={pathname}` 重挂载，
                      所以切页时错误状态会自动重置，不需要额外处理。
                    */}
                    <ErrorBoundary>
                      <Suspense fallback={<PageFallback />}>
                        <Page />
                      </Suspense>
                    </ErrorBoundary>
                  </PageTransition>
                }
              />
            )
          })}

          {/* 未知路径回默认页。不做 404 页面：目前没有会生成坏链接的来源 */}
          <Route path="*" element={<Navigate to={DEFAULT_PATH} replace />} />
        </Route>
      </Route>
    </Routes>
  )
}

function App() {
  /*
    启动时问一次 `/auth/state`（契约 §8 的「启动状态查询」）。

    放在 App 而不是某个页面里：它决定的是**整棵树显示什么**
    （初始化页 / 登录页 / 主界面），任何页面级组件都太晚了。
  */
  useEffect(() => {
    void bootstrapAuth()
  }, [])

  /*
    ★ 401 的统一接入口（契约 §8：「不能只在登录接口处理」）。

    `lib/api.ts` 在 React 树外面，它需要一个不依赖组件的回调来清状态；
    注册在这里，`handleUnauthorized` 把 phase 推回 `login`，
    `RequireAuth` 随即把用户带到登录页 —— 跳转逻辑只有这一条路径。

    卸载时注销：热更新（HMR）会重复执行这个 effect，
    不注销就会累积一串旧闭包，而它们指向的是已经被替换掉的 store。
  */
  useEffect(() => {
    setUnauthorizedHandler(handleUnauthorized)
    return () => setUnauthorizedHandler(null)
  }, [])

  return (
    <BrowserRouter>
      <ScrollToTop />
      <AppRoutes />
      {/*
        ★ toast 宿主挂在**路由之外**：它是全局浮层，与当前在哪个页面无关。
        放进 Layout 的话登录页/初始化页就没有它，
        而那两处也需要报错提示（比如口令不对）。
      */}
      <ToastHost />
    </BrowserRouter>
  )
}

export default App
