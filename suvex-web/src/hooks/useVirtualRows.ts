import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import {
  DEFAULT_OVERSCAN,
  anchorDelta,
  buildOffsets,
  clamp,
  computeRange,
  type HeightOffsets,
} from '@/data/virtual'

/**
 * 虚拟滚动的 DOM 侧胶水。**所有数学在 `data/virtual.ts`**，这里只管三件事：
 *   1. 听滚动、听尺寸变化，什么时候重算；
 *   2. 用 ResizeObserver 把每行的真实高度量回来；
 *   3. 高度变化时补偿 `scrollTop`，让用户正在看的内容不动。
 *
 * ## 为什么必须量真实高度
 *
 * 事件行有折叠态（约 76px）与展开态（PoC 源码可以到 400px+）。
 * 按常数估算时，展开几行之后所有后续位置都会偏 —— 而且误差累加，
 * 滚到列表后半段偏差可能有几千像素，表现为"滚到一半突然空一大块"。
 *
 * ## 为什么用观察者而不是固定阈值
 *
 * 常见做法是"超过 200 条才虚拟化"。这个阈值在本页没有意义：
 * 一次 45 分钟的扫描有 330 条事件，折叠态下 DOM 节点约 330 × 6 = 2000 个，
 * 已经会让展开/折叠的一次交互掉帧。而虚拟化的代价只有在滚动时才体现，
 * 所以这里**不设阈值**，一直虚拟化。
 */

export interface UseVirtualRowsOptions {
  /** 行数 */
  count: number
  /** 每行的稳定 key。用于把测量结果对齐到"哪一行"而不是"第几个位置" */
  keys: readonly string[]
  /** 未测量行的估算高度 */
  estimatePx?: number
  /** 视口上下各多渲染几条 */
  overscan?: number
}

export interface UseVirtualRowsResult {
  /**
   * 挂载滚动容器。**唯一**的挂载方式。
   *
   * 为什么不再导出 `containerRef`：一个滚动元素常常要同时喂给两个 hook
   * （本 hook 算可视区间，`useAutoScroll` 判"用户往回翻"）。如果各自导出
   * 一个 ref 对象、由组件去写 `.current`，组件就在**改别的 hook 的内部状态**
   * —— 那正是 `react-hooks/immutability` 拦的"改别人的引用"，而且"哪个 hook
   * 需要 node"这件事被泄漏到了调用方。
   *
   * 现在节点只以 state 形式存在于本 hook 内部，外部只能通过这个回调交给它：
   * `ref={attachContainer}`，组合两个 hook 时就是两行调用（见 `EventStream`）。
   *
   * ⚠️ 回调 ref 的身份必须恒定（空依赖 `useCallback`）：身份一变，React 会先
   *    用 null 调旧的、再用新节点调新的，等于让容器卸载又挂载一次 —— 滚动
   *    位置归零、observer 也要重新挂。
   */
  attachContainer: (node: HTMLDivElement | null) => void
  /** 本帧要渲染的区间 `[startIndex, endIndex)` */
  startIndex: number
  endIndex: number
  padTop: number
  padBottom: number
  /** 总高度，用于撑开占位 */
  totalHeight: number
  /**
   * **稳定的**测量回调，交给每一行的 ref。
   *
   * ⚠️ 为什么不是 `measureRef(index)` 这种返回闭包的写法：那样每次渲染都会
   *    产出一个新函数，而 `EventRow` 是 `memo` 包着的 —— 新函数意味着
   *    prop 引用每次变，`memo` 会**静默失效**（"加了 memo 但一帧都没省"）。
   *    行组件自己也有一条注释在警告这件事。
   *
   * 所以这里保持身份恒定，行身份由元素自己的 `data-virtual-key` 带过来。
   * key 是原始字符串，可以安全地进 memo 的比较。
   */
  measureRef: (node: HTMLElement | null) => void
  /** 滚到指定行（用于"跳到某条事件"） */
  scrollToIndex: (index: number, align?: 'start' | 'center') => void
  /** 当前视口第一条可见行 */
  firstVisibleIndex: number
}

