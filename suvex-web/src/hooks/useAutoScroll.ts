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
  /** 挂到滚动容器上 */
  containerRef: React.RefObject<HTMLDivElement | null>
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
  const containerRef = useRef<HTMLDivElement | null>(null)
  const [isAtBottom, setIsAtBottom] = useState(true)

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
    const container = containerRef.current
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
    const container = containerRef.current
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
  }, [])

  // 新内容到达时跟随到底部
  useEffect(() => {
    if (!follow) return
    const container = containerRef.current
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
  }, [follow, contentKey])

  return { containerRef, isAtBottom, scrollToLatest }
}
