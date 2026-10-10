import { ApiError, api, toApiError } from '@/lib/api'
import { useSyncExternalStore } from 'react'

/**
 * 「我的视图」—— 存后端，**不存 localStorage**。
 *
 * ## 为什么不写浏览器存储
 *
 * 项目既有原则：凭据与状态不写浏览器存储。这条对视图同样成立 ——
 * 视图里装的是目标名（`q=acme-web.internal`），而 localStorage 是同源
 * 任何脚本都能读的。把它放到后端还顺带换来了跨设备一致：换台机器登录，
 * 视图还在。
 *
 * ## 为什么是覆盖式保存
 *
 * 后端只提供 `GET` / `PUT`（全量覆盖），没有增删改三个端点。视图是**短列表**
 * （上限 20 条）且编辑它的是一个人，覆盖式让前端不必维护三套调用，
 * 也不会出现"删了一条但列表没刷新"的中间态。
 *
 * ## 为什么不做乐观更新
 *
 * 覆盖式 PUT 的响应**就是**权威列表，直接拿它覆盖本地即可，不需要猜。
 * 而且这两个操作（存一条、删一条）都是低频的显式动作，用户点完会看着菜单 ——
 * 一次本地后端往返的延迟远小于"猜错了再回滚"带来的闪烁。
 * 真正需要乐观更新的是列表行的归档/删除（高频、批量、要立刻看到反馈），
 * 那些在 `scans-store` 里做了。
 */

export type ScanViewsStatus = 'idle' | 'loading' | 'ready' | 'error'

export interface ScanView {
  id: string
  name: string
  /** 原始 query 串，不含前导 `?`。编解码见 `@/lib/scan-view-query` */
  query: string
  createdAt: string
}

export interface ScanViewsSnapshot {
  views: ScanView[]
  status: ScanViewsStatus
  /** 正在保存（覆盖式 PUT 往返期间）。用来禁用重复点击，不是加载态 */
  saving: boolean
  error: ApiError | null
}

/** 后端 `MAX_SCAN_VIEWS`。前端提前拦住，避免打完一次必然失败的请求才报错 */
export const MAX_SCAN_VIEWS = 20
export const MAX_SCAN_VIEW_NAME = 60

interface ViewsResponse {
  views: ScanView[]
}

const listeners = new Set<() => void>()

let views: ScanView[] = []
let status: ScanViewsStatus = 'idle'
let saving = false
let error: ApiError | null = null
let snapshot: ScanViewsSnapshot = { views, status, saving, error }

function commit() {
  snapshot = { views, status, saving, error }
  for (const listener of listeners) listener()
}

export function subscribeScanViews(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function getScanViewsSnapshot(): ScanViewsSnapshot {
  return snapshot
}

export async function loadScanViews(): Promise<void> {
  status = 'loading'
  error = null
  commit()
  try {
    const res = await api.get<ViewsResponse>('/settings/scan-views')
    views = res.views
    status = 'ready'
    error = null
  } catch (err) {
    status = 'error'
    error = toApiError(err)
  }
  commit()
}

/**
 * 全量保存。
 *
 * `id` 为空的项是新建，由服务端分配 id —— 响应回来后本地就有真 id 了。
 * 抛错时**保留本地列表不动**并返回 false：调用方（菜单）用它决定要不要
 * 给用户弹提示。这里不把错误塞进 `error` 状态，因为菜单是浮层，
 * 错误应该就近显示在菜单里，而不是把整个列表页打成错误页。
 */
export async function saveScanViews(next: { id?: string; name: string; query: string }[]): Promise<boolean> {
  saving = true
  error = null
  commit()
  try {
    const res = await api.put<ViewsResponse>('/settings/scan-views', { views: next })
    views = res.views
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

/** 存一条新视图。名字重复时后端会 400，这里先拦一道给出更快的反馈。 */
export async function addScanView(name: string, query: string): Promise<boolean> {
  const trimmed = name.trim()
  if (!trimmed) return false
  if (views.some((v) => v.name === trimmed)) {
    error = new ApiError('VALIDATION_FAILED', '视图名称不能重复。', 400)
    commit()
    return false
  }
  return saveScanViews([
    ...views.map((v) => ({ id: v.id, name: v.name, query: v.query })),
    { name: trimmed, query },
  ])
}

export async function removeScanView(id: string): Promise<boolean> {
  return saveScanViews(
    views.filter((v) => v.id !== id).map((v) => ({ id: v.id, name: v.name, query: v.query })),
  )
}

export function useScanViews(): ScanViewsSnapshot {
  return useSyncExternalStore(subscribeScanViews, getScanViewsSnapshot, getScanViewsSnapshot)
}
