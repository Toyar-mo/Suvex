import Sidebar from './Sidebar'
import { useSidebarCollapsed } from '@/hooks/useSidebarCollapsed'
import TopStatusBar from './TopStatusBar'
import AppShell from './AppShell'
import MobileNav from './MobileNav'
import { CommandPalette } from './CommandPalette'
import { isNavItemActive, NAV_ITEMS } from '@/navigation'
import { ensureOverviewLoaded } from '@/data/overview-store'
import { ensureScansLoaded } from '@/data/scans-store'
import { ensureSettingsLoaded } from '@/data/settings-store'
import { useEffect, useState } from 'react'
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
  const [paletteOpen, setPaletteOpen] = useState(false)

  useEffect(() => {
    ensureScansLoaded()
    ensureSettingsLoaded()
    ensureOverviewLoaded()
  }, [])

  /*
    全局快捷键：Cmd/Ctrl+K 打开命令面板。

    ★ 为什么绑在 window 而不是某个容器上：使用者可能在任意页面、
      任意焦点位置按下它 —— 在输入框里打完字想跳走，是最常见的用法。
      绑在容器上会漏掉那些情况，而"有时灵有时不灵"的快捷键比没有更糟：
      人会以为是自己按错了。

    ★ 为什么**不**在输入框里屏蔽它：Cmd/Ctrl+K 不是字符输入键，
      在浏览器里它本来就被各种站点用作搜索入口，用户的预期是
      "无论我在哪，按它都能搜"。

    ★ macOS 上是 metaKey，其它平台是 ctrlKey —— 两个都收，因为
      `navigator.platform` 判断在 iPad + 外接键盘等场景里并不可靠，
      而"两种修饰键都认"的代价只是 Ctrl+K 在 Mac 上也能用（无害）。
  */
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key.toLowerCase() !== 'k') return
      if (!event.metaKey && !event.ctrlKey) return
      // preventDefault：Chrome 的 Ctrl+K 是"聚焦地址栏"，不拦下来
      // 面板会打开、焦点却跑到地址栏，用户以为没打开。
      event.preventDefault()
      setPaletteOpen((open) => !open)
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
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
      {/*
        面板挂在 Outlet **之后**：它是 portal 到 body 的浮层，
        位置不影响渲染结果；放这里是为了让「Layout 拥有它」这件事
        在代码里一眼看得见（而不是藏在 AppShell 里）。
      */}
      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} />
    </AppShell>
  )
}

export default Layout
