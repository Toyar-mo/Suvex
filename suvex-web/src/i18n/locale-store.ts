import { resolveLocale, type LocaleId } from './index'

/**
 * 会话外读取当前语言的桥。
 *
 * `formatRelativeTime` / `formatDuration` 这些纯函数不在 React 树里，
 * 拿不到 Context；但它们的结果**必须跟着语言变**（中文"3 小时前"、
 * 日文"3 時間前"、英文"3 hours ago"）。这里直接读同一份 localStorage +
 * 浏览器语言，规则与 Provider 完全一致（同一个 `resolveLocale`）。
 *
 * ⚠️ 不要在这里做缓存：Provider 切换语言后，下一次调用必须立刻看到新值。
 */
export function getActiveLocale(): LocaleId {
  if (typeof localStorage === 'undefined') return 'zh-CN'
  const stored = localStorage.getItem('suvex.locale') as LocaleId | 'auto' | null
  if (!stored) return 'zh-CN'
  return resolveLocale(stored)
}
