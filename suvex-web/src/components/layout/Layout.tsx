import Sidebar from './Sidebar'
import { useSidebarCollapsed } from '@/hooks/useSidebarCollapsed'
import TopStatusBar from './TopStatusBar'
import AppShell from './AppShell'
import MobileNav from './MobileNav'
import { isNavItemActive, NAV_ITEMS } from '@/navigation'
import { ensureOverviewLoaded } from '@/data/overview-store'
import { ensureScansLoaded } from '@/data/scans-store'
import { ensureSettingsLoaded } from '@/data/settings-store'
import { useEffect } from 'react'
import { Outlet, useLocation } from 'react-router-dom'

/**
 * 布局路由。作为 <Route element={<Layout />}> 的父级，用 <Outlet /> 渲染子页面。
 * 这样布局只挂载一次，页面切换不会重建侧栏和状态条。
 *
 * 高度模式由当前路由**自己声明**（navigation.ts 的 fillsViewport）：
 *   - 普通页面：document 滚动，高度自然增长；
 *   - 满视口页面（智能体活动）：锁死视口高度，内部自己滚动。
 * 两种模式的差异只在最外层容器的一个类，见 AppShell。
 *
 * ## 为什么"全局数据"的首次加载放在这里
 *
 * 侧栏的"N 个运行中"与状态条的"模型 XXX"要读 `scans-store` 与
 * `settings-store`，状态条的"token 用量"要读 `overview-store`，
 * 而**不是每个页面都会拉它们** ——
 * 总览页读 `/overview` 一个端点，根本不经过前两个 store；
 * 反过来，停在其它页面时没有任何人会去拉 `/overview`。
 * 于是直接落在总览时，侧栏会显示"0 运行中"、状态条会显示"未配置"，
 * 而系统里可能真有几个扫描在跑、provider 也早就配好了。
 * 两句都是**说反了的话**，比不显示更糟 —— 前者让人以为系统闲置，
 * 后者会让人跑去「设置」重配一遍。用量那一栏同理：它恒显"暂无"。
 *
 * ⚠️ 放在 `Layout` 而不是 `TopStatusBar` 里，是因为加载必须发生在
 *    **登录之后**：未登录时 `GET /providers` 会 401，而 `lib/api.ts`
 *    的 401 处理会把人踢到登录页 —— 触发点若在状态条，
 *    公开的落地页（不走 Layout）就会变得不可访问。
 *
 * 三个 `ensure*` 都是**幂等**的：已经发起过就什么都不做，
 * 所以各页面自己的 effect 照常调用，不会变成重复请求。
 */
export function Layout() {
  const { collapsed, toggle } = useSidebarCollapsed()
  const { pathname } = useLocation()

  useEffect(() => {
    ensureScansLoaded()
    ensureSettingsLoaded()
    ensureOverviewLoaded()
  }, [])

  // 用导航项自己的声明判断，而不是在这里列一份"特殊路由"名单
  const fillsViewport = NAV_ITEMS.some(
    (item) => item.fillsViewport && isNavItemActive(item, pathname),
  )

  return (
    <AppShell
      fillsViewport={fillsViewport}
      sidebar={
        // 平板/手机（<md）不显示侧栏，改由底部 tab 承担导航
        <Sidebar
          collapsed={collapsed}
          onToggleCollapsed={toggle}
          className="sticky top-0 hidden max-h-dvh md:flex"
        />
      }
      statusBar={
        <div className="sticky top-0 z-20">
          <TopStatusBar />
        </div>
      }
      mobileNav={<MobileNav />}
    >
      <Outlet />
    </AppShell>
  )
}

export default Layout
