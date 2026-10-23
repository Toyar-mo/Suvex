import { lazy } from 'react'

/**
 * 页面导出。
 *
 * ★ 六个页面全部按路由分包（`lazy`）。
 *
 *   分包的理由是**载荷分布不均**，不是"文件大就要拆"：
 *   漏洞详情页带着证据代码块、PoC 与时间线（还有语法高亮的色板），
 *   报告页带着整份报告纸与印刷色板，设置页带着四个表单面板。
 *   而用户第一次打开通常只落在总览。
 *   不拆的话，首屏要为几屏之外的内容付全部代价。
 *
 *   `lazy` 的默认导出契约：工厂函数必须 resolve 到一个**默认导出**的组件。
 *   下面每个页面组件都是 `export default`，改导出方式时要注意别破坏它。
 *
 * ⚠️ 拆包后有一条必须守住的约束：Suspense 的兜底块会**替换**页面内容，
 *    所以它必须透传 AppShell 的高度链，否则满视口页面（智能体活动）
 *    的内部滚动逻辑会静默失效。见 PageFallback 的注释。
 */

export const OverviewPage = lazy(() => import('./OverviewPage'))
export const ActivityPage = lazy(() => import('./ActivityPage'))
export const ScansPage = lazy(() => import('./ScansPage'))
export const FindingsPage = lazy(() => import('./FindingsPage'))
export const ReportsPage = lazy(() => import('./ReportsPage'))
export const KernelPage = lazy(() => import('./Kernel'))
export const IrPage = lazy(() => import('./IrPage'))
export const ToolsPage = lazy(() => import('./ToolsPage'))
export const SettingsPage = lazy(() => import('./SettingsPage'))

/**
 * ★ 首页**不走 lazy**，是全站唯一同步引入的页面。
 *
 *   理由是分包的判据在这里正好反过来：其它页面 lazy 是因为"用户第一次打开
 *   通常只落在总览，不该为几屏之外的内容付代价"。而首页**就是**那个第一次
 *   打开时看到的东西 —— 把它 lazy 了，首屏就要先等一个额外的 chunk 请求
 *   才能开始渲染，等于**为了省首屏的字节而推迟首屏**，因果完全颠倒。
 *
 *   首页自身的体积也刻意压得很小：不含 WebGL（HeroBackdrop 内部再懒加载
 *   Threads/ogl），**也不含 DotGrid**（同样懒加载，而且它已改成零依赖的
 *   自研物理，见 `effects/DotGrid.tsx`），不含任何动画库。
 *   所以它进主包的净增量是几 kB 的 JSX + 图标。
 */
export { LandingPage } from './LandingPage'
