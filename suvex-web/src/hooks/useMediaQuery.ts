import { useCallback, useSyncExternalStore } from 'react'

/**
 * 订阅一个 CSS 媒体查询。
 *
 * 用 useSyncExternalStore 而不是 useState + useEffect：
 * matchMedia 本来就是一个外部数据源，查询结果在任何时刻都能同步读到。
 * 写成 useState 会多一次「先渲染 false、再渲染真实值」的级联渲染，
 * 而断点判断恰恰是这个页面决定要不要挂 WebGL 的依据 —— 不该闪。
 */
export function useMediaQuery(query: string): boolean {
  const subscribe = useCallback(
    (onStoreChange: () => void) => {
      const mediaQuery = window.matchMedia(query)
      mediaQuery.addEventListener('change', onStoreChange)
      return () => mediaQuery.removeEventListener('change', onStoreChange)
    },
    [query],
  )

  const getSnapshot = useCallback(() => window.matchMedia(query).matches, [query])

  // 无 window 时（预渲染）一律按不匹配处理
  const getServerSnapshot = useCallback(() => false, [])

  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot)
}
