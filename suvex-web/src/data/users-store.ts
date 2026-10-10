/**
 * 用户管理的 store（v0.24）。
 *
 * ## 为什么单独一个文件，不并进 `settings-store`
 *
 * `settings-store` 已经在 1000 行以上，而且它管的是**配置**
 * （provider / 沙箱 / 通知 / 令牌）—— 那些是"系统怎么跑"。
 *
 * 用户管的是**权限本身**：谁能进、谁是管理员。它与配置有一个本质区别：
 *
 *   · 配置改错了 → 系统行为不对，改回来即可；
 *   · 权限改错了 → 可能**把自己锁在门外**（删掉最后一个管理员），
 *     而那时只能直接改库。
 *
 * 所以这一层刻意独立，并在几个地方做了"防自锁"的前置判断
 * （见下面 `canDelete` / `canDemote` 的说明）。
 *
 * ## 与 `auth-store` 的分工
 *
 * | | 管什么 |
 * | --- | --- |
 * | `auth-store` | **当前这个人**是谁、登没登录 |
 * | `users-store` | **系统里有哪些人**、各自什么角色 |
 *
 * 两者都要知道"谁是管理员"，但用途不同：前者决定显不显示管理入口，
 * 后者决定某一行上那几个按钮能不能点。
 */

import { api, toApiError, type ApiError } from '@/lib/api'
import type { UserRole } from './auth-store'

/** 一个用户（与后端 `UserOut` 逐字一致）。 */
export interface ManagedUser {
  id: string
  username: string
  role: UserRole
  createdAt: string
}

export interface UsersState {
  users: ManagedUser[]
  status: 'idle' | 'loading' | 'ready' | 'error'
  error: ApiError | null
}

let state: UsersState = { users: [], status: 'idle', error: null }
const listeners = new Set<() => void>()

/**
 * 快照引用必须稳定 —— `useSyncExternalStore` 每次渲染都比它，
 * 每次返回新对象会无限重渲染。所以变更一律走"整体替换"。
 */
function commit(patch: Partial<UsersState>): void {
  state = { ...state, ...patch }
  for (const listener of listeners) listener()
}

export function subscribeUsers(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function getUsersSnapshot(): UsersState {
  return state
}

export function getUsersServerSnapshot(): UsersState {
  return state
}

// ─────────────────────────────────────────────
// 读
// ─────────────────────────────────────────────

export async function loadUsers(): Promise<void> {
  commit({ status: 'loading', error: null })
  try {
    const result = await api.get<{ users: ManagedUser[] }>('/auth/users')
    commit({ users: result.users, status: 'ready', error: null })
  } catch (error) {
    commit({ status: 'error', error: toApiError(error) })
  }
}

// ─────────────────────────────────────────────
// 写
// ─────────────────────────────────────────────

export interface CreateUserInput {
  username: string
  password: string
  role: UserRole
}

/**
 * 新建用户。返回新用户，或一句错误。
 *
 * ⚠️ 返回 `{ error }` 而不是抛异常：调用方（面板）要把那句话显示在
 * 表单旁边，而 `try/catch` 会让它必须包一层。更重要的是**这里不需要
 * 区分错误类型** —— 用户名的占用、口令强度不足、角色非法，
 * 对用户来说都是"这句话告诉了我哪里不对"。
 */
export async function createUser(
  input: CreateUserInput,
): Promise<{ user: ManagedUser } | { error: string }> {
  try {
    const user = await api.post<ManagedUser>('/auth/users', input)
    commit({ users: [...state.users, user] })
    return { user }
  } catch (error) {
    return { error: toApiError(error).message }
  }
}

/**
 * 改角色。
 *
 * ⚠️ **不乐观更新**：先等服务端确认，再改本地列表。
 *
 * 理由：这个操作可能被拒（最后一个管理员不能降级），而乐观更新会让
 * 界面先显示"已改成 viewer"、再退回 admin —— 那一瞬间的闪烁会让用户
 * 以为改成功了又变回去，而不是"被拒绝了"。
 *
 * 代价是点击到生效之间有个往返延迟。对这个低频操作（改角色）
 * 那点延迟无所谓，而"不撒谎"更重要。
 */
export async function updateUserRole(
  userId: string,
  role: UserRole,
): Promise<{ user: ManagedUser } | { error: string }> {
  try {
    const user = await api.patch<ManagedUser>(`/auth/users/${userId}`, { role })
    commit({ users: state.users.map((u) => (u.id === userId ? user : u)) })
    return { user }
  } catch (error) {
    return { error: toApiError(error).message }
  }
}

export async function deleteUser(userId: string): Promise<{ ok: true } | { error: string }> {
  try {
    await api.del(`/auth/users/${userId}`)
    commit({ users: state.users.filter((u) => u.id !== userId) })
    return { ok: true }
  } catch (error) {
    return { error: toApiError(error).message }
  }
}

// ─────────────────────────────────────────────
// ★ 防自锁的前置判断（纯函数，可测）
// ─────────────────────────────────────────────

/**
 * 这个用户能不能被删除？
 *
 * 两条拒绝，与后端**逐条对应**（后端才是真正的拦截，这里只是
 * 让按钮别可点 —— 点了必然 403 的按钮是坏交互）：
 *
 *   1. **不能删自己** —— 删完立刻掉线，而如果同时是最后一个管理员
 *      就再也进不来了；
 *   2. **不能删最后一个管理员** —— 那会让系统再也没有人能改设置，
 *      而恢复只能直接改库。
 *
 * ⚠️ 返回的是**原因**（字符串）而不是布尔值。
 *    界面要显示"为什么不能删"，而不是一个灰掉的按钮 ——
 *    用户看到按钮不能点却不知道为什么，只会去猜。
 */
export function deleteBlockedReason(
  target: ManagedUser,
  all: ManagedUser[],
  currentUserId: string | null,
): string | null {
  if (target.id === currentUserId) {
    return '不能删除当前登录的账号。'
  }
  if (target.role === 'admin' && countAdmins(all) <= 1) {
    return '这是最后一个管理员，删了之后没人能再改设置。'
  }
  return null
}

/** 这个用户能不能被降级成只读？理由同 `deleteBlockedReason` 的第 2 条。 */
export function demoteBlockedReason(
  target: ManagedUser,
  all: ManagedUser[],
): string | null {
  if (target.role === 'admin' && countAdmins(all) <= 1) {
    return '这是最后一个管理员，降级之后没人能再改设置。'
  }
  return null
}

export function countAdmins(users: ManagedUser[]): number {
  return users.filter((u) => u.role === 'admin').length
}

/** 清空 store。**只给测试用** —— 与其它 store 同一条约定。 */
export function resetUsersStore(): void {
  state = { users: [], status: 'idle', error: null }
  for (const listener of listeners) listener()
}
