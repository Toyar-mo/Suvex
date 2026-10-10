import { api, ApiError } from '@/lib/api'
import { useSyncExternalStore } from 'react'

/**
 * 认证状态的**单一事实来源**（契约 §0.3、§3.1、§8）。
 *
 * ## 为什么要有这一层
 *
 * 契约 §0.3 决定加登录之后，前端的启动顺序变成：
 *
 * ```
 *   GET /auth/state ──┬─ initialized=false ──→ 初始化页（只出现一次）
 *                     ├─ authenticated=false ─→ 登录页
 *                     └─ authenticated=true ───→ 主界面
 * ```
 *
 * 这个判断**只能有一处**。写在 App 里但让 Sidebar 自己再查一次、
 * 或者让每个页面各自处理 401，就会出现"页面显示已登录、侧栏显示未登录"
 * 这种自相矛盾的状态 —— 而用户根本不知道该信哪个。
 *
 * 用模块级 store + `useSyncExternalStore`（和 `findings-store` 同一套），
 * 不套 Provider：401 拦截发生在 `lib/api.ts` 里，那是 React 树**外面**。
 * 它需要一个不依赖组件的入口来把状态清掉。
 *
 * ## `offline` 是一个独立的状态，不能混进 `login`
 *
 * 后端没起时前端**无法判断** initialized —— 这时候显示登录表单是误导：
 * 用户会去输口令，然后拿到一个和口令毫无关系的失败。
 * 所以单独一个状态，界面直接说"连不上后端"并给重试。
 * 这个状态在开发态极其常见（忘了起服务），值得为它多一个分支。
 */

// ─────────────────────────────────────────────
// 类型
// ─────────────────────────────────────────────

/** 契约 §3.1 的 `UserOut`。**故意只有这两个字段** —— 后端也不返回别的 */
export interface AuthUser {
  id: string
  createdAt: string
}

export type AuthPhase =
  /** 正在问 `/auth/state`，还不知道该显示什么 */
  | 'loading'
  /** 后端连不上（请求根本没拿到应答） */
  | 'offline'
  /** 系统未初始化 —— 显示初始化页，**只会出现一次** */
  | 'setup'
  /** 已初始化但未登录 */
  | 'login'
  /** 已登录，可以进主界面 */
  | 'ready'

export interface AuthSnapshot {
  phase: AuthPhase
  user: AuthUser | null
  /** 上一次 login / setup 的失败提示。成功后清空 */
  error: string | null
  /** login / setup 请求进行中 —— 按钮据此禁用并显示"正在…" */
  pending: boolean
}

// ─────────────────────────────────────────────
// store
// ─────────────────────────────────────────────

const INITIAL: AuthSnapshot = {
  phase: 'loading',
  user: null,
  error: null,
  pending: false,
}

let snapshot: AuthSnapshot = INITIAL
const listeners = new Set<() => void>()

/**
 * 快照必须**引用稳定** —— `useSyncExternalStore` 每次渲染都比较它，
 * 返回新对象会无限重渲染。所以变更一律走"整体替换"。
 */
function setState(patch: Partial<AuthSnapshot>): void {
  snapshot = { ...snapshot, ...patch }
  for (const listener of listeners) listener()
}

export function subscribeAuth(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function getAuthSnapshot(): AuthSnapshot {
  return snapshot
}

/** 服务端/预渲染快照 —— 纯 CSR，用同一个即可 */
export function getAuthServerSnapshot(): AuthSnapshot {
  return snapshot
}

export function useAuth(): AuthSnapshot {
  return useSyncExternalStore(subscribeAuth, getAuthSnapshot, getAuthServerSnapshot)
}

// ─────────────────────────────────────────────
// 动作
// ─────────────────────────────────────────────

/** 把任意异常收敛成给用户看的一句话 */
function messageOf(error: unknown): string {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error && error.message) return error.message
  return '发生了未知错误，请稍后重试。'
}

/**
 * 问一次 `/auth/state` 并决定显示哪个界面。
 *
 * App 启动时调一次；`offline` 状态下的"重试"也调它。
 */
