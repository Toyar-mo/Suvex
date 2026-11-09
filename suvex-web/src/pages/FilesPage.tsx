import {
  ChevronDown,
  ChevronUp,
  ChevronsUpDown,
  Copy,
  Download,
  Eye,
  FileDown,
  FolderOpen,
  Hash,
  RefreshCw,
  Search,
  Trash2,
  Upload,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { PageHeader } from '@/components/layout/AppShell'
import ConfirmDialog from '@/components/ui/ConfirmDialog'
import Checkbox from '@/components/ui/Checkbox'
import EmptyState from '@/components/ui/EmptyState'
import ErrorState from '@/components/ui/ErrorState'
import Modal from '@/components/ui/Modal'
import { Button, IconButton } from '@/components/ui/Button'
import { SkeletonRows } from '@/components/ui/Skeleton'
import { cn } from '@/lib/cn'
import { toApiError, type ApiError } from '@/lib/api'
import { PERSIST_PREFIX, workspaceFileUrl } from '@/lib/workFiles'
import { showToast } from '@/data/toast-store'
import {
  deleteFile,
  exportFilesZip,
  formatBytes,
  formatTimestamp,
  listFiles,
  listUploadSessions,
  subPathOf,
  uploadFile,
  type FileFilters,
  type FilesStatus,
  type UploadSessionOption,
  type WorkspaceFileRow,
} from '@/data/files'

/**
 * 文件管理 —— 「对话里产出的东西，都在哪、怎么拿走」。
 *
 * ## 这一页回答什么
 *
 * 模型在沙箱里跑出来的报告、脚本、证据、截图，散落在各个会话的
 * `/work/persist/` 里（v1.36 起持久化到宿主）。用户要知道「我这几天
 * 让它做过哪些文件、在哪、多大」，并能直接下载 —— 而不是回到每个
 * 会话里翻聊天记录找路径。
 *
 * 版式参照 CyberStrikeAI 的文件管理：一张带筛选的表格，列是
 * 选择 / 会话 / 子路径 / 文件名 / 来源 / 大小 / 修改时间 / 操作。
 * 「修改时间」已经包含日期，不再单列「日期」。
 *
 * ## 为什么筛选在服务端
 *
 * 工作区文件可能有上万个。把 `sessionId` / `projectId` / `name` 下推到
 * 后端（见 `app/chat/router.py` 的 `list_all_workspace_files`），命中
 * 单会话时连别的会话目录都不扫。
 *
 * ## 为什么「复制路径」复制的是 `/work/persist/...`
 *
 * 用户复制路径多半是为了回对话里让模型接着用它。模型看到的世界是
 * 容器的 `/work/persist`，不是宿主目录 —— 复制宿主路径对它是无效的。
 */

type GroupBy = 'none' | 'session' | 'project'

/** 可排序的列。前三个是文本，后两个是数值。 */
type SortKey = 'sessionTitle' | 'path' | 'name' | 'sizeBytes' | 'modifiedAt'

interface FileSort {
  key: SortKey
  dir: 'asc' | 'desc'
}

interface FilterDraft {
  sessionId: string
  projectId: string
  name: string
  source: 'all' | 'workspace'
}

const EMPTY_DRAFT: FilterDraft = { sessionId: '', projectId: '', name: '', source: 'all' }

const GROUP_OPTIONS: ReadonlyArray<{ value: GroupBy; label: string }> = [
  { value: 'none', label: '不分组（平铺）' },
  { value: 'session', label: '按会话' },
  { value: 'project', label: '按项目' },
]

const INPUT_CLASS =
  'w-full rounded-sm border border-line bg-canvas px-2.5 py-1.5 text-sm text-ink placeholder:text-ink-muted focus:border-accent focus:outline-none'

/**
 * 每页行数。
 *
 * ★ 为什么必须有分页：设计审查实测 5,000 个文件时这一页渲染了
 *   **20,008 个 button**、文档高 **234,855px**。行内四个图标按钮 × 5000 行，
 *   滚动、筛选、Tab 遍历全被拖垮（键盘用户要经过两万个停靠点）。
 *   100 行/页把 DOM 压到约 1/50，同时保住"一眼扫一屏"的密度。
 *
 * ★ 为什么是分页而不是虚拟滚动：这里是 `<table>`，还带按会话/项目分组；
 *   虚拟滚动要求固定行高并自己接管表头、分组行和滚动锚点，分组一开就对不齐。
 *   分页对分组是天然兼容的 —— 页边界可以落在组内（标「续」），
 *   而 DOM 规模只跟页大小有关，跟总行数无关。
 */
const PAGE_SIZE = 100

interface RowGroup {
  key: string
  label: string
  rows: WorkspaceFileRow[]
}

/**
 * 把「分组」切成「页」。
 *
 * 一个组可能大于一页（5000 个文件全在同一个会话里），所以不能简单地
 * "每页 N 个组"：组要能在页边界处断开，断开的第二段标「（续）」。
 */
function paginate(units: RowGroup[], pageSize: number): RowGroup[][] {
  const pages: RowGroup[][] = []
  let current: RowGroup[] = []
  let count = 0

  for (const unit of units) {
    let rest = unit.rows
    let part = 0
    while (rest.length > 0) {
      const room = pageSize - count
      const take = rest.slice(0, room)
      current.push({
        key: `${unit.key}#${part}`,
        label: part === 0 ? unit.label : `${unit.label}（续）`,
        rows: take,
      })
      count += take.length
      rest = rest.slice(room)
      part += 1
      if (count >= pageSize) {
        pages.push(current)
        current = []
        count = 0
      }
    }
  }

  if (current.length > 0) pages.push(current)
  return pages
}

/** 会话 ID 太长，列表里截短显示；完整值放在 title 与「复制会话 ID」按钮里。 */
function shortSessionId(id: string): string {
  // chat_01M6ABC... 这种前缀 + 随机段；保留头 12 位 + 尾 4 位即可辨认同一个。
  if (id.length <= 18) return id
  return `${id.slice(0, 12)}…${id.slice(-4)}`
}

/** 行标识：会话内 path 唯一，跨会话必须带上 sessionId。 */
function fileKeyOf(row: WorkspaceFileRow): string {
  return `${row.sessionId}:${row.path}`
}

export default function FilesPage() {
  const [rows, setRows] = useState<WorkspaceFileRow[]>([])
  const [status, setStatus] = useState<FilesStatus>('loading')
  const [error, setError] = useState<ApiError | null>(null)
  /**
   * 清单被服务端上限截断了。
   *
   * ★ v1.79：后端 `MAX_LISTED_FILES`（5000）截断时以前**什么都不说**
   *   （响应是裸数组，标记算完就丢了）。界面于是把 5000 当成"一共就这些"。
   *   现在顶部会说清"只显示了最近修改的前 N 个"，并指路到筛选。
   */
  const [truncated, setTruncated] = useState(false)
  const [listLimit, setListLimit] = useState(0)
  const [draft, setDraft] = useState<FilterDraft>(EMPTY_DRAFT)
  const [applied, setApplied] = useState<FileFilters>({})
  const [groupBy, setGroupBy] = useState<GroupBy>('none')
  /** 当前页（0 起）。筛选、分组、上传、删除后都回到第一页。 */
  const [page, setPage] = useState(0)
  const [pendingDelete, setPendingDelete] = useState<WorkspaceFileRow | null>(null)
  const [deleting, setDeleting] = useState(false)
  /** 批量选择：key = `${sessionId}:${path}`（path 在会话内唯一）。 */
  const [selected, setSelected] = useState<ReadonlySet<string>>(() => new Set())
  const [pendingBulkDelete, setPendingBulkDelete] = useState<WorkspaceFileRow[] | null>(null)
  const [bulkDeleting, setBulkDeleting] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [sort, setSort] = useState<FileSort>({ key: 'modifiedAt', dir: 'desc' })
  const [preview, setPreview] = useState<WorkspaceFileRow | null>(null)
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  const [uploading, setUploading] = useState(false)
  // v1.39：上传目标会话改成下拉选（会话 ID 不在这一页上，让用户手填看不到的东西是设计缺陷）。
  const [uploadSessions, setUploadSessions] = useState<UploadSessionOption[]>([])
  const [uploadTarget, setUploadTarget] = useState('')
  /**
   * 可选会话的加载状态。
   *
   * ★ v1.79：以前失败**完全静默**，下拉显示「（没有会话）」并提示
   *   "先去对话页开一个" —— 网络出错时这是一句假话，用户会跑去对话页
   *   开一堆会话，回来发现还是空的。失败要能看见、能重试。
   */
  const [sessionsStatus, setSessionsStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const [sessionsError, setSessionsError] = useState<ApiError | null>(null)

  const loadUploadSessions = useCallback(() => {
    setSessionsStatus('loading')
    setSessionsError(null)
    void listUploadSessions()
      .then((items) => {
        setUploadSessions(items)
        setSessionsStatus('ready')
      })
      .catch((err) => {
        // 不挡住「浏览/下载/删除」这些只读能力，只让上传那一块说清原因
        setSessionsError(toApiError(err))
        setSessionsStatus('error')
      })
  }, [])

  // 拉一次可选会话（失败可在下拉旁重试）
  useEffect(() => {
    const timer = window.setTimeout(() => loadUploadSessions(), 0)
    return () => window.clearTimeout(timer)
  }, [loadUploadSessions])

  const load = useCallback(async (filters: FileFilters) => {
    setStatus('loading')
    setError(null)
    try {
      const data = await listFiles(filters)
      setRows(data.items)
      setTruncated(data.truncated)
      setListLimit(data.limit)
      // 筛选/删除后，选择里可能留下已经不存在的行；清掉，避免批量操作打到幻影。
      setSelected((prev) => {
        if (prev.size === 0) return prev
        const alive = new Set(data.items.map(fileKeyOf))
        const next = new Set([...prev].filter((key) => alive.has(key)))
        return next.size === prev.size ? prev : next
      })
      setStatus('ready')
    } catch (err) {
      setError(toApiError(err))
      setStatus('error')
    }
  }, [])

  useEffect(() => {
    // ★ 放到宏任务里再拉：`load` 一进来就同步 `setStatus('loading')`，
    //   直接在 effect 体里调用会被 react-hooks/set-state-in-effect 判成
    //   「effect 内同步 setState → 级联渲染」。挪出同步执行栈即可，
    //   0ms 对用户不可感（反正随后就是一次网络请求）。
    const timer = window.setTimeout(() => void load(applied), 0)
    return () => window.clearTimeout(timer)
  }, [applied, load])

  const search = () => {
    setPage(0)
    setApplied({ ...draft })
  }
  const refresh = () => void load(applied)

  /**
   * 排序在**前端**做：列表最多 5000 行，当前页只渲染 100 行；下推到后端
   * 要多一套查询参数与索引，收益不成比例。默认与后端一致（修改时间倒序），
   * 不点表头时行为完全不变。
   */
  const sortedRows = useMemo(() => {
    const factor = sort.dir === 'asc' ? 1 : -1
    return [...rows].sort((a, b) => {
      if (sort.key === 'sizeBytes') return (a.sizeBytes - b.sizeBytes) * factor
      if (sort.key === 'modifiedAt') return (a.modifiedAt - b.modifiedAt) * factor
      if (sort.key === 'path') return a.path.localeCompare(b.path, 'zh') * factor
      if (sort.key === 'name') return a.name.localeCompare(b.name, 'zh') * factor
      // 剩下只有 sessionTitle；空标题回退到会话 ID，否则它们会全部并列。
      const left = a.sessionTitle || a.sessionId
      const right = b.sessionTitle || b.sessionId
      return left.localeCompare(right, 'zh') * factor
    })
  }, [rows, sort])

  const toggleSort = (key: SortKey) => {
    setPage(0)
    setSort((prev) =>
      prev.key === key
        ? { key, dir: prev.dir === 'asc' ? 'desc' : 'asc' }
        : // 数值列先给"大的在前"（与默认的最近修改一致），文本列给正序。
          { key, dir: key === 'sizeBytes' || key === 'modifiedAt' ? 'desc' : 'asc' },
    )
  }

  /** 分组后的渲染单元。空组不渲染。 */
  const units = useMemo<RowGroup[]>(() => {
    if (groupBy === 'none') return [{ key: 'all', label: '', rows: sortedRows }]
    const map = new Map<string, RowGroup>()
    for (const row of sortedRows) {
      const key = groupBy === 'session' ? row.sessionId : (row.project ?? '(未标项目)')
      const label = groupBy === 'session' ? row.sessionTitle : (row.project ?? '(未标项目)')
      let bucket = map.get(key)
      if (!bucket) {
        bucket = { key, label, rows: [] }
        map.set(key, bucket)
      }
      bucket.rows.push(row)
    }
    return [...map.values()]
  }, [sortedRows, groupBy])

  const pages = useMemo(() => paginate(units, PAGE_SIZE), [units])
  const pageCount = Math.max(1, pages.length)
  // 数据变少（筛选、删除）后页码可能越界：夹一下，不要报错也不要空白页
  const currentPage = Math.min(page, pageCount - 1)
  const groups = useMemo(() => pages[currentPage] ?? [], [pages, currentPage])

  /** 当前页的行。全选只作用于当前页——避免"点了全选，删掉看不见的 5000 个"。 */
  const pageRows = useMemo(() => groups.flatMap((group) => group.rows), [groups])
  const allOnPageSelected =
    pageRows.length > 0 && pageRows.every((row) => selected.has(fileKeyOf(row)))
  const selectedRows = useMemo(
    () => rows.filter((row) => selected.has(fileKeyOf(row))),
    [rows, selected],
  )

  const toggleRow = (row: WorkspaceFileRow) => {
    const key = fileKeyOf(row)
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  const toggleAllOnPage = () => {
    setSelected((prev) => {
      if (allOnPageSelected) {
        const next = new Set(prev)
        for (const row of pageRows) next.delete(fileKeyOf(row))
        return next
      }
      return new Set([...prev, ...pageRows.map(fileKeyOf)])
    })
  }

  const copySelectedPaths = async () => {
    if (selectedRows.length === 0) return
    const text = selectedRows.map((row) => `${PERSIST_PREFIX}${row.path}`).join('\n')
    try {
      await navigator.clipboard.writeText(text)
      showToast({ tone: 'ok', message: `已复制 ${selectedRows.length} 条路径` })
    } catch {
      showToast({ tone: 'risk', message: '复制失败，请手动选择。' })
    }
  }

  /**
   * 批量删除。逐条串行：一次几十个并发 DELETE 会把后端与磁盘同时打满，
   * 而这里本来就只处理当前页（≤100）的选择。失败项不静默——
   * 逐个汇总进 toast，成功的照常删掉。
   */
  const confirmBulkDelete = async () => {
    const files = pendingBulkDelete ?? []
    if (files.length === 0) return
    setBulkDeleting(true)
    let ok = 0
    const failures: string[] = []
    for (const row of files) {
      try {
        await deleteFile(row.sessionId, row.path)
        ok += 1
      } catch (err) {
        failures.push(`${row.name}：${toApiError(err).message}`)
      }
    }
    setBulkDeleting(false)
    setPendingBulkDelete(null)
    setSelected(new Set())
    if (ok > 0) showToast({ tone: 'ok', message: `已删除 ${ok} 个文件` })
    if (failures.length > 0) {
      showToast({
        tone: 'risk',
        message:
          failures.length === 1 ? failures[0] : `${failures.length} 个文件删除失败`,
      })
    }
    void load(applied)
  }

  /**
   * 把勾选的文件一次性下载成 ZIP。
   *
   * 用 `URL.createObjectURL` + 临时 `<a download>`：ZIP 是刚生成的 blob，
   * 没有可直接导航的 URL。`revokeObjectURL` 推迟 10 秒 —— 点完立刻 revoke
   * 会让部分浏览器把已经开始下载的 blob 取消。
   */
  const exportSelected = async () => {
    if (selectedRows.length === 0) return
    setExporting(true)
    try {
      const { blob, filename } = await exportFilesZip(
        selectedRows.map((row) => ({ sessionId: row.sessionId, path: row.path })),
      )
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = filename
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      window.setTimeout(() => URL.revokeObjectURL(url), 10_000)
      showToast({ tone: 'ok', message: `已打包 ${selectedRows.length} 个文件` })
    } catch (err) {
      showToast({ tone: 'risk', message: toApiError(err).message })
    } finally {
      setExporting(false)
    }
  }

  const copyPath = async (row: WorkspaceFileRow) => {
    const value = `${PERSIST_PREFIX}${row.path}`
    try {
      await navigator.clipboard.writeText(value)
      showToast({ tone: 'ok', message: `已复制路径：${value}` })
    } catch {
      showToast({ tone: 'risk', message: '复制失败，请手动选择路径。' })
    }
  }

  const copySessionId = async (row: WorkspaceFileRow) => {
    try {
      await navigator.clipboard.writeText(row.sessionId)
      showToast({ tone: 'ok', message: `已复制会话 ID：${row.sessionId}` })
    } catch {
      showToast({ tone: 'risk', message: '复制失败，请手动选择会话 ID。' })
    }
  }

  const confirmDelete = async () => {
    if (!pendingDelete) return
    setDeleting(true)
    try {
      await deleteFile(pendingDelete.sessionId, pendingDelete.path)
      showToast({ tone: 'ok', message: `已删除 ${pendingDelete.name}` })
      setPendingDelete(null)
      void load(applied)
    } catch (err) {
      showToast({ tone: 'risk', message: toApiError(err).message })
    } finally {
      setDeleting(false)
    }
  }

  const exportCsv = () => {
    if (sortedRows.length === 0) {
      showToast({ tone: 'info', message: '当前没有可导出的结果。' })
      return
    }
    // 与界面同序（用户点了表头排序，导出的就是他看到的顺序）；不再单列「日期」。
    const header = ['会话', '项目', '子路径', '文件名', '来源', '大小(字节)', '修改时间']
    const lines = sortedRows.map((row) =>
      [
        row.sessionTitle,
        row.project ?? '',
        subPathOf(row.path),
        row.name,
        row.source === 'workspace' ? '工作目录' : row.source,
        String(row.sizeBytes),
        formatTimestamp(row.modifiedAt),
      ]
        .map((cell) => `"${cell.replace(/"/g, '""')}"`)
        .join(','),
    )
    const blob = new Blob([`\uFEFF${[header.join(','), ...lines].join('\n')}`], {
      type: 'text/csv;charset=utf-8',
    })
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `suvex-files-${Date.now()}.csv`
    anchor.click()
    URL.revokeObjectURL(url)
  }

  /**
   * 当前选定的上传目标会话（v1.40）。
   *
   * ★ 只认下拉里真正列出的会话，**不再回落到筛选栏里的自由文本**。
   *   曾经的回落（`uploadTarget || draft.sessionId`）有个真实事故：用户往
   *   筛选栏「会话 ID」里写了标题「解题」，上传时就把「解题」当会话 ID 发出去，
   *   后端 `get_session_or_404` 返回 404 → 页面报「这个对话不存在，可能已被删除」。
   *   自由文本框是给人**筛**的，不是给人**填 ID** 的；上传目标必须是校验过的真实会话。
   */
  const resolveUploadTarget = (): UploadSessionOption | null => {
    if (!uploadTarget) return null
    return uploadSessions.find((s) => s.id === uploadTarget) ?? null
  }

  /** 点击「上传文件」：先确定目标会话，再拉起文件选择框。 */
  const pickUpload = () => {
    // 工作区是按会话隔离的，这一页没有"当前会话"——必须让用户先指定一个。
    const target = resolveUploadTarget()
    if (!target) {
      showToast({ tone: 'info', message: '先在「上传到会话」里选一个会话，再上传。' })
      return
    }
    fileInputRef.current?.click()
  }

  const onFilesChosen = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const target = resolveUploadTarget()
    const files = event.target.files ? Array.from(event.target.files) : []
    // 立刻清空 value：否则连续选同一个文件不会再触发 change。
    event.target.value = ''
    if (!target || files.length === 0) return

    setUploading(true)
    let ok = 0
    const failures: string[] = []
    for (const file of files) {
      try {
        await uploadFile(target.id, file)
        ok += 1
      } catch (err) {
        failures.push(`${file.name}：${toApiError(err).message}`)
      }
    }
    setUploading(false)

    if (ok > 0) {
      // ★ 上传成功后把筛选切到**这个会话**并带上它的标题 ——
      //   否则刷新后列表回到"全部会话"，用户会怀疑"是不是没按会话区分/传到别处了"。
      //   顺带把筛选栏的会话 ID 也填上，让"这些文件属于哪个会话"一眼可见。
      const nextFilters: FileFilters = { sessionId: target.id }
      setDraft({ ...EMPTY_DRAFT, sessionId: target.id })
      setPage(0)
      setApplied(nextFilters)
      showToast({
        tone: 'ok',
        message: `已上传 ${ok} 个文件到「${target.title || target.id}」`,
      })
      void load(nextFilters)
    }
    if (failures.length > 0) {
      showToast({
        tone: 'risk',
        message: failures.length === 1 ? failures[0] : `${failures.length} 个文件上传失败`,
      })
    }
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <PageHeader navId="files" />
      <div className="flex flex-col gap-panel px-gutter py-bay">
        {/* 筛选条 —— 版式对齐 CyberStrikeAI：一行四个条件 + 两个动作 */}
        <section className="rounded-md border border-line bg-surface p-panel">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <label className="flex flex-col gap-1.5 text-sm">
              <span className="text-ink-muted">会话 ID</span>
              <input
                type="text"
                value={draft.sessionId}
                onChange={(e) => setDraft({ ...draft, sessionId: e.target.value })}
                placeholder="留空表示全部"
                className={INPUT_CLASS}
              />
            </label>
            <label className="flex flex-col gap-1.5 text-sm">
              <span className="text-ink-muted">项目 ID</span>
              <input
                type="text"
                value={draft.projectId}
                onChange={(e) => setDraft({ ...draft, projectId: e.target.value })}
                placeholder="留空表示全部"
                className={INPUT_CLASS}
              />
            </label>
            <label className="flex flex-col gap-1.5 text-sm">
              <span className="text-ink-muted">文件名</span>
              <input
                type="text"
                value={draft.name}
                onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') search()
                }}
                placeholder="筛选文件名"
                className={INPUT_CLASS}
              />
            </label>
            <label className="flex flex-col gap-1.5 text-sm">
              <span className="text-ink-muted">来源</span>
              <select
                value={draft.source}
                onChange={(e) =>
                  setDraft({ ...draft, source: e.target.value as FilterDraft['source'] })
                }
                className={INPUT_CLASS}
              >
                <option value="all">全部</option>
                <option value="workspace">工作目录</option>
              </select>
            </label>
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <label className="text-sm text-ink-muted">分组方式</label>
            <select
              value={groupBy}
              onChange={(e) => {
                setPage(0)
                setGroupBy(e.target.value as GroupBy)
              }}
              className={cn(INPUT_CLASS, 'w-auto')}
            >
              {GROUP_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
            <Button variant="primary" size="sm" onClick={search}>
              <Search size={14} strokeWidth={2} aria-hidden="true" />
              搜索
            </Button>
            {/* 窄屏必须换行：这一组（上传目标 + 三个按钮）在 390px 下宽 507px，
                不换行就把整页撑出 168px 的横向滚动。宽屏才靠 ml-auto 靠右。 */}
            <div className="flex w-full flex-wrap items-center gap-2 sm:ml-auto sm:w-auto">
              {/* min-w-0 + 固定宽度：`<select>` 的固有宽度由最长选项决定，
                  不给宽度它会撑到 682px，整页横向滚动。 */}
              <div className="flex min-w-0 max-w-full items-center gap-1.5 text-sm">
                <span className="whitespace-nowrap text-ink-muted">上传到会话</span>
                <select
                  value={uploadTarget}
                  onChange={(e) => setUploadTarget(e.target.value)}
                  disabled={uploading || sessionsStatus !== 'ready' || uploadSessions.length === 0}
                  title={
                    sessionsStatus === 'error'
                      ? '会话列表加载失败 —— 点旁边的「重试」'
                      : sessionsStatus === 'loading'
                        ? '正在加载会话…'
                        : uploadSessions.length === 0
                          ? '还没有能上传的会话，先去对话页开一个'
                          : undefined
                  }
                  className={cn(INPUT_CLASS, 'w-40 min-w-0 sm:w-60')}
                  data-testid="upload-target"
                >
                  <option value="">
                    {sessionsStatus === 'loading'
                      ? '（正在加载会话…）'
                      : sessionsStatus === 'error'
                        ? '（会话列表加载失败）'
                        : uploadSessions.length === 0
                          ? '（没有会话）'
                          : '请选择会话…'}
                  </option>
                  {uploadSessions.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.title || '(无标题)'} · {s.id}
                    </option>
                  ))}
                </select>
                {/*
                  ★ 失败不能只写成「（没有会话）」—— 那是在说
                  "你还没有会话"，而真相是"没问到"。旁边给重试。
                */}
                {sessionsStatus === 'error' && (
                  <button
                    type="button"
                    onClick={loadUploadSessions}
                    title={sessionsError?.message ?? '重新拉取会话列表'}
                    className="whitespace-nowrap text-xs text-signal-risk underline-offset-2 hover:underline"
                  >
                    会话加载失败，重试
                  </button>
                )}
              </div>
              <input
                ref={fileInputRef}
                type="file"
                multiple
                className="hidden"
                onChange={(event) => void onFilesChosen(event)}
                data-testid="upload-input"
              />
              <Button variant="primary" size="sm" onClick={pickUpload} disabled={uploading}>
                <Upload size={14} strokeWidth={2} aria-hidden="true" />
                {uploading ? '上传中…' : '上传文件'}
              </Button>
              <Button variant="secondary" size="sm" onClick={exportCsv}>
                <FileDown size={14} strokeWidth={2} aria-hidden="true" />
                导出当前结果
              </Button>
              <Button variant="secondary" size="sm" onClick={refresh}>
                <RefreshCw size={14} strokeWidth={2} aria-hidden="true" />
                刷新
              </Button>
            </div>
          </div>
        </section>

        {/* 结果表 */}
        <section className="overflow-hidden rounded-md border border-line bg-surface">
          <div className="flex items-center justify-between border-b border-line px-gutter py-2.5 text-sm text-ink-muted">
            <span>
              {truncated
                ? `显示最近修改的 ${rows.length} 个文件（服务端上限 ${listLimit} 个）`
                : `共 ${rows.length} 个文件`}
            </span>
            {status === 'loading' && <span>加载中…</span>}
          </div>

          {/*
            截断必须自己说出来。以前这里只写「共 5000 个文件」——
            而 5000 恰好是服务端上限，用户会以为"我的旧产物丢了"。
          */}
          {truncated && (
            <p
              role="status"
              className="bg-sunken border-b border-line px-gutter py-2 text-xs text-signal-warn"
            >
              命中文件超过服务端上限，这里只列出最近修改的前 {listLimit} 个，
              更早的文件没有显示。用上面的「会话 ID / 项目 ID / 文件名」缩小范围，
              或直接在对应会话的对话页里找。
            </p>
          )}

          {selected.size > 0 && (
            <div className="flex flex-wrap items-center gap-2 border-b border-line bg-accent-wash/40 px-gutter py-2 text-sm">
              <span className="tabular text-ink" data-numeric>
                已选 {selected.size} 个
              </span>
              <Button variant="secondary" size="sm" onClick={() => void copySelectedPaths()}>
                <Copy size={14} strokeWidth={2} aria-hidden="true" />
                复制路径
              </Button>
              <Button
                variant="secondary"
                size="sm"
                disabled={exporting}
                onClick={() => void exportSelected()}
              >
                <Download size={14} strokeWidth={2} aria-hidden="true" />
                {exporting ? '打包中…' : '下载 ZIP'}
              </Button>
              <Button
                variant="danger"
                size="sm"
                onClick={() => setPendingBulkDelete(selectedRows)}
              >
                <Trash2 size={14} strokeWidth={2} aria-hidden="true" />
                删除选中
              </Button>
              <Button variant="ghost" size="sm" onClick={() => setSelected(new Set())}>
                取消选择
              </Button>
            </div>
          )}

          {status === 'loading' && rows.length === 0 ? (
            <div className="p-gutter">
              <SkeletonRows rows={6} rowClassName="h-10" />
            </div>
          ) : status === 'error' ? (
            <div className="p-gutter">
              <ErrorState error={error} onRetry={refresh} />
            </div>
          ) : rows.length === 0 ? (
            <div className="p-gutter">
              <EmptyState
                icon={FolderOpen}
                title="还没有文件"
                description="模型在对话里写进 /work/persist/ 的报告、脚本、证据会出现在这里。让它在对话里把产物写进 /work/persist/ 即可。"
              />
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[880px] border-collapse text-sm">
                <thead>
                  <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-ink-muted">
                    <th className="w-10 px-3 py-2 font-medium">
                      <Checkbox
                        id="files-select-all"
                        checked={allOnPageSelected}
                        indeterminate={selected.size > 0 && !allOnPageSelected}
                        onChange={toggleAllOnPage}
                        label="全选当前页"
                      />
                    </th>
                    <SortableTh label="会话" sortKey="sessionTitle" sort={sort} onSort={toggleSort} />
                    <SortableTh label="子路径" sortKey="path" sort={sort} onSort={toggleSort} />
                    <SortableTh label="文件名" sortKey="name" sort={sort} onSort={toggleSort} />
                    <th className="px-3 py-2 font-medium">来源</th>
                    <SortableTh
                      label="大小"
                      sortKey="sizeBytes"
                      sort={sort}
                      onSort={toggleSort}
                      align="right"
                    />
                    <SortableTh
                      label="修改时间"
                      sortKey="modifiedAt"
                      sort={sort}
                      onSort={toggleSort}
                    />
                    <th className="px-3 py-2 text-right font-medium">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {groups.map((group) => (
                    <FileGroupBody
                      key={group.key}
                      label={group.label}
                      rows={group.rows}
                      selected={selected}
                      onToggle={toggleRow}
                      onPreview={setPreview}
                      onCopy={copyPath}
                      onCopySession={copySessionId}
                      onDelete={setPendingDelete}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {pageCount > 1 && (
            <div
              className="flex flex-wrap items-center justify-between gap-2 border-t border-line px-gutter py-2 text-sm text-ink-muted"
              data-pagination
            >
              <span className="tabular" data-numeric>
                第 {currentPage + 1} / {pageCount} 页 · 每页 {PAGE_SIZE} 条
              </span>
              <div className="flex items-center gap-2">
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => setPage(Math.max(0, currentPage - 1))}
                  disabled={currentPage === 0}
                >
                  上一页
                </Button>
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => setPage(Math.min(pageCount - 1, currentPage + 1))}
                  disabled={currentPage >= pageCount - 1}
                >
                  下一页
                </Button>
              </div>
            </div>
          )}
        </section>
      </div>

      <ConfirmDialog
        open={pendingDelete !== null}
        title="删除文件"
        description={`将删除「${pendingDelete?.name ?? ''}」。这是磁盘上的真实文件，删除后无法恢复。`}
        confirmLabel="删除"
        busy={deleting}
        onConfirm={() => void confirmDelete()}
        onCancel={() => setPendingDelete(null)}
      />
      <ConfirmDialog
        open={pendingBulkDelete !== null}
        title="批量删除文件"
        description={`将删除选中的 ${pendingBulkDelete?.length ?? 0} 个文件。这是磁盘上的真实文件，删除后无法恢复。`}
        confirmLabel="全部删除"
        busy={bulkDeleting}
        onConfirm={() => void confirmBulkDelete()}
        onCancel={() => {
          if (!bulkDeleting) setPendingBulkDelete(null)
        }}
      />
      <FilePreviewDialog
        key={preview ? fileKeyOf(preview) : 'none'}
        row={preview}
        onClose={() => setPreview(null)}
      />
    </div>
  )
}

/** 表头里的一列：点一下切换排序，箭头表示当前方向。 */
function SortableTh({
  label,
  sortKey,
  sort,
  onSort,
  align = 'left',
}: {
  label: string
  sortKey: SortKey
  sort: FileSort
  onSort: (key: SortKey) => void
  align?: 'left' | 'right'
}) {
  const active = sort.key === sortKey
  return (
    <th
      className={cn('px-3 py-2 font-medium', align === 'right' && 'text-right')}
      aria-sort={active ? (sort.dir === 'asc' ? 'ascending' : 'descending') : 'none'}
    >
      <button
        type="button"
        onClick={() => onSort(sortKey)}
        className={cn(
          'inline-flex items-center gap-1 transition-colors hover:text-ink',
          active && 'text-ink',
        )}
      >
        {label}
        {active ? (
          sort.dir === 'asc' ? (
            <ChevronUp size={13} aria-hidden="true" />
          ) : (
            <ChevronDown size={13} aria-hidden="true" />
          )
        ) : (
          <ChevronsUpDown size={12} className="opacity-40" aria-hidden="true" />
        )}
      </button>
    </th>
  )
}

//: 在线预览的体积上限。超过就只给下载 —— 不能让一次点击读几百 MB 进浏览器。
const PREVIEW_MAX_TEXT_BYTES = 1024 * 1024
const PREVIEW_MAX_IMAGE_BYTES = 16 * 1024 * 1024
const IMAGE_EXTENSIONS: Record<string, true> = {
  png: true, jpg: true, jpeg: true, gif: true, webp: true, svg: true, bmp: true, ico: true, avif: true,
}
const TEXT_EXTENSIONS: Record<string, true> = {
  txt: true, md: true, markdown: true, py: true, js: true, mjs: true, ts: true, tsx: true,
  jsx: true, sh: true, bash: true, zsh: true, json: true, log: true, csv: true, tsv: true,
  html: true, htm: true, css: true, scss: true, yml: true, yaml: true, xml: true, sql: true,
  c: true, h: true, cpp: true, hpp: true, go: true, rs: true, java: true, kt: true, rb: true,
  php: true, ini: true, conf: true, toml: true, env: true, diff: true, patch: true,
}

/**
 * 预览一张工作区文件。
 *
 * 图片走 `<img src>`（同源、带 Cookie），文本用 fetch 读正文后放进 `<pre>`
 * —— 都是只读操作，不新增后端端点。HTML/SVG 在这里不会执行脚本：
 * HTML 是当纯文本渲染，SVG 由 `<img>` 加载（浏览器不会在其中跑脚本）。
 */
function FilePreviewDialog({
  row,
  onClose,
}: {
  row: WorkspaceFileRow | null
  onClose: () => void
}) {
  const [text, setText] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const url = row ? workspaceFileUrl(row.sessionId, row.path) : ''
  const ext = row?.name.includes('.') ? (row.name.split('.').pop() ?? '').toLowerCase() : ''
  const isImage = IMAGE_EXTENSIONS[ext] === true
  const isText = !isImage && (TEXT_EXTENSIONS[ext] === true || ext === '')
  const tooBig =
    row !== null &&
    row.sizeBytes > (isImage ? PREVIEW_MAX_IMAGE_BYTES : PREVIEW_MAX_TEXT_BYTES)

  useEffect(() => {
    if (!row || !isText || tooBig) return
    const controller = new AbortController()
    // 放进宏任务：effect 体内同步 setState 会触发级联渲染告警（项目统一这样处理）。
    const timer = window.setTimeout(() => {
      setLoading(true)
      fetch(url, { credentials: 'include', signal: controller.signal })
        .then(async (response) => {
          if (!response.ok) throw new Error(`读取失败（HTTP ${response.status}）`)
          return response.text()
        })
        .then((body) => setText(body))
        .catch((err: unknown) => {
          if (err instanceof DOMException && err.name === 'AbortError') return
          setError(err instanceof Error ? err.message : '读取失败')
        })
        .finally(() => setLoading(false))
    }, 0)
    return () => {
      window.clearTimeout(timer)
      controller.abort()
    }
  }, [row, isText, tooBig, url])

  return (
    <Modal
      open={row !== null}
      onClose={onClose}
      title={row?.name ?? '预览'}
      titleAside={
        row ? (
          <span className="text-xs text-ink-muted">
            {formatBytes(row.sizeBytes)}
            {ext ? ` · ${ext}` : ''}
          </span>
        ) : null
      }
      widthClass="max-w-4xl"
      footer={
        row ? (
          <div className="flex w-full items-center justify-between gap-2">
            <span className="truncate font-mono text-xs text-ink-muted" title={row.path}>
              {row.path}
            </span>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => {
                window.location.href = url
              }}
            >
              <Download size={14} strokeWidth={2} aria-hidden="true" />
              下载
            </Button>
          </div>
        ) : null
      }
    >
      <div className="min-h-[180px]">
        {!row ? null : tooBig ? (
          <p className="text-sm text-ink-muted">
            文件 {formatBytes(row.sizeBytes)}，超过在线预览上限（
            {formatBytes(isImage ? PREVIEW_MAX_IMAGE_BYTES : PREVIEW_MAX_TEXT_BYTES)}），
            请下载后查看。
          </p>
        ) : isImage ? (
          <div className="flex max-h-[65vh] items-center justify-center overflow-auto rounded-sm bg-sunken p-2">
            <img src={url} alt={row.name} className="max-h-[62vh] max-w-full object-contain" />
          </div>
        ) : isText ? (
          loading ? (
            <p className="text-sm text-ink-muted">读取中…</p>
          ) : error ? (
            <p className="text-sm text-signal-risk">{error}</p>
          ) : (
            <pre className="max-h-[65vh] overflow-auto whitespace-pre-wrap break-words rounded-sm bg-sunken p-3 font-mono text-xs leading-relaxed text-ink">
              {text ?? ''}
            </pre>
          )
        ) : (
          <p className="text-sm text-ink-muted">
            这个类型不支持在线预览{ext ? `（.${ext}）` : ''}，请下载后查看。
          </p>
        )}
      </div>
    </Modal>
  )
}

