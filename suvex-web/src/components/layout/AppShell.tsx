import { cn } from '@/lib/cn'
import { NAV_ITEMS } from '@/navigation'
import { Suspense, lazy } from 'react'

/**
 * AppShell —— 全站骨架。
 *
 * ★ 任务 8：氛围底改成 `lazy`。这是本轮**唯一一处真正影响首屏字节**的改动。
 *
 *   起因：首页（`/`）是全站入口，而这是个纯 CSR 应用 —— 首屏必须等
 *   主 chunk 下载并执行完才能渲染。构建报告显示主 chunk 里装着
 *   **ogl + Threads**（当时还含一个动画库与一层点阵，两者后因该库的许可证
 *   问题被移除），全部经由下面这个 `AgentField` 静态引入。也就是说：一个
 *   只想看首页文字的人，要先下载一套 WebGL 渲染器。
 *
 *   改成本包之后再构建，主 chunk 从 445 kB 降到 395 kB（gzip 147 → 131 kB）。
 *   背景动效没有被删除，只是不再挡在首屏前面 —— 它本来就是装饰，
 *   晚 50ms 出现没有任何信息损失（design-system §5.1：装饰不能挡住功能）。
 *
 *   `fallback={null}` 而不是骨架：背景是**装饰**，给它做占位骨架等于
 *   用一个真实的视觉元素假装"内容正在来"。什么都不显示才对 ——
 *   首屏本来就只是一层底色，一点也不空。
 */
const AgentField = lazy(() => import('@/components/effects/AgentField'))

/**
 * AppShell —— 全站骨架。
 *
 * 滚动策略（重要，别改）：**让 document 滚动，不要给内容区加内部滚动容器。**
 * 两个原因：
 *   1. 移动端浏览器地址栏的收起/展开依赖文档级滚动；内部滚动容器会让它
 *      永远不收起，白白吃掉约 56px 高度。
 *   2. 全站沿用这个约定，包括未来任何滚动相关的组件（ScrollTrigger、
 *      IntersectionObserver 入场等）默认 scroller 都是 window。
 *      改成内部容器就得一路透传 container，得不偿失。
 *
 *   注：任务 8 之前这里还有第三层依赖 —— ReactBits 的 AnimatedContent
 *   用 ScrollTrigger 且 scroller 固定为 window。那个组件已移除，
 *   但**这条约定继续有效**（第 2 条就是它的一般化形式）。
 * 侧栏和状态条因此用 sticky 而不是 fixed。
 *
 * 层级（从下到上）：
 *   z-0   AgentField  全站氛围底
 *   z-0   内容遮罩     压住氛围层，保证正文对比度
 *   z-20  状态条 sticky
 *   z-30  移动端底栏
 *   z-40  More 面板
 *
 * 移动端底部必须留出底栏高度 + 安全区，否则最后一行内容会被压在底栏下面。
 */

export interface AppShellProps {
  /** 侧栏（由 Layout 注入） */
  sidebar: React.ReactNode
  /** 顶部状态条 */
  statusBar: React.ReactNode
  /** 移动端底栏 */
  mobileNav: React.ReactNode
  /**
   * 页面是否占满视口（内部自己滚动）。
   *
   * 两种模式的区别就在最外层这一个类：
   *   false（默认，document 滚动）→ `min-h-dvh`，内容可以把容器撑高，整页滚
   *   true （内部滚动）           → `h-dvh overflow-hidden`，容器锁死视口高度，
   *                                   高度沿 flex 链往下传，由页面内部的滚动区接管
   *
   * 为什么必须区分：`min-h-dvh` 只是"至少一屏"，内容一多它就跟着长。
   * 满视口页面若用 min-h-dvh，内部滚动区的 clientHeight 会等于 scrollHeight
   * （容器和内容一样高），于是**永远不会出现"不在底部"这个状态** ——
   * 自动跟随、暂停、回到最新这套逻辑会全部失效，且表面上看不出问题。
   * 这个 bug 实际发生过，是靠 `clientHeight === scrollHeight` 这个特征抓到的。
   */
  fillsViewport?: boolean
  children: React.ReactNode
}

export function AppShell({
  sidebar,
  statusBar,
  mobileNav,
  fillsViewport = false,
  children,
}: AppShellProps) {
  return (
    <div
      className={cn(
        'relative flex bg-canvas',
        fillsViewport ? 'h-dvh overflow-hidden' : 'min-h-dvh',
      )}
      data-shell-mode={fillsViewport ? 'viewport' : 'document'}
    >
      {/* 全站氛围底。固定定位，不随内容滚动 —— 它表达的是"这个系统一直在跑"。
          懒加载（见文件头的说明）：背景不该出现在首屏关键路径上。 */}
      <div className="pointer-events-none fixed inset-0 z-0">
        <Suspense fallback={null}>
          <AgentField />
        </Suspense>
      </div>

      {sidebar}

      <div className="relative z-10 flex min-h-0 min-w-0 flex-1 flex-col">
        {statusBar}

        {/*
          内容遮罩：把氛围层压到"能感觉到但读不到"的程度。
          没有它，正文会直接压在流动的线上，对比度随动画起伏 —— 这正是
          design-system §5 说的"背景被单独注意到就是过头了"。
          80% 是实测出来的平衡点：再高就看不见场，再低正文开始抖。
        */}
        <main className="flex min-h-0 flex-1 flex-col bg-canvas/80">
          {/*
            高度用 flex 链传递，而不是让页面自己写 `h-[calc(100dvh-44px-56px)]`。
            硬编码高度要和状态条(44px)、移动端底栏(56px+safe-area) 三处同步，
            改一处就会错位。由这一层统一扣掉底栏高度，
            满视口页面只管写 `flex-1 min-h-0` 即可。

            document 模式下这层刻意**不加** min-h-0 之外的约束，
            高度自然增长，滚动交给 document。
          */}
          <div
            className={cn(
              // +1px：抵消亚像素舍入。底栏是 fixed 定位，几帧之内它的 top
              // 可能比内容底边小 1px，视觉上看不出来但会被自动化检测报成遮挡。
              'flex flex-1 flex-col pb-[calc(3.5rem+env(safe-area-inset-bottom)+1px)] md:pb-0',
              fillsViewport && 'min-h-0',
            )}
          >
            {children}
          </div>
        </main>
      </div>

      {mobileNav}
    </div>
  )
}

/**
 * 页面头。每个页面共用同一个结构：标题 + 一句话说明它回答什么问题。
 *
 * 说明文字来自 navigation.ts 的 blurb —— 和侧栏 tooltip、路由表共用一份，
 * 不在页面里另写一遍（写两遍就会开始漂移）。
 */
export function PageHeader({ navId }: { navId: string }) {
  const item = NAV_ITEMS.find((entry) => entry.id === navId)
  if (!item) return null

  return (
    <header className="border-b border-line px-gutter py-bay">
      <h1 className="text-xl font-medium tracking-[-0.01em] text-ink">{item.label}</h1>
      <p className="mt-tight max-w-[68ch] text-sm text-ink-muted">{item.blurb}</p>
    </header>
  )
}

export default AppShell
