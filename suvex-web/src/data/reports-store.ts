import { ApiError, api, toApiError } from '@/lib/api'
import type { ReportDocument } from '@/lib/report-document'
import { useCallback, useSyncExternalStore } from 'react'
import type { Report, ReportFormat, ReportOptions, ReportScope } from './report'

/**
 * 报告历史的**单一运行时事实来源**。
 *
 * 与 findings-store / scans-store 同一套模式（useSyncExternalStore + 整体替换快照），
 * 理由也一样：ReportsPage 生成一份报告后，历史列表要立刻出现它，
 * 而两处各自 useState 一份快照就会漂移。
 *
 * ## 任务 7：mock 换成了什么
 *
 * 原来这里有两件事是假的，现在都从后端来：
 *
 *   1. **初始历史**：原先 `seedReports()` 会造两份假报告
 *      （`report_seed_4f2a` / `report_seed_9c11`，编号还长得挺像真的）。
 *      假历史比假列表更糟 —— 用户会以为系统里躺着两份别人生成的交付物，
 *      而"这份报告是谁、什么时候生成的"正是历史功能唯一的价值。
 *      现在初始为空，进页面看到的是「还没有生成过报告」。
 *
 *   2. **生成与删除**：原先在前端内存里改数组。现在
 *      `POST /reports` 与 `DELETE /reports/{id}`。
 *      **编号（`code`）也由后端分配** —— 前端原先自己从 id 派生一套，
 *      与后端那套必然分叉（见 `data/report.ts` 的说明）。
 *
 * ## 历史文档单独一个按需加载的 store
 *
 * 历史预览读 `GET /reports/{id}/document`：后端按**冻结的
 * `report_findings` 顺序**重建，而不是按 scope 重新过滤。
 * 它和列表是两次请求，各有各的加载态 —— 列表已经出来时点开某一份，
 * 不应该把整个页面打回骨架。
 */

type Listener = () => void

export type ReportsStatus = 'loading' | 'ready' | 'error'

export interface ReportsSnapshot {
  reports: Report[]
  status: ReportsStatus
  /** ★ 真的 `ApiError`，不是一句文案 —— ErrorState 要靠它区分失败种类 */
  error: ApiError | null
}

const listeners = new Set<Listener>()

/**
 * ★ **初始为空** —— 原来是两份假报告。理由见文件头。
 */
let reports: Report[] = []
let dataStatus: ReportsStatus = 'loading'
let loadError: ApiError | null = null

/** 快照必须引用稳定，否则 useSyncExternalStore 会无限重渲染 */
let snapshot: ReportsSnapshot = { reports, status: dataStatus, error: loadError }

function emit() {
  for (const listener of listeners) listener()
}

function commit() {
  snapshot = { reports, status: dataStatus, error: loadError }
  emit()
}

export function subscribeReports(listener: Listener): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function getReportsSnapshot(): ReportsSnapshot {
  return snapshot
}

export function getReportsServerSnapshot(): ReportsSnapshot {
  return snapshot
}

// ─────────────────────────────────────────────
// 列表
// ─────────────────────────────────────────────

/**
 * 读报告历史 —— `GET /reports`。
 *
 * `silent` 与 scans/findings 同义：重新挂载时静默刷新，
 * 不把已经显示的列表打回骨架，失败也不把页面换成错误页。
 */
export async function loadReports(options: { silent?: boolean } = {}): Promise<void> {
  const silent = options.silent ?? false

  if (!silent) {
    dataStatus = 'loading'
    loadError = null
    commit()
  }

  try {
    reports = await api.get<Report[]>('/reports')
    dataStatus = 'ready'
    loadError = null
  } catch (error) {
    if (!silent) {
      dataStatus = 'error'
      loadError = toApiError(error)
    }
  }
  commit()
}

// ─────────────────────────────────────────────
// 变更操作
// ─────────────────────────────────────────────

