import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

// 自托管可变字体，先于全局样式加载，避免首帧回退到系统字体
import '@fontsource-variable/geist'
import '@fontsource-variable/jetbrains-mono'
// 中文回退字体。包体 4.4MB，但按 unicode-range 分成 101 个分片，
// 浏览器只下载页面实际用到的那几片（中位数 47KB）。
import '@fontsource-variable/noto-sans-sc'

import App from './App'
import ErrorBoundary from './components/ui/ErrorBoundary'
import { initTheme } from './data/theme-store'
import './index.css'

/*
  ★ 主题在**挂载之前**定好（亮/暗主题）。

  `index.html` 里那段内联脚本已经设过一次 `data-theme` —— 它管的是
  「bundle 还没下载完时首帧不要闪」。这里再跑一次 `initTheme()` 不是重复劳动，
  两者管的是不同的事：

    · 内联脚本跑在 bundle **之前**，它只能读存储 + matchMedia，
      立不了监听（那时立了也没人管）；
    · `initTheme()` 负责把 store 的快照对齐到真实偏好，并**建立系统换色监听**，
      让「跟随系统」在系统换色时真的跟着变。

  ⚠️ 放在 `createRoot` 前面，而不是某个组件的 `useEffect` 里：首次渲染时
    右上角的图标就是按真实偏好画的，不会先画成缺省值再跳一下。它幂等，
    StrictMode / HMR 重复执行无副作用。

  ⚠️ 也刻意不放在 `App.tsx`：`App` 的 effect 晚于首次渲染，而且那里的
    两个 effect 都是"启动后干点什么"的语义，主题属于"启动前就对"的那一类。
*/
initTheme()

/*
  ★ 顶层错误边界（任务 10 P6 补）。

  与 `App.tsx` 里页面那一层是**两层**，各挡一类：

  · 顶层（这里）挡的是**外壳自己**炸掉 —— 路由、Layout、ToastHost、
    以及认证守卫。没有它，那些地方出错的后果是彻底白屏。
  · 页面层（`App.tsx`）挡的是**某一个页面**炸掉，那时导航还在，
    用户能自己走去别的页面。

  只有页面层是不够的：崩溃若发生在外壳里，整棵树一起没。
  只有顶层也不够：一个页面的错误会顶掉整个外壳，用户连换页都做不到。

  ⚠️ 它**必须在 `StrictMode` 里面**，否则它自己不受严格模式的检查；
  而且要在 `App` 外面，才能接住 `App` 内部的一切。
*/
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </StrictMode>,
)