export function useVirtualRows({
  count,
  keys,
  estimatePx,
  overscan = DEFAULT_OVERSCAN,
}: UseVirtualRowsOptions): UseVirtualRowsResult {
  /*
    ★ 测量结果按 **key** 存，不是按下标。

    列表是"最新在最后"的追加流，而 `useActivityStream` 有 5000 条上限，
    超出时会从头丢 —— 每丢一条，所有下标都会左移一位。
    按下标存的话，每行会继承前一行的错误高度，整个列表越滚越歪；
    按 key 存则不受位移影响。
  */
  const [measuredByKey, setMeasuredByKey] = useState<ReadonlyMap<string, number>>(() => new Map())

  /*
    高度表按 key 重建。`keys` 由调用方 `useMemo` 出来（见 `EventStream`），
    所以这里的 memo 依赖在内容不变时是稳定的。
  */
  const offsets = useMemo(() => {
    const measuredByIndex = new Map<number, number>()
    for (let i = 0; i < count; i += 1) {
      const h = measuredByKey.get(keys[i])
      if (h !== undefined) measuredByIndex.set(i, h)
    }
    return buildOffsets(count, estimatePx ?? 0, measuredByIndex)
  }, [count, estimatePx, measuredByKey, keys])

  const [viewportHeight, setViewportHeight] = useState(0)
  const [scrollTop, setScrollTop] = useState(0)

  /*
    ── 容器节点：ref + 一个"已挂载"标记 ──

    为什么不把节点放进 state：state 里的值按不可变语义对待，而这里要对外部
    DOM 做命令式操作（读 scrollTop、改 scrollTop、挂监听）。把 DOM 节点塞进
    state 会让"这是可变的真实对象"这件事看不出来，`react-hooks/immutability`
    也会将其拦下 —— 它的判断是对的：那确实不是 React 数据。

    为什么不只用 ref：滚动/尺寸两个 effect 若依赖为空、只在挂载时读一次
    `ref.current`，那**容器晚于 effect 出现**时（骨架屏切正文、条件渲染换节点）
    就再也读不到 —— 监听永远挂不上，表现为"能渲染但滚不动、可视区间永远停在
    第一屏"，而且没有任何报错。所以还需要一个**状态位**让 effect 重新跑。

    分工：`nodeRef` 持有真实节点（在回调 ref 里写，那是"DOM 交给 React 的
    那一刻"）；`nodeVersion` 只在节点**换了或没了**的时候 +1，专门用来驱动
    effect 重跑。effect 里读 ref 拿节点 —— 读 ref 是允许的。
  */
  const nodeRef = useRef<HTMLDivElement | null>(null)
  const [nodeVersion, setNodeVersion] = useState(0)

  const attachContainer = useCallback((next: HTMLDivElement | null) => {
    nodeRef.current = next
    if (next) {
      /*
        挂载瞬间的滚动位置/视口高度在这里同步 —— 不放在 effect body 里。
        effect 里同步 setState 会级联渲染（`react-hooks/set-state-in-effect`），
        而回调 ref 本身就是"外部系统交出节点"的那一个时刻，一个容器只发生一次。
        之后的变化由 scroll 监听与 ResizeObserver 负责。
      */
      setScrollTop(next.scrollTop)
      setViewportHeight(next.clientHeight)
    }
    // 换节点或卸载都要让服听重新挂：+1 而不是布尔，因为同一个容器可能被换掉
    setNodeVersion((v) => v + 1)
  }, [])

  /*
    ── 滚动监听 ──

    ⚠️ 用原生监听 + `{ passive: true }`，不用 React 的 onScroll：
       后者在高频滚动里会走合成事件系统，jsdom 下也更难触发。
    ⚠️ 不节流到 rAF 之外：`setScrollTop` 是同值跳过渲染的（React 保证），
       而滚动本身已经是按帧节流的。
  */
  useEffect(() => {
    const el = nodeRef.current
    if (!el) return

    const onScroll = () => setScrollTop(el.scrollTop)
    el.addEventListener('scroll', onScroll, { passive: true })
    return () => el.removeEventListener('scroll', onScroll)
  }, [nodeVersion])

  /* 视口尺寸变化：窗口缩放、侧栏折叠、软键盘弹出都会变 */
  useEffect(() => {
    const el = nodeRef.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const h = entry.contentRect.height
        if (h > 0) setViewportHeight(h)
      }
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [nodeVersion])

  /*
    ── 逐行测量 ──

    每个行组件把自己交给这里，进出时更新高度表。

    用一整个 observer 观察所有行（而不是每行一个），因为每行一个的话，
    滚动时每帧都在 disconnect/connect 几十个 observer，开销比测量本身还大。

    ★ 这个 effect 的依赖必须是空数组 —— 曾经写成 `[measuredByKey]`，
      那是**错的**：每次量到新高度就 disconnect 掉旧 observer、建一个新的，
      而新 observer 一个节点都没 observe（observation 只在节点挂载时建立）。
      结果是"第一分量完之后，已有的行再也不被观察" —— 用户展开一行，
      高度变了却量不到，后续所有位置全是旧的，表现为展开后滚动条错乱。
  */
  const observerRef = useRef<ResizeObserver | null>(null)
  const nodeToKeyRef = useRef<WeakMap<Element, string>>(new WeakMap())

  useEffect(() => {
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver((entries) => {
      /*
        ★ 用函数式更新读"当前"高度表。

        以前是 `const measuredRef = useRef(...)；measuredRef.current = measuredByKey`
        —— 在渲染期间写 ref。规则不允许（渲染必须可被打断/丢弃，写 ref 是不可
        回滚的副作用），而且这里根本不需要它：`setState(fn)` 拿到的就是最新值。
      */
      setMeasuredByKey((prev) => {
        let next: Map<string, number> | null = null
        for (const entry of entries) {
          const key = nodeToKeyRef.current.get(entry.target)
          if (key === undefined) continue
          const h = entry.contentRect.height
          // 0 高度不采信：刚挂载时 ResizeObserver 可能先报一次 0，
          // 若采信它，那几行会被算成零高度，后续位置整体上移
          if (!(h > 0)) continue
          if (prev.get(key) === h) continue
          next ??= new Map(prev)
          next.set(key, h)
        }
        // 没变化就返回原对象，React 会跳过这次渲染
        return next ?? prev
      })
    })
    observerRef.current = ro
    return () => {
      ro.disconnect()
      observerRef.current = null
    }
  }, [])

  /*
    ★ 稳定的测量回调：身份恒定，行身份由元素自己的 `data-virtual-key` 带过来。

    为什么不 `measureRef(index)` 返回闭包 —— 那样每次渲染都是新函数，
    `EventRow` 的 `memo` 会**静默失效**（看起来加了 memo，其实一帧没省）。

    为什么用 key 而不是下标：元素上的 `data-*` 是渲染那一刻的静态快照，
    而闭包/ref 里读到的下标可能已经过期（列表头部丢事件会整体左移）。
    key 是这条事件自己的标识，永远不会指错行。
  */
  const measureRef = useCallback((node: HTMLElement | null) => {
    if (!node) return
    const key = node.dataset.virtualKey
    if (key === undefined || key === '') return

    nodeToKeyRef.current.set(node, key)
    observerRef.current?.observe(node)
  }, [])

  /*
    ── 高度变化后补偿 scrollTop ──

    这件事必须在 `useLayoutEffect` 里做：在浏览器绘制**之前**把 scrollTop
    改掉，用户就一次都看不到跳动。放到 useEffect 里会先绘制一帧旧位置，
    表现为一闪。

    锚点取**当前视口第一条可见行**。选第 0 行是错的 —— 它自己就在变。
  */
  const prevOffsetsRef = useRef<HeightOffsets>(offsets)
  useLayoutEffect(() => {
    const el = nodeRef.current
    const before = prevOffsetsRef.current
    prevOffsetsRef.current = offsets
    if (!el) return
    if (before.offsets.length !== offsets.offsets.length) return // 行数变了，不是高度变化
    if (before.totalHeight === offsets.totalHeight) return

    const { startIndex } = computeRange(before, el.scrollTop, el.clientHeight, 0)
    const delta = anchorDelta(before, offsets, startIndex)
    if (delta === 0) return
    el.scrollTop = el.scrollTop + delta
    setScrollTop(el.scrollTop)
  }, [nodeVersion, offsets])

  /*
    `scrollToIndex` 要读"最新高度表"，但它必须身份稳定（会进 effect 依赖）。
    → 用 ref 传表，但**在 effect 里写**，不在渲染期间写。
  */
  const offsetsRef = useRef<HeightOffsets>(offsets)
  useLayoutEffect(() => {
    offsetsRef.current = offsets
  }, [offsets])

  const range = computeRange(offsets, scrollTop, viewportHeight, overscan)

  const scrollToIndex = useCallback(
    (index: number, align: 'start' | 'center' = 'start') => {
      const el = nodeRef.current
      if (!el) return
      const table = offsetsRef.current
      const total = table.offsets.length - 1
      if (total <= 0) return
      const i = clamp(Math.floor(index), 0, total - 1)
      const top = table.offsets[i]
      const height = table.offsets[i + 1] - top
      const target =
        align === 'center' ? top - Math.max(0, (el.clientHeight - height) / 2) : top
      el.scrollTo({ top: Math.max(0, target), behavior: 'smooth' })
    },
    [],
  )

  const firstVisibleIndex = computeRange(offsets, scrollTop, viewportHeight, 0).startIndex

  return {
    attachContainer,
    startIndex: range.startIndex,
    endIndex: range.endIndex,
    padTop: range.padTop,
    padBottom: range.padBottom,
    totalHeight: offsets.totalHeight,
    measureRef,
    scrollToIndex,
    firstVisibleIndex,
  }
}