/**
 * 生成一份报告 —— `POST /reports`。
 *
 * ★ **不再传 findingIds。** 原来的 `createReport` 由调用方算好命中集合
 *   再传进来，前端于是同时负责"筛哪些"和"记什么"。现在这两件事
 *   都在服务端：它按 scope 查库、应用选项、**在同一个事务里**
 *   冻结 `findingIds` 与 `excludedCount`。
 *
 *   这是必要的：前端算出来的 id 列表与服务端查到的不可能总是一致
 *   （并发扫描会在这两次读之间写入新发现），而报告冻结的必须是
 *   服务端**实际收录**的那一批 —— 否则历史列表的条数与报告正文对不上。
 *
 * 也不传 `now`：生成时刻由服务端定为 `created_at`，
 * 编号由它派生。前端传时刻会让"编号"和"入库时间"来自两个时钟。
 */
export interface CreateReportInput {
  title: string
  scope: ReportScope
  options: ReportOptions
  format: ReportFormat
}

export async function createReport(input: CreateReportInput): Promise<Report> {
  const report = await api.post<Report>('/reports', {
    title: input.title,
    scope: input.scope,
    options: input.options,
    format: input.format,
  })

  // 最新生成排最前 —— 与后端 `created_at desc` 的顺序一致
  reports = [report, ...reports]
  commit()
  return report
}

/**
 * 删除一份历史报告 —— `DELETE /reports/{id}`（204）。
 *
 * ★ 失败会**抛错**，不再返回 `false`。原来返回布尔值时，
 *   调用方很容易把"没删掉"和"删掉了"都走同一个分支 ——
 *   而删除是不可逆的，用户必须知道它到底成没成。
 */
export async function removeReport(id: string): Promise<void> {
  await api.del<void>(`/reports/${id}`)
  reports = reports.filter((r) => r.id !== id)
  commit()
}

// ─────────────────────────────────────────────
// 草稿预览（后端构建）
// ─────────────────────────────────────────────

/**
 * ★ 草稿预览为什么也走后端（`POST /reports/preview`），而不是前端
 *   `buildReportDocument`。
 *
 *   任务 7 之后前端**拿不到构建报告所需的数据**：`GET /findings` 返回的是
 *   **摘要**（契约 §3.4 —— 列表可能几百条，带上完整证据文本会让响应到几 MB），
 *   里面没有 `detail.description` / `evidence` / `reasoning` / `poc` / `remediation`。
 *
 *   症状一开始很隐蔽：预览照常渲染出来了，标题、严重度、状态、时间全对，
 *   只有**「问题描述」和「证据」两节是空的** —— 而"这条漏洞没有内容"
 *   和"内容没取到"在屏幕上长得一模一样。
 *
 *   顺带解决的另一件事：预览与"下载到的 Markdown"现在都由服务端渲染，
 *   两者不可能分叉。前端那份 `buildReportDocument` 仍留在 lib 里给
 *   对账脚本当参照物（契约 §3.5.1）。
 */

/** 探测"这个范围里到底有什么"时用的全开选项 */
const PROBE_OPTIONS: ReportOptions = {
  includePoc: true,
  includeReasoning: true,
  onlyConfirmed: false,
  includeRemediation: true,
}

/**
 * 范围内的内容可用性 —— 决定配置区哪些开关该被禁用。
 *
 * ⚠️ 少了一项 `unconfirmed`（"范围内有没有待确认/误报"）：
 *   后端文档的条目只带 `statusLabel`（中文标签），没有机器可读的 status，
 *   拿中文去比是在赌文案不变。那一项由页面从**摘要**算 ——
 *   摘要里有 `status` 原值（见 ReportsPage 的 `available`）。
 */
export interface AvailableContent {
  poc: boolean
  reasoning: boolean
  remediation: boolean
}

export interface ReportPreviewState {
  status: 'idle' | 'loading' | 'ready' | 'error'
  doc: ReportDocument | null
  /** 范围内实际能提供的内容。null = 还没探测出来 */
  available: AvailableContent | null
  error: ApiError | null
}

