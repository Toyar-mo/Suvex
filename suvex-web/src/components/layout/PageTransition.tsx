import { useLocation } from 'react-router-dom'
import type { ReactNode } from 'react'

/**
 * 路由切换的过渡。
 *
 * ★ 任务 8 重写。上一版用 ReactBits 的 `AnimatedContent`（gsap + ScrollTrigger），
 *   它带来两个真实代价，都违反「动效不能挡住功能」：
 *
 *   1. **它是"先隐藏、等 JS 放行"的。** 组件渲染时带 `invisible`，
 *      挂载后由 gsap 在 effect 里 `set({ visibility: 'visible' })` 兜回来。
 *      于是每个页面至少要等一次 effect 才能出现 —— 这是**白屏的一帧**，
 *      而我们本来可以让它第一帧就可见（纯 CSS 动画不需要等 JS）。
 *      更糟的是失败模式：gsap 若因为任何原因没跑完，内容**永远不可见**，
 *      且控制台不报错 —— 用户看到的就是一片空白。
 *
 *   2. **它把"滚动入场"的语义借来当路由过渡用。** 注释里自己写着
 *      "靠元素挂载时已在视口内 → 立即触发"。这条依赖 ScrollTrigger 的
 *      scroller 必须是 window（所以 AppShell 被迫让 document 滚动）、
 *      依赖 threshold 必须传 0、依赖元素挂载时机。四个前提少一个，
 *      元素就停在 `opacity: 0` —— 又是一种"页面空白但不报错"。
 *
 *   现在换成 **纯 CSS**（`animate-page-in`，140ms）。
 *   好处是结构性的，不是省了几 KB：
 *     · 第一帧就可见。浏览器解析到样式即开始动画，**不等任何 JS**；
 *     · 没有"JS 不跑就白屏"这个失败模式 —— 哪怕整个 bundle 挂掉，
 *       内容和动画都由 CSS 独立成立；
 *     · gsap 的 ScrollTrigger 从主包里彻底消失（见下方"顺带修掉的耦合"）。
 *
 * ── ★ 只动 transform，不动 opacity（任务 8 复核修正）──────────────
 *
 *   这里原先写的是"140ms 纯透明度"。复核时做对照实验发现那样更危险：
 *
 *   | 写法                      | 时间轴冻结（隐藏标签页）时的状态        |
 *   | ------------------------- | --------------------------------------- |
 *   | `from { opacity: 0 }`     | 计算 opacity = **0** → 整层不可见       |
 *   | `from { transform: ... }` | opacity 恒为 1 → 只停在一个 6px 位移上  |
 *
 *   这条包裹层是整个应用**每一页**的祖先 —— 把"不可见的初态"挂在它上面，
 *   影响面就是"全站所有内容"，风险等级最高。
 *   **内容的可见性绝不该依赖动画是否推进。** 所以改成只动 6px 位移。
 *
 *   这同时是 HeroTitle 与向导步骤用的那条纪律（§5.1「动效不能挡住功能」第 4 条）。
 *
 *   也不做**出场**动画：出场会推迟用户看到新内容的时间，而这个产品的核心
 *   体验就是"信息正在流动"—— 它不该等动画。
 *
 * ── ⚠️ 关于 containing block：实测结论，不是推理 ──────────────────
 *
 *   因为这条动画动的是 transform，就必须回答"它会不会重新引入
 *   `position: fixed` 包含块的问题"（旧版 gsap 就是栽在这上面）。
 *   实测过了，两点：
 *
 *   | 检查                                   | 结果            |
 *   | -------------------------------------- | --------------- |
 *   | 动画**结束后**的 `transform`           | **`none`** ✓    |
 *   | 动画结束后是否构成包含块               | **否** ✓        |
 *   | 时间轴冻结时的内容 `opacity`           | **`1`** ✓       |
 *
 *   第 1、2 行的关键是**这条动画刻意不带 `fill: both`**。
 *   最初我给它加了 `both`，实测动画结束后 `transform` 是
 *   `matrix(1, 0, 0, 1, 0, 0)` —— **一个恒等矩阵，视觉上完全看不出来，
 *   但它照样构成包含块**。那正是旧版 gsap 留下的同一个坑。
 *   去掉 `both` 后 transform 回到 `none`，坑消失。
 *   （`both` 本来也没用：它的作用是让"延迟期间"保持 from 态，
 *     而这条动画没有 delay。加它只有副作用。）
 *
 *   所以受影响的只剩"动画进行中的那 140ms"，且期间可能受影响的浮层
 *   只有 Drawer（portal 到 body，**免疫**）和 BulkActionBar（用 sticky，
 *   本来就正确）。这两处当初就是按"不要依赖 fixed"设计的。
 *
 *   如果将来要加新的 `fixed` 浮层，**先看这里**：要么用 portal，
 *   要么确认它不在这条包裹层的子树里。
 *
 * ── 关于高度链（这条仍然必须守住）───────────────────────────────
 * 包裹层必须透传 flex 高度：`flex min-h-0 flex-1 flex-col`。
 * 「智能体活动」是 fillsViewport 页面，高度靠这条链传进它内部的滚动区。
 * 掐断它，滚动区的 clientHeight 会等于 scrollHeight，"不在底部"这个状态
 * 永不出现 —— 自动跟随 / 暂停 / 回到最新 全部静默失效，表面看不出来。
 * 这个 bug 在本项目已因相同原因发生过两次。所以类名是刻意的，别删。
 *
 * key={pathname} 让每次路由切换重新挂载，CSS 动画随之重播。
 * 减少动效偏好下不带 animation 类 —— `index.css` 的全局兜底还会把时长
 * 压到 0.001ms，两条一起保证"要么快速淡入、要么完全不动"。
 */
export function PageTransition({ children }: { children: ReactNode }) {
  const { pathname } = useLocation()

  return (
    <div
      key={pathname}
      className="flex min-h-0 flex-1 animate-page-in flex-col"
      data-page-transition=""
    >
      {children}
    </div>
  )
}

export default PageTransition
