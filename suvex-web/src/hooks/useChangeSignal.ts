import { useState } from 'react'

/**
 * 「这个值变了没有」—— 用于让状态切换的反馈**只在变化时**播放。
 *
 * 返回 `[revision, changed]`：
 *   · `revision` 放进 `key` —— 变化时 React 重新挂载元素，CSS 动画随之重播；
 *   · `changed`  首次渲染为 false，之后恒为 true —— 用来决定**要不要挂动画类**。
 *
 * ★ 为什么不能只用 `key={value}`：
 *
 *   那样每次**挂载**都会播一次动画。于是一进列表页，13 行徽标会同时缩放
 *   落定一遍 —— 正是 design-system §5 禁止的"给每个区块配相同的入场动画"
 *   （最明显的模板感来源）。动画应该只回答"刚刚改了没有"，而不是
 *   "这里有个元素"。
 *
 * ★ 实测数据（任务 8 补测，`/findings`，数「带 animate-settle-in 的徽标数」）：
 *
 *   | 场景                     | 实测 | 说明                                   |
 *   | ------------------------ | ---- | -------------------------------------- |
 *   | 冷加载进列表             | **0** | 13 个徽标全部 animationName: none ✓    |
 *   | 打开抽屉（未流转）       | **0** | 14 个徽标（13 列表 + 1 抽屉）✓          |
 *   | 流转一条                 | **2** | 列表行 + 抽屉标题栏，各一个实例        |
 *   | 筛选 → 清筛选（重渲染）  | 仍 2 | 其余 11 行干净，**类名不扩散** ✓        |
 *
 *   第 3 行的 2 不是 bug：被流转的那条漏洞**同时渲染在两个地方**，
 *   两个 `StatusBadge` 各持有自己的 `useChangeSignal`，各播一次。
 *   （这类"同一条数据多处渲染"本身是跨页一致性的表现，见 §7。）
 *
 * ★ 为什么用 `useState` 而不是 `useRef` 记上一个值：
 *
 *   渲染期读/写 ref 在 React 19 下是明确的反模式（并发渲染下会被丢弃或重放，
 *   `react-hooks/purity` 也会拦）。下面用的"渲染期直接调 setState 来修正状态"
 *   是 React 官方文档给的模式（*adjusting state when props change*）：
 *   它不产生级联提交 —— React 会立刻用新值重跑这次渲染，用户看不到中间态。
 *
 * 纯派生、无副作用，所以可以安全地在任何渲染路径里调用。
 */
export function useChangeSignal<T>(value: T): [number, boolean] {
  const [previous, setPrevious] = useState(value)
  const [revision, setRevision] = useState(0)

  if (previous !== value) {
    setPrevious(value)
    setRevision((r) => r + 1)
  }

  return [revision, revision > 0]
}
