/**
 * ReactBits · CountUp  (ts-tailwind 变体)
 * 出处: https://reactbits.dev/text-animations/count-up
 * 源文件: https://github.com/DavidHDev/react-bits/blob/main/src/ts-tailwind/TextAnimations/CountUp/CountUp.tsx
 * 取用日期: 2026-02-26 · 许可证见 ../effects/README.md
 *
 * 本地改动：
 *   1. 新增 `animateOnChange`（默认 true）。上游用 useInView({once:true})，
 *      意味着**只在首次进入视口时滚一次**；而我们的指标卡是轮询更新的，
 *      数字变化时不会重播，用户看到的是硬跳变 —— brief 明确要求
 *      「数字变化时用数字滚动动效，不要用突兀的跳变」。
 *      这里额外依赖 `to`：变化时把 motionValue 从**当前值**推到新值。
 *      （不是从 0 重来 —— 从 0 重滚会让"用得多"这件事看起来像重置，
 *       而且 3→4 这种小变化滚一遍比直接跳更让人困惑。）
 *   2. 补了 `aria-hidden` 之外的可访问性：动画期间数字在变，读屏器若跟着念
 *      会读出几十个中间值。真正的语义由外层 StatCard 的静态文本承载，
 *      这里统一标 aria-hidden（见 StatCard 的实现说明）。
 *
 * 依赖：motion（framer-motion 的新包名，`motion/react` 入口）。
 */
import { useInView, useMotionValue, useSpring } from 'motion/react'
import { useCallback, useEffect, useRef } from 'react'

interface CountUpProps {
  to: number
  from?: number
  direction?: 'up' | 'down'
  delay?: number
  duration?: number
  className?: string
  startWhen?: boolean
  separator?: string
  /** 值变化时是否重新滚动（本项目默认开） */
  animateOnChange?: boolean
  onStart?: () => void
  onEnd?: () => void
}

export default function CountUp({
  to,
  from = 0,
  direction = 'up',
  delay = 0,
  duration = 2,
  className = '',
  startWhen = true,
  separator = '',
  animateOnChange = true,
  onStart,
  onEnd,
}: CountUpProps) {
  const ref = useRef<HTMLSpanElement>(null)
  const motionValue = useMotionValue(direction === 'down' ? to : from)

  const damping = 20 + 40 * (1 / duration)
  const stiffness = 100 * (1 / duration)

  const springValue = useSpring(motionValue, {
    damping,
    stiffness,
  })

  const isInView = useInView(ref, { once: true, margin: '0px' })

  const getDecimalPlaces = (num: number): number => {
    const str = num.toString()
    if (str.includes('.')) {
      const decimals = str.split('.')[1]
      if (parseInt(decimals) !== 0) {
        return decimals.length
      }
    }
    return 0
  }

  const maxDecimals = Math.max(getDecimalPlaces(from), getDecimalPlaces(to))

  const formatValue = useCallback(
    (latest: number) => {
      const hasDecimals = maxDecimals > 0

      const options: Intl.NumberFormatOptions = {
        useGrouping: !!separator,
        minimumFractionDigits: hasDecimals ? maxDecimals : 0,
        maximumFractionDigits: hasDecimals ? maxDecimals : 0,
      }

      const formattedNumber = Intl.NumberFormat('en-US', options).format(latest)

      return separator ? formattedNumber.replace(/,/g, separator) : formattedNumber
    },
    [maxDecimals, separator],
  )

  useEffect(() => {
    if (ref.current) {
      ref.current.textContent = formatValue(direction === 'down' ? to : from)
    }
  }, [from, to, direction, formatValue])

  useEffect(() => {
    if (isInView && startWhen) {
      if (typeof onStart === 'function') {
        onStart()
      }

      const timeoutId = setTimeout(() => {
        motionValue.set(direction === 'down' ? from : to)
      }, delay * 1000)

      const durationTimeoutId = setTimeout(
        () => {
          if (typeof onEnd === 'function') {
            onEnd()
          }
        },
        delay * 1000 + duration * 1000,
      )

      return () => {
        clearTimeout(timeoutId)
        clearTimeout(durationTimeoutId)
      }
    }
  }, [isInView, startWhen, motionValue, direction, from, to, delay, onStart, onEnd, duration])

  // 本地新增：值变化时从**当前显示值**继续滚到新值
  useEffect(() => {
    if (!animateOnChange || !isInView) return
    const target = direction === 'down' ? from : to
    if (motionValue.get() === target) return
    motionValue.set(target)
  }, [to, from, direction, animateOnChange, isInView, motionValue])

  useEffect(() => {
    const unsubscribe = springValue.on('change', (latest: number) => {
      if (ref.current) {
        ref.current.textContent = formatValue(latest)
      }
    })

    return () => unsubscribe()
  }, [springValue, formatValue])

  return <span className={className} ref={ref} aria-hidden="true" />
}
