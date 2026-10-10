import { useCallback, useState } from 'react'

/**
 * 把一份状态存进 localStorage，刷新后恢复。
 *
 * 三个必须处理的点（朴素写法都会漏）：
 *   1. 首次渲染不能直接读 localStorage —— 预渲染环境没有 window。
 *   2. 读取要 try/catch：用户可能禁用了存储，或值是上一版留下的坏 JSON。
 *   3. 写入失败（配额满、隐私模式）不能让界面崩掉，所以吞掉异常。
 *
 * initial 支持传函数：这样「默认值取决于屏幕宽度」这类逻辑只在首次执行一次。
 *
 * ⚠️ 刻意不做「key 变化时重新读取」：那需要 effect + 依赖 initial，
 *    而调用方传内联箭头函数时 initial 每次渲染都是新引用，会变成无限循环。
 *    本 hook 的 key 在生命周期内必须保持不变；真要换 key 就换组件（用 key={}）。
 */
export function useLocalStorage<T>(key: string, initial: T | (() => T)) {
  const [value, setValue] = useState<T>(() => {
    const fallback = typeof initial === 'function' ? (initial as () => T)() : initial
    if (typeof window === 'undefined') return fallback
    try {
      const raw = window.localStorage.getItem(key)
      return raw === null ? fallback : (JSON.parse(raw) as T)
    } catch {
      // 存储不可用或 JSON 损坏 —— 用默认值继续，不要白屏
      return fallback
    }
  })

  const set = useCallback(
    (next: T | ((prev: T) => T)) => {
      setValue((prev) => {
        const resolved = typeof next === 'function' ? (next as (p: T) => T)(prev) : next
        try {
          window.localStorage.setItem(key, JSON.stringify(resolved))
        } catch {
          // 配额满/隐私模式：退化成「仅本次会话有效」，不打断用户
        }
        return resolved
      })
    },
    [key],
  )

  return [value, set] as const
}
