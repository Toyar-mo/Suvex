import {
  Copy,
  Download,
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
import EmptyState from '@/components/ui/EmptyState'
import ErrorState from '@/components/ui/ErrorState'
import { Button, IconButton } from '@/components/ui/Button'
import { SkeletonRows } from '@/components/ui/Skeleton'
import { cn } from '@/lib/cn'
import { toApiError, type ApiError } from '@/lib/api'
import { PERSIST_PREFIX, workspaceFileUrl } from '@/lib/workFiles'
import { showToast } from '@/data/toast-store'
import {
  deleteFile,
  formatBytes,
  formatDate,
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
 * 日期 / 会话 / 子路径 / 文件名 / 来源 / 大小 / 修改时间 / 操作。
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

/** 会话 ID 太长，列表里截短显示；完整值放在 title 与「复制会话 ID」按钮里。 */
function shortSessionId(id: string): string {
  // chat_01M6ABC... 这种前缀 + 随机段；保留头 12 位 + 尾 4 位即可辨认同一个。
  if (id.length <= 18) return id
  return `${id.slice(0, 12)}…${id.slice(-4)}`
}

export default function FilesPage() {
  const [rows, setRows] = useState<WorkspaceFileRow[]>([])
  const [status, setStatus] = useState<FilesStatus>('loading')
  const [error, setError] = useState<ApiError | null>(null)
  const [draft, setDraft] = useState<FilterDraft>(EMPTY_DRAFT)
  const [applied, setApplied] = useState<FileFilters>({})
  const [groupBy, setGroupBy] = useState<GroupBy>('none')
  const [pendingDelete, setPendingDelete] = useState<WorkspaceFileRow | null>(null)
  const [deleting, setDeleting] = useState(false)
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  const [uploading, setUploading] = useState(false)
  // v1.39：上传目标会话改成下拉选（会话 ID 不在这一页上，让用户手填看不到的东西是设计缺陷）。
  const [uploadSessions, setUploadSessions] = useState<UploadSessionOption[]>([])
  const [uploadTarget, setUploadTarget] = useState('')

  // 拉一次可选会话。失败不致命：上传按钮会提示"先选会话"（v1.40 起上传目标
  // 只认下拉里的真实会话，不再回落到筛选栏的自由文本，见 resolveUploadTarget）。
  useEffect(() => {
    let alive = true
    void listUploadSessions()
      .then((items) => {
        if (!alive) return
        setUploadSessions(items)
      })
      .catch(() => {
        /* 吞掉：会话列表拿不到不该挡住「浏览/下载/删除」这些只读能力 */
      })
    return () => {
      alive = false
    }
  }, [])

  const load = useCallback(async (filters: FileFilters) => {
    setStatus('loading')
    setError(null)
    try {
      const data = await listFiles(filters)
      setRows(data)
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

  const search = () => setApplied({ ...draft })
  const refresh = () => void load(applied)

  /** 分组后的渲染单元。空组不渲染。 */
  const groups = useMemo(() => {
    if (groupBy === 'none') return [{ key: 'all', label: '', rows }]
    const map = new Map<string, { key: string; label: string; rows: WorkspaceFileRow[] }>()
    for (const row of rows) {
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
  }, [rows, groupBy])

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
    if (rows.length === 0) {
      showToast({ tone: 'info', message: '当前没有可导出的结果。' })
      return
    }
    const header = ['日期', '会话', '项目', '子路径', '文件名', '来源', '大小(字节)', '修改时间']
    const lines = rows.map((row) =>
      [
        formatDate(row.modifiedAt),
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
              onChange={(e) => setGroupBy(e.target.value as GroupBy)}
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
            <div className="ml-auto flex items-center gap-2">
              <label className="flex items-center gap-1.5 text-sm">
                <span className="whitespace-nowrap text-ink-muted">上传到会话</span>
                <select
                  value={uploadTarget}
                  onChange={(e) => setUploadTarget(e.target.value)}
                  disabled={uploading || uploadSessions.length === 0}
                  title={
                    uploadSessions.length === 0
                      ? '还没有能上传的会话，先去对话页开一个'
                      : undefined
                  }
                  className={cn(INPUT_CLASS, 'w-auto max-w-[240px]')}
                  data-testid="upload-target"
                >
                  <option value="">
                    {uploadSessions.length === 0 ? '（没有会话）' : '请选择会话…'}
                  </option>
                  {uploadSessions.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.title || '(无标题)'} · {s.id}
                    </option>
                  ))}
                </select>
              </label>
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
            <span>共 {rows.length} 个文件</span>
            {status === 'loading' && <span>加载中…</span>}
          </div>

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
                    <th className="px-3 py-2 font-medium">日期</th>
                    <th className="px-3 py-2 font-medium">会话</th>
                    <th className="px-3 py-2 font-medium">子路径</th>
                    <th className="px-3 py-2 font-medium">文件名</th>
                    <th className="px-3 py-2 font-medium">来源</th>
                    <th className="px-3 py-2 text-right font-medium">大小</th>
                    <th className="px-3 py-2 font-medium">修改时间</th>
                    <th className="px-3 py-2 text-right font-medium">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {groups.map((group) => (
                    <FileGroupBody
                      key={group.key}
                      label={group.label}
                      rows={group.rows}
                      onCopy={copyPath}
                      onCopySession={copySessionId}
                      onDelete={setPendingDelete}
                    />
                  ))}
                </tbody>
              </table>
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
    </div>
  )
}

/** 一个分组（平铺时只有一个，label 为空）。 */
function FileGroupBody({
  label,
  rows,
  onCopy,
  onCopySession,
  onDelete,
}: {
  label: string
  rows: WorkspaceFileRow[]
  onCopy: (row: WorkspaceFileRow) => void
  onCopySession: (row: WorkspaceFileRow) => void
  onDelete: (row: WorkspaceFileRow) => void
}) {
  return (
    <>
      {label && (
        <tr className="bg-surface-raised">
          <td colSpan={8} className="px-3 py-2 text-xs font-medium text-ink-muted">
            {label} · {rows.length}
          </td>
        </tr>
      )}
      {rows.map((row) => (
        <tr
          key={`${row.sessionId}:${row.path}`}
          className="border-b border-line last:border-0 hover:bg-surface-raised"
        >
          <td className="whitespace-nowrap px-3 py-2 text-ink-muted">{formatDate(row.modifiedAt)}</td>
          <td className="max-w-[220px] px-3 py-2">
            <div className="flex flex-col leading-tight">
              <span className="truncate text-ink" title={row.sessionTitle}>
                {row.sessionTitle || '(无标题)'}
              </span>
              {/* v1.39：会话 ID 直接显示在页面上、可一键复制 —— 之前这一页根本查不到它，
                  「上传文件」却要求用户填会话 ID，等于让人填一个看不到的值。 */}
              <span className="truncate font-mono text-[11px] text-ink-muted" title={row.sessionId}>
                {shortSessionId(row.sessionId)}
              </span>
            </div>
          </td>
          <td className="max-w-[160px] truncate px-3 py-2 text-ink-muted" title={subPathOf(row.path)}>
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
      ))}
    </>
  )
}
