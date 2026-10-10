import { useEffect, useRef } from 'react'

/**
 * 让"就地展开"的表单在展开后自动进入视野。
 *
 * ★ 解决的是一个**实测出来**的问题（不是猜的）：
 *
 *   设置页的几个编辑器都是**行内展开**的（点「编辑」，表单在该行下方展开）。
 *   实测：视口高 1018px 时，展开后整行的底边落在 1170px —— 溢出 152px。
 *   保存按钮正好在折叠线以下，用户填完字段看不到它，还得自己往下滚。
 *   对一个"保存状态必须可信"的页面来说，主操作够不着是很糟的开场。
 *
 * ★ 为什么只滚"超出量"，而不是 scrollIntoView：
 *
 *   `block: 'end'` 每次展开都把元素底边对齐到视口底部 —— 即使它**本来就完整可见**，
 *   页面也会莫名其妙地跳一下。`block: 'nearest'` 则相反：元素比视口高时它对齐顶边，
 *   保存按钮反而被推出屏幕。两个都不合适。
 *   所以这里只算一件事：**底边超出视口多少就滚多少**（外加一点余量），没超出就一动不动。
 *
 * ★★ 为什么用 behavior: 'auto' 而不是 'smooth'（这里踩过坑）★★
 *
 *   第一版写的是 `reducedMotion ? 'auto' : 'smooth'`。实测发现**完全没滚动**，
 *   埋点看到 effect 跑了、overflow 也算对了 152，但 scrollY 始终是 0。
 *   隔离验证：
 *       window.scrollBy({ top: 176, behavior: 'smooth' }) → scrollY 0（没动）
 *       window.scrollBy({ top: 176, behavior: 'auto' })   → scrollY 176（正常）
 *
 *   也就是说**平滑滚动会在某些环境里静默 no-op**（无头浏览器、后台标签页、
 *   部分 webview —— 只要合成器没有帧在跑，平滑动画就不会推进）。
 *   而它失默的方式最坏：不报错、不滚动，用户只是"保存按钮还是看不见"。
 *
 *   结论：**把内容带进视野是功能性动作，不是装饰**。
 *   功能性动作不能依赖动画能不能播出来。这与 design-system §5
 *   「动效服务于信息，不服务于装饰」是同一条原则的延伸 ——
 *   当动画可能挡住功能时，去掉动画。
 *
 *   代价是失去那一点平滑感。值得：可预期 > 好看。
 */
export function useRevealOnExpand<T extends HTMLElement>(expanded: boolean) {
  const ref = useRef<T>(null)

  useEffect(() => {
    if (!expanded) return

    const el = ref.current
    if (!el) return

    /*
      在 effect 里量、在 effect 里滚 —— 而不是在点击处理里。

      点击处理执行时，展开的内容还没渲染，getBoundingClientRect() 拿到的是
      展开**之前**的高度，算出的溢出量是错的（少算一整块表单）。
      effect 在 DOM 提交之后跑，量到的才是真实的展开后布局。
    */
    const overflow = el.getBoundingClientRect().bottom - window.innerHeight

    // 加 24px 余量，让底部不贴着屏幕边缘
    if (overflow > 0) {
      window.scrollBy({ top: overflow + 24, behavior: 'auto' })
    }
  }, [expanded])

  return ref
}

export default useRevealOnExpand
