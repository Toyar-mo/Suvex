/**
 * 「文件管理」页的数据访问（v1.37）。
 *
 * 后端 `GET /chat/files` 跨会话列出「我的」产物文件；每个会话的产物就是
 * 沙箱 `/work/persist` 在宿主上的那一份工作区（v1.36）。下载复用会话级
 * 端点，URL 由 `lib/workFiles.workspaceFileUrl` 拼，避免两处各写一遍。
 */

import { api } from '@/lib/api'

/** 产物来源。当前只有工作区；后端留了字符串以便以后扩展。 */
export type FileSource = 'workspace'

/** 清单里的一行。 */
export interface WorkspaceFileRow {
  sessionId: string
  sessionTitle: string
  project: string | null
  source: FileSource
  /** 相对会话工作区根目录的 POSIX 路径。 */
  path: string
  name: string
  sizeBytes: number
  /** 修改时间（epoch 秒）。 */
  modifiedAt: number
}

/** 过滤条件（全部可选；空字符串视作未填）。 */
export interface FileFilters {
  sessionId?: string
  projectId?: string
  name?: string
  source?: 'all' | FileSource
}

export type FilesStatus = 'loading' | 'ready' | 'error'

/** 拉取文件清单。过滤条件全部下推给后端（见后端 docstring 的说明）。 */
export async function listFiles(filters: FileFilters = {}): Promise<WorkspaceFileRow[]> {
  const qs = new URLSearchParams()
  if (filters.sessionId?.trim()) qs.set('sessionId', filters.sessionId.trim())
  if (filters.projectId?.trim()) qs.set('projectId', filters.projectId.trim())
  if (filters.name?.trim()) qs.set('name', filters.name.trim())
  if (filters.source && filters.source !== 'all') qs.set('source', filters.source)
  const suffix = qs.toString() ? `?${qs.toString()}` : ''
  return api.get<WorkspaceFileRow[]>(`/chat/files${suffix}`)
}

/** 删除工作区里的一个文件（不可恢复）。 */
export async function deleteFile(sessionId: string, path: string): Promise<void> {
  await api.del<void>(
    `/chat/sessions/${encodeURIComponent(sessionId)}/files?path=${encodeURIComponent(path)}`,
  )
}

/** 人类可读的文件大小。 */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '—'
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let value = bytes / 1024
  let index = 0
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024
    index += 1
  }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[index]}`
}

/** 把 epoch 秒格式化成 `YYYY-MM-DD HH:mm`（本地时区）。 */
export function formatTimestamp(epochSeconds: number): string {
  if (!Number.isFinite(epochSeconds) || epochSeconds <= 0) return '—'
  const date = new Date(epochSeconds * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}`
}

/** 只取日期部分（表格「日期」列）。 */
export function formatDate(epochSeconds: number): string {
  const stamp = formatTimestamp(epochSeconds)
  return stamp === '—' ? stamp : stamp.slice(0, 10)
}

/** 相对路径的父目录；根目录返回 "(根目录)"。 */
export function subPathOf(path: string): string {
  const cut = path.lastIndexOf('/')
  return cut <= 0 ? '(根目录)' : path.slice(0, cut)
}