const IDLE_PREVIEW: ReportPreviewState = {
  status: 'idle',
  doc: null,
  available: null,
  error: null,
}

const previewListeners = new Set<Listener>()
let preview: ReportPreviewState = IDLE_PREVIEW
/** 探测结果按**范围**缓存：范围没变就不必再探一次 */
let probeKey: string | null = null
/** 丢弃过期响应：用户连点几下开关时，先发的可能后到 */
let previewEpoch = 0

function emitPreview() {
  for (const listener of previewListeners) listener()
}

function commitPreview() {
  emitPreview()
}

export function subscribeReportPreview(listener: Listener): () => void {
  previewListeners.add(listener)
  return () => previewListeners.delete(listener)
}

export function getReportPreviewSnapshot(): ReportPreviewState {
  return preview
}

export interface LoadReportPreviewInput {
  scope: ReportScope
  options: ReportOptions
  title: string
}

/**
 * 拉一份草稿预览 —— `POST /reports/preview`。
 *
 * 范围**或**选项变了都要重拉（调一次开关就想看到结果，这是报告页的核心体验）。
 * 调用方负责 debounce，见 ReportsPage 的 effect。
 *
 * 范围变化时**额外**发一次全开选项的探测请求：`available` 是范围的属性，
 * 与当前选项无关，用当前选项问不出"这里有没有 PoC"（没勾就等于被裁掉了）。
 * 探测结果按范围缓存，所以只有换范围才会多发这一个请求。
 *
 * ⚠️ 刷新期间**保留旧文档**（`doc` 不清空）：每调一次开关就把右侧打回
 *   骨架屏，会让整页闪个不停。调用方据此显示"更新中"而不是骨架。
 */
export async function loadReportPreview(input: LoadReportPreviewInput): Promise<void> {
  const epoch = ++previewEpoch
  const key = JSON.stringify(input.scope)
  const needProbe = probeKey !== key

  preview = { ...preview, status: 'loading', error: null }
  commitPreview()

  try {
    const [doc, probeDoc] = await Promise.all([
      api.post<ReportDocument>('/reports/preview', {
        title: input.title,
        scope: input.scope,
        options: input.options,
      }),
      needProbe
        ? api.post<ReportDocument>('/reports/preview', {
            title: input.title,
            scope: input.scope,
            options: PROBE_OPTIONS,
          })
        : null,
    ])
    // 期间又改过一次 —— 这次的结果已经过期，丢掉
    if (epoch !== previewEpoch) return

    preview = {
      doc,
      status: 'ready',
      error: null,
      available: probeDoc
        ? {
            // 全开选项下还为空，就说明这个范围里真的没有 —— 不是被裁掉的
            poc: probeDoc.entries.some((entry) => entry.poc !== null),
            reasoning: probeDoc.entries.some((entry) => entry.reasoning !== null),
            remediation: probeDoc.entries.some((entry) => entry.remediation !== null),
          }
        : preview.available,
    }
    if (probeDoc) probeKey = key
  } catch (error) {
    if (epoch !== previewEpoch) return
    preview = { ...preview, status: 'error', error: toApiError(error) }
  }
  commitPreview()
}

/** 订阅草稿预览 */
export function useReportPreview(): ReportPreviewState {
  return useSyncExternalStore(
    subscribeReportPreview,
    getReportPreviewSnapshot,
    getReportPreviewSnapshot,
  )
}

/** 仅供测试与开发：清空 store（**不再复位到 mock**） */
export function resetReports(): void {
  reports = []
  dataStatus = 'loading'
  loadError = null
  documentStates = {}
  documentSnapshot = {}
  preview = IDLE_PREVIEW
  probeKey = null
  commit()
  emitDocument()
  emitPreview()
}

// ─────────────────────────────────────────────
// 历史文档（按需加载）
// ─────────────────────────────────────────────

