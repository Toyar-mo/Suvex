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

/**
 * 上传时可选的会话（v1.39）。
 *
 * ★ 为什么要有它：工作区是按会话隔离的，上传必须先指定会话；但「文件管理」
 *   页本身没有"当前会话"，而会话 ID（`chat_01M...`）**不在这一页上**——用户
 *   被要求填一个自己看不到的东西，只能去对话页翻地址栏。这是设计缺陷。
 *   改成下拉选会话，列表直接来自 `GET /chat/sessions`，同时把 ID 显示在选项
 *   边上（副标题），用户一眼就能对照。
 */
export interface UploadSessionOption {
  id: string
  title: string
}

/**
 * 拉「我能上传到哪些会话」的列表。
 *
 * 只取未归档的：归档会话的后端 `POST .../files` 会 400「这个会话已归档」，
 * 与其让用户选中后再失败，不如干脆不列出来。
 */
export async function listUploadSessions(): Promise<UploadSessionOption[]> {
  const sessions = await api.get<Array<{ id: string; title: string; archived?: boolean }>>(
    '/chat/sessions',
  )
  return sessions
    .filter((s) => !s.archived)
    .map((s) => ({ id: s.id, title: s.title }))
}

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

/** 上传回执。 */
export interface UploadedFile {
  path: string
  name: string
  sizeBytes: number
}

/**
 * 往某个会话的工作区上传一个文件（v1.38「文件管理」页的「上传文件」）。
 *
 * 与对话页附件上传不同：这里写进 `/work/persist/uploads/`，长期留存，
 * 上传后立刻出现在本页清单里。必须指定会话 —— 工作区是按会话隔离的，
 * 「文件管理」页本身没有"当前会话"，所以由调用方先选一个。
 *
 * ⚠️ 用 `api.upload`（multipart），**不能** `api.post(path, {file})`：
 * `post` 会 `JSON.stringify` 请求体，`JSON.stringify(FormData)` 等于 `"{}"`，
 * 服务端收到一个没有文件的请求却**不报错**（见 `lib/api.ts` 的注记）。
 */
export async function uploadFile(sessionId: string, file: File): Promise<UploadedFile> {
  const form = new FormData()
  // 字段名必须是 `file` —— 后端 `upload_workspace_file(file: UploadFile = File(...))`
  // 按这个名字取。写得不一样会得到 422「field required」，而不是"没传文件"。
  form.append('file', file, file.name)
  return api.upload<UploadedFile>(
    `/chat/sessions/${encodeURIComponent(sessionId)}/files`,
    form,
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