function FileGroupBody({
  label,
  rows,
  selected,
  onToggle,
  onPreview,
  onCopy,
  onCopySession,
  onDelete,
}: {
  label: string
  rows: WorkspaceFileRow[]
  selected: ReadonlySet<string>
  onToggle: (row: WorkspaceFileRow) => void
  onPreview: (row: WorkspaceFileRow) => void
  onCopy: (row: WorkspaceFileRow) => void
  onCopySession: (row: WorkspaceFileRow) => void
  onDelete: (row: WorkspaceFileRow) => void
}) {
  return (
    <>
      {label && (
        <tr className="bg-surface-raised">
          <td colSpan={7} className="px-3 py-2 text-xs font-medium text-ink-muted">
            {label} · {rows.length}
          </td>
        </tr>
      )}
      {rows.map((row) => {
        const key = fileKeyOf(row)
        const isSelected = selected.has(key)
        const title = (row.sessionTitle ?? '').trim()
        // 会话标题常是首条消息（可能是 JSON）；这种标题当主行既难看又撑宽。
        const titleIsJson = title.startsWith('{') || title.startsWith('[')
        const shortId = shortSessionId(row.sessionId)
        const primary = !title || titleIsJson ? shortId : title
        return (
          <tr
            key={key}
            className={cn(
              'border-b border-line last:border-0 hover:bg-surface-raised',
              isSelected && 'bg-accent-wash/30',
            )}
          >
            <td className="px-3 py-2">
              <Checkbox
                id={`files-pick-${key.replace(/[^a-zA-Z0-9_-]/g, '_')}`}
                checked={isSelected}
                onChange={() => onToggle(row)}
                label={`选择 ${row.name}`}
              />
            </td>
            <td className="max-w-[200px] px-3 py-2">
              <div className="flex flex-col leading-tight">
                <span className="truncate text-ink" title={title || row.sessionId}>
                  {primary}
                </span>
                {/* 标题本身是短 ID 时不重复；是正常标题时副行给可复制的短 ID。 */}
                {primary !== shortId && (
                  <span className="truncate font-mono text-2xs text-ink-muted" title={row.sessionId}>
                    {shortId}
                  </span>
                )}
              </div>
            </td>
            <td
              className="max-w-[160px] truncate px-3 py-2 text-ink-muted"
              title={subPathOf(row.path)}
            >
              {subPathOf(row.path)}
            </td>
            <td className="max-w-[240px] truncate px-3 py-2">
              <a
                href={workspaceFileUrl(row.sessionId, row.path)}
                className="text-ink hover:text-accent hover:underline"
                title={`下载 ${row.path}`}
              >
                {row.name}
              </a>
            </td>
            <td className="whitespace-nowrap px-3 py-2">
              <span className="rounded-xs border border-line px-1.5 py-0.5 text-xs text-ink-muted">
                {row.source === 'workspace' ? '工作目录' : row.source}
              </span>
            </td>
            <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums text-ink-muted">
              {formatBytes(row.sizeBytes)}
            </td>
            <td className="whitespace-nowrap px-3 py-2 text-ink-muted">
              {formatTimestamp(row.modifiedAt)}
            </td>
            <td className="whitespace-nowrap px-3 py-2">
              <div className="flex items-center justify-end gap-1">
                <IconButton label={`预览 ${row.name}`} onClick={() => onPreview(row)}>
                  <Eye size={15} strokeWidth={1.75} aria-hidden="true" />
                </IconButton>
                <IconButton label={`复制 ${row.name} 的路径`} onClick={() => onCopy(row)}>
                  <Copy size={15} strokeWidth={1.75} aria-hidden="true" />
                </IconButton>
                <IconButton label={`复制会话 ID ${row.sessionId}`} onClick={() => onCopySession(row)}>
                  <Hash size={15} strokeWidth={1.75} aria-hidden="true" />
                </IconButton>
                <IconButton
                  label={`下载 ${row.name}`}
                  onClick={() => {
                    window.location.href = workspaceFileUrl(row.sessionId, row.path)
                  }}
                >
                  <Download size={15} strokeWidth={1.75} aria-hidden="true" />
                </IconButton>
                <IconButton label={`删除 ${row.name}`} onClick={() => onDelete(row)}>
                  <Trash2 size={15} strokeWidth={1.75} aria-hidden="true" />
                </IconButton>
              </div>
            </td>
          </tr>
        )
      })}
    </>
  )
}
