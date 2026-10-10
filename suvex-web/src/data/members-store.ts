import { ApiError, api, toApiError } from '@/lib/api'
import { useSyncExternalStore } from 'react'

/**
 * 处理人名单 —— 独立的小 store（任务 10 P2）。
 *
 * ## 为什么不并进 `findings-store`
 *
 * 两者的**加载时机不同**：漏洞列表进页面就要拉，而名单只在两种情况下需要
 * —— 渲染"负责人"那一列的名字（其实那由 `Finding.assigneeName` 直接给了）、
 * 以及用户真的要点「指派」时。
 *
 * 并进去的代价是：每次打开漏洞页都多打一个与它无关的请求，
 * 而那个请求的失败还要混进 `findings-store` 的错误状态里 ——
 * 页面会显示"漏洞加载失败"，而其实漏洞好好的。
 *
 * 判据与 `scan-retention` 那条一致：**它有自己的端点、自己的加载状态、
 * 自己的失败原因**，三者都独立时就该独立。
 */

export type MembersStatus = 'idle' | 'loading' | 'ready' | 'error'

export interface Member {
  id: string
  name: string
  email: string | null
  active: boolean
  createdAt: string
}

export interface MembersSnapshot {
  members: Member[]
  status: MembersStatus
  saving: boolean
  error: ApiError | null
}

/** 后端 `MAX_MEMBERS`。前端提前拦住，避免打完一次必然失败的请求才报错 */
export const MAX_MEMBERS = 200

const listeners = new Set<() => void>()

let members: Member[] = []
let status: MembersStatus = 'idle'
let saving = false
let error: ApiError | null = null
let snapshot: MembersSnapshot = { members, status, saving, error }

function commit() {
  snapshot = { members, status, saving, error }
  for (const listener of listeners) listener()
}

export function subscribeMembers(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function getMembersSnapshot(): MembersSnapshot {
  return snapshot
}

/**
 * 拉名单。**默认含已停用** —— 历史指派要能显示名字。
 *
 * 已经 `ready` 时不重复拉（`silent` 除外）：这个 store 被指派下拉框和
 * 成员管理面板共用，两边都挂载时会各拉一次。
 */
export async function loadMembers(options: { force?: boolean } = {}): Promise<void> {
  if (!options.force && status === 'ready') return
  status = 'loading'
  error = null
  commit()
  try {
    members = await api.get<Member[]>('/members')
    status = 'ready'
    error = null
  } catch (err) {
    status = 'error'
    error = toApiError(err)
  }
  commit()
}

/**
 * 新增。
 *
 * 失败时**不改本地列表**并返回 false：调用方（面板）用它决定要不要显示
 * 错误。不把错误塞进 `error` 状态，因为那是"整块加载失败"的意思，
 * 而这里是"这一次操作没成功"—— 两者在界面上的表现完全不同。
 */
export async function createMember(name: string, email: string | null): Promise<boolean> {
  saving = true
  error = null
  commit()
  try {
    const created = await api.post<Member>('/members', { name, email })
    // 后端按名字排序返回，本地插入也保持有序 —— 否则新加的人会跳到最后，
    // 而列表看起来"没加上"
    members = [...members, created].sort((a, b) => a.name.localeCompare(b.name, 'zh'))
    status = 'ready'
    return true
  } catch (err) {
    error = toApiError(err)
    return false
  } finally {
    saving = false
    commit()
  }
}

export async function updateMember(
  id: string,
  patch: { name?: string; email?: string | null; active?: boolean },
): Promise<boolean> {
  saving = true
  error = null
  commit()
  try {
    const updated = await api.patch<Member>(`/members/${id}`, patch)
    members = members
      .map((m) => (m.id === id ? updated : m))
      .sort((a, b) => a.name.localeCompare(b.name, 'zh'))
    return true
  } catch (err) {
    error = toApiError(err)
    return false
  } finally {
    saving = false
    commit()
  }
}

export async function deleteMember(id: string): Promise<boolean> {
  saving = true
  error = null
  commit()
  try {
    await api.del<void>(`/members/${id}`)
    members = members.filter((m) => m.id !== id)
    return true
  } catch (err) {
    error = toApiError(err)
    return false
  } finally {
    saving = false
    commit()
  }
}

export function useMembers(): MembersSnapshot {
  return useSyncExternalStore(subscribeMembers, getMembersSnapshot, getMembersSnapshot)
}

/**
 * 只取可指派的候选（`active` 的那些）。
 *
 * 参数是**数组**而不是 snapshot：调用方拿到的常常已经是 `snapshot.members`
 * （或者从 props 传进来的一份名单），让它为了调这个函数再拼一个假
 * snapshot 出来，是在用一个多余的形状去换一个不存在的灵活性。
 */
export function activeMembers(members: Member[]): Member[] {
  return members.filter((m) => m.active)
}
