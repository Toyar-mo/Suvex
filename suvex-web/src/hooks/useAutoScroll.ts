import { useCallback, useEffect, useRef, useState } from 'react'

/**
 * 自动滚动 + 暂停 + 回到最新。
 *
 * 这是整页最容易做错的一段逻辑，所以把判断依据写清楚：
 *
 * **核心问题：程序自己滚动也会触发 `scroll` 事件。**
 * 如果监听 scroll 就暂停，每次自动滚动都会把自己暂停掉 —— 典型的自反馈 bug。
 * 解法：滚动前把 `isProgrammaticRef` 置位，滚动后清除；
 * scroll 事件里看到这个标志就忽略。
 *
 * **判定「用户往回翻」的阈值**：不能要求精确贴底（scrollHeight - scrollTop
 * 常因小数舍入差 1px），留 BOTTOM_THRESHOLD_PX 余量；但也不能太大，
 * 否则用户往回翻一点却仍被判为"在底部"。往回翻的判定用另一个更大的阈值，
 * 避免惯性滚动误触发。
 *
 * **暂停的双向性**：
 *   - 用户手动往上滚 → 自动暂停（brief 的硬要求）
 *   - 用户手动滚回底部 → 自动恢复，不用再点按钮
 * 只做单向的话，用户回到最新还得再点一次，很别扭。
 */

/** 距底部多少像素内算「在底部」。留余量吸收小数舍入 */
const BOTTOM_THRESHOLD_PX = 24

/** 用户往回翻多少像素才判定为"在读历史"。太小会被惯性滚动误触发 */
const AWAY_THRESHOLD_PX = 48

export interface UseAutoScrollOptions {
  /** 是否跟随到底部。false 时（暂停）不自动滚 */
  follow: boolean
  /**
   * 内容指纹。变化时若 follow 为 true 就滚到底。
   * 传事件数这类单调递增值即可。
   */
  contentKey: number
  /** 用户离开底部时回调（同步暂停态） */
  onUserAway?: () => void
  /** 用户回到最新时回调 */
  onUserBack?: () => void
}

export interface UseAutoScrollResult {
  /**
   * **挂载方式：稳定的回调 ref**。
   *
   * 为什么不直接 `ref={containerRef}` 或者从外面写 `containerRef.current = node`：
   *
   * ① 一个滚动元素常常需要同时喂给两个 hook（本 hook 判"用户往回翻"，
   *    `useVirtualRows` 算可视区间）。如果各自导出一个 ref 对象、由组件去
   *    写 `.current`，组件就在**改别的 hook 的内部状态** —— 那正是
   *    `react-hooks/immutability` 要拦的"渲染结束后改本地变量"，
   *    而且"哪个 hook 需要 node"这件事被泄漏到了调用方。
   *
   * ② 把写入收进 hook 自己的稳定回调里，调用方只需要 `ref={attachContainer}`，
   *    组合两个 hook 时也就是两行 `a.attachContainer(node); b.attachContainer(node)`。
   *
   * 身份恒定（空依赖 `useCallback`）：身份一变，React 会先用 null 调旧的、
   * 再用新节点调新的，等于让容器卸载又挂载一次 —— 滚动位置归零。
   */
  attachContainer: (node: HTMLDivElement | null) => void
  /** 是否贴在最底部 */
  isAtBottom: boolean
  /** 滚到最新并恢复自动滚动 */
  scrollToLatest: (behavior?: ScrollBehavior) => void
}