export interface ReportDocumentState {
  status: 'idle' | 'loading' | 'ready' | 'error'
  doc: ReportDocument | null
  error: ApiError | null
}

/** 未加载过的 id 都返回它 —— 共享引用，保证快照稳定 */
const IDLE_DOCUMENT: ReportDocumentState = { status: 'idle', doc: null, error: null }

const documentListeners = new Set<Listener>()
let documentStates: Record<string, ReportDocumentState> = {}
let documentSnapshot: Record<string, ReportDocumentState> = documentStates

/** 每个 id 一个序号，用来丢弃过期响应（快速连点两份报告时后发先至） */
const documentEpoch: Record<string, number> = {}

function emitDocument() {
  for (const listener of documentListeners) listener()
}

function commitDocument() {
  documentSnapshot = { ...documentStates }
  emitDocument()
}

export function subscribeReportDocument(listener: Listener): () => void {
  documentListeners.add(listener)
  return () => documentListeners.delete(listener)
}

export function getReportDocumentSnapshot(id: string): ReportDocumentState {
  return documentSnapshot[id] ?? IDLE_DOCUMENT
}

/**
 * 取一份历史报告的文档 —— `GET /reports/{id}/document`。
 *
 * ⚠️ **必须走后端，不能用列表项 + 当前 findings 在本地重放。**
 *   报告冻结的是"哪几条、什么顺序、什么选项"，条目内容（状态、时间线）
 *   取自库里的当前值 —— 后端 `service.document_for` 就是这么做的，
 *   前端 `rebuildReportDocument` 也一样。两者语义相同，
 *   但只有后端那条路径读的是**服务端的**当前值，
 *   而前端 store 里的 findings 是它自己那一次快照，可能已经落后。
 *   预览与"下载到的 Markdown"必须是同一份东西，所以两边都走服务端。
 */
export async function loadReportDocument(id: string): Promise<void> {
  const current = documentSnapshot[id]
  if (current?.status === 'loading' || current?.status === 'ready') return
  await fetchReportDocument(id)
}

/** 强制重取 */
export async function refreshReportDocument(id: string): Promise<void> {
  if (documentSnapshot[id]?.status === 'loading') return
  await fetchReportDocument(id)
}

async function fetchReportDocument(id: string): Promise<void> {
  const epoch = (documentEpoch[id] ?? 0) + 1
  documentEpoch[id] = epoch

  documentStates = {
    ...documentStates,
    [id]: { status: 'loading', doc: documentSnapshot[id]?.doc ?? null, error: null },
  }
  commitDocument()

  try {
    const doc = await api.get<ReportDocument>(`/reports/${id}/document`)
    if (documentEpoch[id] !== epoch) return
    documentStates = { ...documentStates, [id]: { status: 'ready', doc, error: null } }
  } catch (error) {
    if (documentEpoch[id] !== epoch) return
    documentStates = {
      ...documentStates,
      [id]: { status: 'error', doc: null, error: toApiError(error) },
    }
  }
  commitDocument()
}

/**
 * 订阅一份报告的文档状态。
 *
 * `id` 为 null（没有打开任何一份）时返回 idle —— 让调用方可以无条件调用它，
 * 不必在组件里写条件 hook（那违反 hooks 规则）。
 */
export function useReportDocument(id: string | null): ReportDocumentState {
  const subscribe = useCallback((listener: Listener) => subscribeReportDocument(listener), [])
  const getSnapshot = useCallback(
    () => (id ? getReportDocumentSnapshot(id) : IDLE_DOCUMENT),
    [id],
  )
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
}

// ─────────────────────────────────────────────
// 订阅 hook
// ─────────────────────────────────────────────

export function useReportsState(): ReportsSnapshot {
  return useSyncExternalStore(subscribeReports, getReportsSnapshot, getReportsServerSnapshot)
}

/** 只要列表本身（调用方不关心加载态时用） */
export function useReports(): Report[] {
  return useReportsState().reports
}
