import { useMediaQuery } from './useMediaQuery'

const REDUCED_MOTION_QUERY = '(prefers-reduced-motion: reduce)'

/**
 * 读系统的「减少动效」偏好。
 *
 * 为什么不用 Tailwind 的 motion-reduce 媒体查询了事：
 * 全屏 WebGL/Canvas 背景即使把 CSS 动画关掉，requestAnimationFrame 循环照跑，
 * 该烧的电一点没少。这个 hook 让背景**整层不挂载**，而不是挂载了再隐藏。
 */
export function usePrefersReducedMotion(): boolean {
  return useMediaQuery(REDUCED_MOTION_QUERY)
}
