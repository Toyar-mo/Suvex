import { useSyncExternalStore } from 'react'

import {
  THEME_STORAGE_KEY,
  applyTheme,
  readStoredPreference,
  subscribeSystemTheme,
  type ResolvedTheme,
  type ThemePreference,
} from '@/lib/theme'

/**
 * 主题状态 —— 一个模块级外部 store，组件用 `useSyncExternalStore` 订阅。
 *
 * ## 为什么是外部 store，而不是 Context / 组件 state
 *
 * 与 `toast-store` / `scans-store` 同一套模式，但这里还有一条额外的理由：
 * 主题**必须在 React 挂载之前就被读到并写进 DOM**（见 `initTheme`）。
 * 那时还没有任何 Provider 可挂，所以状态不能住在 React 树里。
 *
 * ## 为什么快照里是「偏好 + 解析值」两个值
 *
 * 因为界面上它们回答的是两个不同问题：
 *   · 偏好（`system` / `light` / `dark`）→ 菜单里哪一项打勾；
 *   · 解析值（`light` / `dark`）→ 按钮上是太阳还是月亮。
 * 只留一个的话，「跟随系统」这个选项就没法表达了 —— 而它恰恰是缺省值。
 *
 * ⚠️ 快照必须**引用稳定**：`useSyncExternalStore` 每次渲染都拿它比较，
 *    每次返回新对象会无限重渲染。所以只在真变化时才新建，见 `commitIfChanged`。
 */

export interface ThemeSnapshot {
  /** 用户在菜单里选的（含「跟随系统」） */
  preference: ThemePreference
  /** 此刻真正生效的 */
  resolved: ResolvedTheme
}

/*
  初始值刻意是 `system` + `dark` —— 它与两件事实一致：
    · 没读过存储时，"跟随系统"是缺省；
    · `:root` 本来就是暗色（CSS 侧兜底方向，见 lib/theme.ts 文件头）。
  即便如此，**首帧前仍必须调用 `initTheme()`**（`main.tsx` 里），
  否则存储里存的是 light 时，右上角图标会先画成月亮。
*/
let preference: ThemePreference = 'system'
let resolved: ResolvedTheme = 'dark'
let snapshot: ThemeSnapshot = { preference, resolved }

const listeners = new Set<() => void>()

/**
 * 当前挂着的系统监听解绑函数。
 *
 * ★ 它是**唯一持有者**：要么是 null（没在听），要么是那一对的解绑函数。
 *   所有挂/摘都走 `syncSystemSubscription`，所以不存在"挂了两份"或
 *   "该摘没摘"的中间态 —— 后者正是监听器泄漏的成因。
 */
let detachSystemTheme: (() => void) | null = null

let initialized = false

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

function getSnapshot(): ThemeSnapshot {
  return snapshot
}

/** 只在快照真的变了才通知 —— 否则同一个选项点两次会白发一轮渲染 */
function commitIfChanged(): void {
  if (snapshot.preference === preference && snapshot.resolved === resolved) return
  snapshot = { preference, resolved }
  for (const listener of listeners) listener()
}

/**
 * 系统换色。
 *
 * 只可能在偏好为 `system` 时被调用 —— 一旦选了显式的亮/暗，
 * 下面 `syncSystemSubscription` 会把监听摘掉，所以这里不需要再判一次。
 */
function handleSystemChange(): void {
  resolved = applyTheme(preference)
  commitIfChanged()
}

function stopSystemSubscription(): void {
  const detach = detachSystemTheme
  detachSystemTheme = null
  detach?.()
}

/**
 * 让「系统配色监听」与当前偏好**严格对齐**。
 *
 * ★ 判据是**目标状态与现状是否一致**，而不是"偏好变了就重挂"：
 *     · 本来在听 system，又选了 system → 什么都不做（不重挂、不漏）；
 *     · system → light/dark        → 摘掉，`detachSystemTheme` 归 null；
 *     · light/dark → system        → 重新挂上，且只挂这一份。
 *   三种情形共用同一条路径，所以"解绑"不可能被某个分支漏掉。
 *
 *   ⚠️ 这就是监听器会积累的那个坑：只写"进 system 时 addEventListener"、
 *   没有对称的 removeEventListener，切十次就攒下十个监听器，
 *   它们还会在系统换色时一起触发 —— 而症状只是"卡了一下"，很难查。
 *   项目里 `App.tsx` 注销 401 回调、`ScanViewsMenu` 摘 document 监听
 *   都是同一条纪律。
 *
 *   也不采用"每次重挂前先无条件摘一次"的写法：那在 StrictMode / HMR
 *   下会把另一个实例的监听一起摘掉，反而漏听。
 */
function syncSystemSubscription(): void {
  if (preference === 'system') {
    // 已经在听就不重复挂：重复挂会让同一次系统换色触发两遍
    if (detachSystemTheme) return
    detachSystemTheme = subscribeSystemTheme(handleSystemChange)
    return
  }
  // 选了显式的亮/暗：必须摘掉系统监听，否则它会在系统换色时
  // 覆盖用户刚做的选择 —— 用户会觉得"我的选择被系统改回去了"
  stopSystemSubscription()
}

/**
 * 写存储。
 *
 * ⚠️ 写失败**不回滚内存里的偏好**：隐私模式下每次写入都会抛，
 *   若因为写不进去就把界面弹回去，用户看到的是"点了没反应"。
 *   这里退化成「仅本次会话有效」—— 与 `useLocalStorage` 对同一问题的处理一致。
 */
function persistPreference(next: ThemePreference): void {
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, next)
  } catch {
    // 配额满 / 隐私模式 / 存储被禁用：不影响本次会话
  }
}

/** 切换偏好：写存储 → 应用到 DOM → 对齐系统监听 → 通知订阅者 */
export function setThemePreference(next: ThemePreference): void {
  preference = next
  persistPreference(next)
  resolved = applyTheme(next)
  syncSystemSubscription()
  commitIfChanged()
}

/**
 * 应用启动时调用**一次**（在 `main.tsx` 里、`createRoot` 之前）。
 *
 * 它做两件事：把存储里的偏好落到 DOM，以及建立系统变化监听。
 *
 * ★ 为什么在 main.tsx 而不是 `App.tsx` 的 effect 里：主题属于"首帧就要对"
 *   的那类状态。放在 effect 里意味着第一次渲染已经发生过了 —— 那时右上角的
 *   图标会先画成缺省值再跳成真实值（存储里是 light 时尤其明显），
 *   而 `App` 的 effect 在 StrictMode 下还会跑第二遍。
 *
 *   注意它与 `index.html` 里那段内联脚本**不是重复劳动**：那段脚本管
 *   "bundle 还没下载完时的首帧不要闪"，它立不了监听（也没人管）；
 *   这里管的是"store 快照与真实偏好对齐 + 建立系统换色监听"。
 *
 * 幂等：重复调用只有第一次生效。HMR 会重新执行模块体，而重复绑定
 * 系统监听正是要防的事。
 */
export function initTheme(): void {
  if (initialized) return
  initialized = true
  preference = readStoredPreference()
  resolved = applyTheme(preference)
  syncSystemSubscription()
  commitIfChanged()
}

/** 非 React 调用方（启动流程、测试）读当前状态 —— 与 `scan-views` 的 getter 同形 */
export function getThemeSnapshot(): ThemeSnapshot {
  return snapshot
}

export function useTheme(): ThemeSnapshot {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
}