export async function bootstrapAuth(): Promise<void> {
  setState({ phase: 'loading', error: null })

  let state: { initialized: boolean; authenticated: boolean }
  try {
    // ⚠️ `skipAuthRedirect`：这个端点返回 401 是**正常结果**（未登录），
    //    不该触发"跳登录页"—— 我们本来就在决定要不要显示登录页。
    state = await api.get('/auth/state', { skipAuthRedirect: true, timeoutMs: 8000 })
  } catch (error) {
    const apiError = error instanceof ApiError ? error : null
    if (apiError?.isNetworkError) {
      setState({ phase: 'offline', user: null, error: apiError.message })
    } else {
      // 非网络错误（极少见：反代返回了坏响应）——仍停在登录页并说明原因
      setState({ phase: 'login', user: null, error: messageOf(error) })
    }
    return
  }

  if (!state.initialized) {
    setState({ phase: 'setup', user: null })
    return
  }

  if (!state.authenticated) {
    setState({ phase: 'login', user: null })
    return
  }

  // 已登录 —— 顺手把用户信息取回来。它只用来显示"已登录"，
  // 失败也不影响进入主界面（会话可能是刚过期的，交给 401 拦截处理）。
  try {
    const user = await api.get<AuthUser>('/auth/me', { skipAuthRedirect: true })
    setState({ phase: 'ready', user, error: null })
  } catch {
    setState({ phase: 'ready', user: null, error: null })
  }
}

/** 登录。成功返回 true（调用方据此决定是否跳转） */
export async function login(password: string): Promise<boolean> {
  setState({ pending: true, error: null })
  try {
    const result = await api.post<{ user: AuthUser }>(
      '/auth/login',
      { password },
      // 口令错误就是 401 —— 跳登录页毫无意义（我们已经在登录页了）
      { skipAuthRedirect: true },
    )
    setState({ phase: 'ready', user: result.user, pending: false, error: null })
    return true
  } catch (error) {
    setState({ pending: false, error: messageOf(error) })
    return false
  }
}

/** 初始化。成功后后端会**自动登录**（契约 §3.1） */
export async function setupAdmin(password: string): Promise<boolean> {
  setState({ pending: true, error: null })
  try {
    const result = await api.post<{ user: AuthUser }>(
      '/auth/setup',
      { password },
      { skipAuthRedirect: true },
    )
    setState({ phase: 'ready', user: result.user, pending: false, error: null })
    return true
  } catch (error) {
    setState({ pending: false, error: messageOf(error) })
    return false
  }
}

/**
 * 登出。
 *
 * 即使请求失败也**必须**把本地状态清掉：用户的意图是"我要离开"，
 * 而不是"如果服务端同意我才离开"。一个失败的登出请求把界面留在
 * 已登录状态，是这一页最不该出现的行为。
 */
export async function logout(): Promise<void> {
  try {
    await api.post('/auth/logout', undefined, { skipAuthRedirect: true })
  } catch {
    // 忽略 —— 后端不可达时也要能登出（清掉本地状态即可）
  }
  setState({ phase: 'login', user: null, error: null, pending: false })
}

/**
 * 会话失效（任何请求收到 401）时调用。
 *
 * ★ 由 `lib/api.ts` 的 401 拦截触发（契约 §8：「不能只在登录接口处理」）。
 *   保留 `error` 之外的一切重置 —— 并把 phase 推回 `login`，
 *   路由守则会随之把用户带到登录页。
 *
 * 幂等：多个并发请求同时 401 时会被调用多次，重复设置同一状态无害
 * （`setState` 只是替换快照，不会产生额外的跳转）。
 */
export function handleUnauthorized(): void {
  // 已经在登录/初始化页时不要打断用户 —— 那可能是他自己输错了口令
  if (snapshot.phase === 'login' || snapshot.phase === 'setup' || snapshot.phase === 'offline') {
    return
  }
  setState({ phase: 'login', user: null, pending: false })
}

/** 清掉上一次的失败提示（用户重新输入时调用） */
export function clearAuthError(): void {
  if (snapshot.error !== null) setState({ error: null })
}

/** 仅测试用：回到初始状态 */
export function resetAuthStore(): void {
  setState({ ...INITIAL })
}