export function useAutoScroll({
  follow,
  contentKey,
  onUserAway,
  onUserBack,
}: UseAutoScrollOptions): UseAutoScrollResult {
  const [isAtBottom, setIsAtBottom] = useState(true)

  /*
    ── 容器节点：ref + 一个"已挂载"版本号 ──

    ① 和 `useVirtualRows` 同一个理由：effect 若依赖为空、在挂载时读一次
       `ref.current`，那**容器晚于 effect 出现**时（骨架屏切正文、
       条件渲染换节点）就再也读不到 —— 滚动监听挂不上，表现为"能渲染但
       自动跟随失效、往上翻也不暂停"，没有任何报错。所以需要 `nodeVersion`
       这个状态位来驱动 effect 重跑。

    ② 节点不放进 state：state 按不可变语义对待，而这里要对真实 DOM 做命令式
       操作（读 scrollTop、写 scrollTop）。放进 state 会让"这是可变对象"这件事
       被掩盖，`react-hooks/immutability` 也会（正确地）拦下。

    ③ 不导出 `containerRef`：导出就意味着调用方会去写 `.current`，
       而那是在改本 hook 的内部状态。节点只以 ref 形式留在 hook 内，
       外面只能通过 `attachContainer` 交进来。
  */
  const nodeRef = useRef<HTMLDivElement | null>(null)
  const [nodeVersion, setNodeVersion] = useState(0)

  const attachContainer = useCallback((next: HTMLDivElement | null) => {
    nodeRef.current = next
    setNodeVersion((v) => v + 1)
  }, [])

  // 程序滚动时置位，scroll 事件里据此忽略 —— 防止自反馈
  const isProgrammaticRef = useRef(false)
  // 上一次已知的 scrollTop，用来判断滚动方向
  const lastScrollTopRef = useRef(0)

  // 回调放进 ref：否则调用方传内联函数会导致 scroll 监听反复解绑重绑
  const onUserAwayRef = useRef(onUserAway)
  const onUserBackRef = useRef(onUserBack)
  useEffect(() => {
    onUserAwayRef.current = onUserAway
    onUserBackRef.current = onUserBack
  }, [onUserAway, onUserBack])

  const scrollToLatest = useCallback((behavior: ScrollBehavior = 'smooth') => {
    const container = nodeRef.current
    if (!container) return

    isProgrammaticRef.current = true
    container.scrollTo({ top: container.scrollHeight, behavior })
    setIsAtBottom(true)

    // 平滑滚动是异步的，标志要等它结束才清。
    // scrollend 兼容性还不齐，用定时兜底。
    window.setTimeout(
      () => {
        isProgrammaticRef.current = false
      },
      behavior === 'auto' ? 60 : 500,
    )
  }, [])

  // 监听滚动，判断用户是否离开 / 回到底部
  useEffect(() => {
    const container = nodeRef.current
    if (!container) return

    const onScroll = () => {
      const { scrollTop, scrollHeight, clientHeight } = container
      const distanceFromBottom = scrollHeight - scrollTop - clientHeight
      const atBottom = distanceFromBottom <= BOTTOM_THRESHOLD_PX

      const prevTop = lastScrollTopRef.current
      lastScrollTopRef.current = scrollTop
      const scrollingUp = scrollTop < prevTop

      setIsAtBottom(atBottom)

      // 程序触发的滚动只更新位置显示，不参与暂停判断
      if (isProgrammaticRef.current) return

      if (atBottom) {
        onUserBackRef.current?.()
        return
      }

      // 只有"明确往回翻了足够距离"才算读历史，避免惯性滚动误触发
      if (scrollingUp && distanceFromBottom > AWAY_THRESHOLD_PX) {
        onUserAwayRef.current?.()
      }
    }

    container.addEventListener('scroll', onScroll, { passive: true })
    onScroll() // 处理"挂载时内容已超过一屏"

    return () => container.removeEventListener('scroll', onScroll)
  }, [nodeVersion])

  // 新内容到达时跟随到底部
  useEffect(() => {
    if (!follow) return
    const container = nodeRef.current
    if (!container) return

    /*
      用直接赋值而不是 scrollTo({behavior:'smooth'})：
      高频事件下 smooth 会不断被下一条新滚动打断，表现为滚动条抽搐。
      直接跳到底更稳；因为每次只差几十像素，视觉上也看不出来。
    */
    isProgrammaticRef.current = true
    container.scrollTop = container.scrollHeight
    requestAnimationFrame(() => {
      isProgrammaticRef.current = false
    })
  }, [follow, contentKey, nodeVersion])

  return { attachContainer, isAtBottom, scrollToLatest }
}
