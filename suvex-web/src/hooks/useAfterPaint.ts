import { useEffect, useState } from 'react'

/**
 * 「首帧画完之后」的信号。
 *
 * 用途：把**昂贵的装饰层**（WebGL / Canvas 背景）推迟到首屏内容画完之后再挂载。
 *
 * ★ 为什么是双重 rAF，而不是 setTimeout(x) 或单个 rAF：
 *
 *   · `setTimeout` 的毫秒数是在赌 —— 慢设备上 200ms 可能还没画完，
 *     快设备上又白白等了一段。它测量的是时间，不是"画完了没有"。
 *   · **单个 rAF** 的回调在**下一帧渲染之前**执行。在里面 setState 会触发
 *     一次新的渲染，而这次渲染和首帧处在同一帧的渲染流程里 ——
 *     于是 WebGL 初始化会挤进首帧，正是我们想避开的。
 *   · **双重 rAF**：外层 rAF 排在首帧渲染前，它注册的内层 rAF 排在
 *     **首帧渲染之后**的下一个帧前。此刻首帧已经真实落屏，
 *     所以在这里 setState 不会推迟任何已画出的内容。
 *
 *   这是"等到画完"这件事在浏览器里能做到的最准确表达。
 *
 * ─────────────────────────────────────────────────────────────
 * ★ 为什么还需要一个兜底 timeout（这条是**实测发现的**，不是预防性设计）
 *
 *   原版实现只靠双重 rAF。用无头/后台标签页实测时发现：
 *   **`document.visibilityState === 'hidden'` 时浏览器完全不给 rAF 派发帧**
 *   （实测 `rAF` 回调在 1.5 秒内一次都没触发）。
 *
 *   后果不是"晚一点出现"，而是**永远不出现** —— 装饰层彻底缺失，
 *   而控制台一条错误都没有。对首页来说这是真实损失：Hero 背景正是
 *   "第一印象"的载体，也是这个页面的辨识度来源。
 *
 *   所以这里补一条 timeout 兜底，并且这个取舍是明确的：
 *     · **可见的页面**：rAF 在 1–2 帧内（约 32ms）就触发，兜底永不生效，
 *       首屏性能完全按上面的分析走，没有变差。
 *     · **不可见的页面**：兜底在 1 秒后挂载。此时根本没人看，
 *       而 Threads 自身有 `document.hidden` 门控，「不可见时直接 return 不渲染」，
 *       所以这个挂载的代价只是创建了一个 GL 上下文，没有每帧开销。
 *     · **最坏情况**等于改动之前的行为（原实现是挂载后延时淡入），
 *       最好情况严格更好。所以这是一笔没有下行风险、只有上行的改动。
 *
 *   教训值得单独记一条：**"等条件成立"型的动效门控，必须回答
 *   "条件要是永远不成立会怎样"。** 只写成功路径的门控 = 一个静默的
 *   功能缺失 bug。这和 design-system §12 里那条"平滑滚动会静默 no-op"
 *   是同一类问题：不报错、不动，用户只看到少了一部分东西。
 */

/**
 * 兜底时延。取 1000ms：在 rAF 正常的环境里它永远不生效（rAF 约 32ms 就到），
 * 所以这个值只影响"rAF 被挂起"这一种情况 —— 那种情况下页面不可见，
 * 早一点晚一点没有观感差异。
 */
const FALLBACK_MS = 1000

export function useAfterPaint(): boolean {
  const [afterPaint, setAfterPaint] = useState(false)

  useEffect(() => {
    let outer = 0
    let inner = 0
    const mark = () => setAfterPaint(true)

    // ① 精确信号：双重 rAF（见文件头）
    outer = requestAnimationFrame(() => {
      inner = requestAnimationFrame(mark)
    })

    // ② 兜底：rAF 被挂起时也必须挂载，否则装饰层会永远缺失
    const timer = window.setTimeout(mark, FALLBACK_MS)

    return () => {
      cancelAnimationFrame(outer)
      cancelAnimationFrame(inner)
      window.clearTimeout(timer)
    }
  }, [])

  return afterPaint
}
