import { cn } from '@/lib/cn'
import { PageHeader } from '@/components/layout/AppShell'
import BulkActionBar from '@/components/scans/BulkActionBar'
import NewScanWizard from '@/components/scans/NewScanWizard'
import ScanList from '@/components/scans/ScanList'
import CompareDialog from '@/components/scans/CompareDialog'
import WriteupDialog from '@/components/scans/WriteupDialog'
import ScanViewsMenu from '@/components/scans/ScanViewsMenu'
import ConfirmDialog from '@/components/ui/ConfirmDialog'
import { Button, IconButton } from '@/components/ui/Button'
import ToggleChip from '@/components/ui/ToggleChip'
import { SORT_LABEL, filterScans, sortScans, type SortDir, type SortKey } from '@/lib/scan-query'
import { encodeViewQuery, type ScanViewParams } from '@/lib/scan-view-query'
import ErrorState from '@/components/ui/ErrorState'
import { SkeletonRows } from '@/components/ui/Skeleton'
import {
  SCAN_AUTHORIZATION_STATEMENT,
  SCAN_STATUS_LABEL,
  totalFindings,
  type Scan,
  type ScanStatus,
} from '@/data/scan'
import {
  abortScan,
  batchArchive,
  batchDelete,
  batchRerun,
  batchRestore,
  hasActiveScans,
  isViewFresh,
  loadScans,
  purgeScan,
  rerunScan,
  resumeScan,
  useScanView,
  useScansState,
  type ScanView,
} from '@/data/scans-store'
import { showToast } from '@/data/toast-store'
import { errorMessage } from '@/lib/api'
import { downloadCsv, exportFilename, scansToCsv } from '@/lib/scan-export'
import {
  Archive,
  ArrowDownWideNarrow,
  CheckCircle2,
  Plus,
  ScanSearch,
  Trash2,
  X,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

/**
 * Scans 扫描任务页。
 *
 * 结构：页面头（标题 + 主操作）→ 工具条（视图/状态筛选 + 搜索 + 排序）→ 列表。
 *
 * 主操作「新建扫描」放在页面头右侧而不是悬浮按钮：
 * 悬浮按钮会盖住列表最后一行，而且这个页面没有"随时可点"的需求。
 *
 * 新建向导以**内联面板**呈现，不是模态弹窗：
 *   它是个多步流程，用户可能想中途去看一眼已有扫描列表做参考。
 *
 * ## 三个视图（任务 9）
 *
 * `全部/运行中/…` 是**状态筛选**，而 `已归档/回收站` 是**视图切换** ——
 * 两者不是一回事，但用户心里的模型是"一排 tab"。
 * 所以做成一条 chip 行、中间用一条竖线分组：
 *
 * * 点状态 chip → 回到默认视图 + 按该状态筛；
 * * 点已归档/回收站 → 切视图（状态筛选重置为"全部"）。
 */

const STATUS_FILTERS: { value: ScanStatus | 'all'; label: string }[] = [
  { value: 'all', label: '全部' },
  { value: 'running', label: SCAN_STATUS_LABEL.running },
  { value: 'queued', label: SCAN_STATUS_LABEL.queued },
  { value: 'completed', label: SCAN_STATUS_LABEL.completed },
  { value: 'failed', label: SCAN_STATUS_LABEL.failed },
  { value: 'aborted', label: SCAN_STATUS_LABEL.aborted },
]

const VIEW_TABS: { value: Exclude<ScanView, 'default'>; label: string; icon: typeof Archive }[] = [
  { value: 'archived', label: '已归档', icon: Archive },
  { value: 'deleted', label: '回收站', icon: Trash2 },
]

export function ScansPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  /*
    ⚠️ 这里原来有一个 `actionError` 状态，用来放"终止/重跑失败"的原因。
    它连同渲染它的那一行小字一起删掉了（2026-09-27）—— 原因见下面
    行内动作那段的说明：渲染在列表底部、字号 2xs，列表一长就看不见，
    结果是"点了没反应"。现在这类失败走 toast，与批量动作一致。
  */

  const [view, setView] = useState<ScanView>('default')
  const [statusFilter, setStatusFilter] = useState<ScanStatus | 'all'>('all')
  const [sortKey, setSortKey] = useState<SortKey>('recent')
  const [sortDir, setSortDir] = useState<SortDir>('desc')

  // ── 批量选择（任务 9）──
  //
  // 用 `Set` 不用数组：判定 `has` 是 O(1)，而列表会长到几百条，
  // 每次渲染对每一行做一次 `Array.includes` 是 O(n²)。
  const [selectedIds, setSelectedIds] = useState<ReadonlySet<string>>(new Set())
  /** 批量请求进行中 —— 期间禁用整栏，避免连点两次发出两份请求 */
  const [bulkBusy, setBulkBusy] = useState(false)
  /** 二次确认：默认视图/已归档是"移入回收站"，回收站里是"永久删除" */
  const [confirm, setConfirm] = useState<null | 'delete' | 'purge'>(null)
  /** 待对比的两条 id（恰好选 2 条时可用） */
  const [compareIds, setCompareIds] = useState<[string, string] | null>(null)
  /**
   * 正在看题解的那次扫描（CTF 夺旗才有）。
   *
   * ★ 存的是**整个 scan 对象**，不是 id。
   *
   * 弹窗头部要显示题目名，而题目名在 `name`/`target` 里 —— 只存 id 的话，
   * 弹窗还得回列表里反查一次；而列表是 2.5 秒轮询整体替换的，
   * 那条记录**可能已经不在当前视图里了**（用户一边看题解一边把它归档了），
   * 反查会得到 undefined，于是标题变成空白。
   *
   * ⚠️ 顺带说明一个刻意的选择：这里拿的是**打开那一刻的快照**，
   *    即使列表随后刷新、这条记录的状态变了，弹窗标题也不会跟着变 ——
   *    题解属于"那次运行的结果"，它的名字就该是当时的那个。
   */
  const [writeupScan, setWriteupScan] = useState<Scan | null>(null)
  /**
   * 键盘焦点行的下标（j/k 移动）。
   *
   * `-1` = 还没有焦点。用**下标**而不是 id：j/k 是"上一条/下一条"，
   * 而列表的顺序会随排序与筛选变化 —— 存 id 的话移动时还要反查它在哪，
   * 而那个反查在"筛掉了我正看着的那条"时会失败。
   */
  const [focusIndex, setFocusIndex] = useState(-1)

  /*
    ★ 三个视图各自的槽（见 `scans-store` 的分槽说明）。

    默认视图的槽是**共享**的（侧栏、总览都读它）；另外两个只属于这一页。
    这样切到回收站不会把侧栏的"运行中 N"带偏。
  */
  const defaultState = useScansState()
  const archivedState = useScanView('archived')
  const deletedState = useScanView('deleted')
  const active =
    view === 'default' ? defaultState : view === 'archived' ? archivedState : deletedState
  const { scans, status, error } = active

  // 新建完成后高亮新记录，让用户明确看到"它加进去了"（验收要求）
  const justCreatedId = searchParams.get('created')

  /*
    ── 外部入口（命令面板 / 资产页 / 链接）传进来的意图 ──

    两个参数，都只**消费一次**：读完就把参数从地址栏抹掉。

    ★ 为什么必须抹掉：`?new=1` 如果留在地址栏里，用户关掉向导、
      刷新页面、或者从别的页面返回，向导会再次弹出来 —— 那时
      他已经不记得自己点过什么，只觉得"这玩意儿自己弹"。
      抹掉之后地址栏恢复干净，副作用不再二次触发。

    ── 为什么是 draft 而不是 effect 里 setState ──

    ★ 需求本身是："用户**已经在这一页**、又从命令面板点了一次
      '发起新扫描'，向导要再弹一次" —— 所以不能只在 `useState`
      初值里读（初值只在首挂载算一次，组件不重挂载就永远不重算）。

    ★ 但也**不能**在 effect 里 `setWizardOpen(true)`：那是
      `react-hooks/set-state-in-effect`，会多一轮渲染；更麻烦的是它
      让"向导开没开"有两份需要手动对齐的真相（URL 一份、state 一份），
      而它们永远差一帧 —— 比如参数刚被抹掉、effect 还没跑完的那一帧。

    ★ 解法与 `SchedulesPage`、`ScanRetentionPanel` 同一个模式：
        · 没被用户碰过 → 由 URL 派生（`?new=1` 就是开着）
        · 用户点了「发起扫描」或关掉 → `draft` 接管
      于是"从命令面板跳进来"和"用户自己开关"是**同一个表达式**算出来的，
      没有第二条路径需要维护。`targetQuery` 同理：URL 带了目标就是初值，
      用户一打字 `draft` 接管。
  */
  const [wizardOpenDraft, setWizardOpenDraft] = useState<boolean | null>(null)
  const wizardOpen = wizardOpenDraft ?? searchParams.get('new') === '1'
  const setWizardOpen = setWizardOpenDraft

  const urlTarget = searchParams.get('target')
  const [targetQueryDraft, setTargetQueryDraft] = useState<string | null>(null)
  const targetQuery = targetQueryDraft ?? (urlTarget ?? '')
  const setTargetQuery = setTargetQueryDraft

  /*
    ── 把消费掉的参数从地址栏抹掉 ──

    ★ 这里只有导航（`setSearchParams`），没有 setState ——
      上面两个开关已经由派生的量表达，不需要在 effect 里纠正。
  */
  useEffect(() => {
    const wantsNew = searchParams.get('new') === '1'
    const targetFromUrl = searchParams.get('target')
    if (!wantsNew && targetFromUrl === null) return

    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev)
        next.delete('new')
        next.delete('target')
        return next
      },
      { replace: true },
    )
  }, [searchParams, setSearchParams])

  /*
    按需加载：进页面、以及每次切换视图时都走这里。

    ★ 判据是「**这份数据还能直接用吗**」，而不是「加载过吗」。
    批量操作会让记录**跨越**视图（在默认视图归档一条，它就进了归档视图），
    对侧那个槽数据还在、但其实已经过期。所以看 `isViewFresh`（`ready` 且不脏）。

    新鲜 → **silent**：每次切 tab 都闪一次骨架会让人以为页面在重新加载，
    而它其实只是换了个筛选条件。
    不新鲜 → **非 silent**：骨架比"先给一份已知过期的列表"更诚实。

    ⚠️ 这里**不能**像原来那样对 `default` 直接 return。恢复一条扫描会让
    默认槽变脏，而用户切回去的那一刻正需要看到它 —— 早退就会把一条已经
    恢复的记录藏起来，直到他手动刷新。

    mount 时 `view` 是 `default`、槽初值是 `loading`（不是 fresh），
    所以"进页面拉一次"也由这里兜住，不必另写一个 effect。
  */
  useEffect(() => {
    void loadScans({ view, silent: isViewFresh(view) })
  }, [view])

  /**
   * ★ 轮询**只对默认视图**开。
   *
   * 归档与回收站里的记录都不会自己变化（归档不改变 status，
   * 而回收站里的更是已经删了）—— 为它们轮询是白烧请求。
   */
  const hasActive = useMemo(() => hasActiveScans(scans), [scans])

  useEffect(() => {
    if (view !== 'default' || !hasActive) return
    const timer = window.setInterval(() => {
      // silent：轮询刷新不能把页面打回骨架态，失败也不该把列表换成错误页
      void loadScans({ silent: true })
    }, 2500)
    return () => window.clearInterval(timer)
  }, [hasActive, view])

  const filtered = useMemo(
    () =>
      sortScans(filterScans(scans, { status: statusFilter, query: targetQuery }), sortKey, sortDir),
    [scans, statusFilter, targetQuery, sortKey, sortDir],
  )

  const focusedId = focusIndex >= 0 && focusIndex < filtered.length ? filtered[focusIndex].id : null

  const hasAnyScan = scans.length > 0
  const isFiltered = statusFilter !== 'all' || targetQuery.trim().length > 0

  /*
    ★ 只把**当前可见**的选中项算进去。

    用户选了 3 条之后改了筛选条件，那 3 条可能已经不在列表里了。
    如果仍然把它们算作"已选"，用户会看到"已选 3 项"但列表里一个勾都没有 ——
    而那时点"删除"，删掉的是他**看不见**的东西。
  */
  const selectedVisible = useMemo(
    () => filtered.filter((scan) => selectedIds.has(scan.id)),
    [filtered, selectedIds],
  )
  const selectedIdList = useMemo(() => selectedVisible.map((s) => s.id), [selectedVisible])

  const toggleRow = useCallback((id: string, next: boolean) => {
    setSelectedIds((prev) => {
      const copy = new Set(prev)
      if (next) copy.add(id)
      else copy.delete(id)
      return copy
    })
  }, [])

  /** 全选/取消全选**当前列表**（不是全部记录 —— 那会说谎） */
  const toggleAll = useCallback(
    (next: boolean) => {
      setSelectedIds((prev) => {
        const copy = new Set(prev)
        for (const scan of filtered) {
          if (next) copy.add(scan.id)
          else copy.delete(scan.id)
        }
        return copy
      })
    },
    [filtered],
  )

  /**
   * 切视图时清空选择。
   *
   * 放在事件里而不是 `useEffect` 里：一是项目禁掉了 effect 里的同步 setState
   * （会多渲染一轮），二是语义上它本来就属于"用户点了切 tab"这个动作。
   */
  const switchView = (next: ScanView) => {
    setView(next)
    setSelectedIds(new Set())
    setFocusIndex(-1)
    setStatusFilter('all')
  }

  const handleCreated = (scanId: string | null) => {
    setWizardOpen(false)
    if (scanId) {
      // 通过 URL 传"刚创建的 id"，这样刷新后高亮还在，也便于分享
      setSearchParams({ created: scanId }, { replace: true })
    }
  }

  /*
    当前筛选的规范化表示。**「我的视图」存的就是这个串**。

    它同时干两件事：判断"有没有东西可存"（空串不能存），
    以及判断"当前是不是正套着某条视图"（和 `view.query` 逐字符相等即命中）。
    用一个值表达两件事，就不会出现"按钮显示已套用、实际筛选已改"的不一致。
  */
  const currentViewQuery = useMemo(
    () => encodeViewQuery({ status: statusFilter, q: targetQuery, sort: sortKey, dir: sortDir }),
    [statusFilter, targetQuery, sortKey, sortDir],
  )

  /**
   * 套用一条视图。
   *
   * ★ **一律切回默认列表**（`view='default'`）。
   *
   * 视图存的是"筛选组合"，不含"我正在看回收站"这种位置信息。
   * 如果套用时保留当前位置，用户在回收站里点一条"失败扫描"的视图，
   * 会因为回收站里没有失败扫描而看到空列表 —— 他会以为视图坏了。
   * 视图的语义是"在扫描列表里怎么筛"，所以套用它就先回到扫描列表。
   */
  const handleApplyView = useCallback(
    (params: ScanViewParams) => {
      setView('default')
      setStatusFilter(params.status)
      setTargetQuery(params.q)
      setSortKey(params.sort)
      setSortDir(params.dir)
      setSelectedIds(new Set())
      setFocusIndex(-1)
    },
    // `setTargetQuery` 现在是 `useState` 的 setter 本体（`setTargetQueryDraft`
    // 的别名），引用永远稳定；但它已经不在组件体内联声明，ESLint 看不到
    // 那个稳定性，所以显式列出来。
    [setTargetQuery],
  )

  // ── 批量动作 ─────────────────────────────────────────
  //
  // 共同的三条：
  //   1. **立即执行**，然后用 toast 给一个 5 秒的撤销窗口（归档不必先问一遍）；
  //   2. **成功/失败都清空选择** —— 失败时留着选择会让用户以为"还没执行"，
  //      而实际上可能已经部分生效了；
  //   3. 失败用 `tone: 'risk'`，与"已完成"区分开。
  //
  // ★ 4. **撤销的 `view` 参数指向"记录要回到的那个视图"**，不是"操作发起时的视图"。
  //
  // `runBatch` 的判据是"这次操作之后，记录还在不在我传进去的这个视图里"：
  // 在 → 乐观移除；回来 → 等服务器然后静默重拉。撤销让记录**回到**视图，
  // 所以规律很整齐：**撤销的 view == 正向操作的 view**。
  //
  // 传反了的后果特别隐蔽：不报错，后端也确实改对了，只是本地**不重拉** ——
  // 那条记录就这么从眼前消失了，直到用户手动刷新才回来。四个撤销里传错
  // 任意一个都会得到这个症状。

  const runBulk = (
    ids: string[],
    action: (ids: string[]) => Promise<unknown>,
    messages: { done: string; failed: string },
    undo?: () => Promise<unknown> | void,
  ) => {
    setBulkBusy(true)
    void Promise.resolve(action(ids))
      .then(() => {
        setSelectedIds(new Set())
        showToast({
          message: messages.done,
          action: undo
            ? {
                label: '撤销',
                onAction: () => {
                  void Promise.resolve(undo()).catch((err) =>
                    showToast({ message: `撤销失败：${errorMessage(err)}`, tone: 'risk' }),
                  )
                },
              }
            : undefined,
        })
      })
      .catch((err) =>
        showToast({ message: `${messages.failed}：${errorMessage(err)}`, tone: 'risk' }),
      )
      .finally(() => setBulkBusy(false))
  }

  const bulkArchive = () => {
    const ids = selectedIdList
    if (ids.length === 0) return
    // 归档后它们从这里消失 —— 留在默认视图才能看到那个结果
    setView('default')
    runBulk(
      ids,
      (list) => batchArchive(list, true, 'default'),
      { done: `已归档 ${ids.length} 项`, failed: '归档失败' },
      () => batchArchive(ids, false, 'default'),
    )
  }

  const bulkUnarchive = () => {
    const ids = selectedIdList
    if (ids.length === 0) return
    runBulk(
      ids,
      (list) => batchArchive(list, false, 'archived'),
      { done: `已取消归档 ${ids.length} 项`, failed: '取消归档失败' },
      () => batchArchive(ids, true, 'archived'),
    )
  }

  const bulkDelete = () => {
    const ids = selectedIdList
    if (ids.length === 0) return
    setConfirm(null)
    runBulk(
      ids,
      (list) => batchDelete(list, view),
      { done: `已移入回收站 ${ids.length} 项`, failed: '删除失败' },
      () => batchRestore(ids, 'default'),
    )
  }

  const bulkRestore = () => {
    const ids = selectedIdList
    if (ids.length === 0) return
    runBulk(
      ids,
      (list) => batchRestore(list, 'deleted'),
      { done: `已恢复 ${ids.length} 项`, failed: '恢复失败' },
      () => batchDelete(ids, 'deleted'),
    )
  }

  const bulkPurge = () => {
    const ids = selectedIdList
    if (ids.length === 0) return
    setConfirm(null)
    setBulkBusy(true)
    /*
      逐条删、逐条收集失败 —— **不用 `Promise.all`**。
      一条被报告引用挡住（409）不该让其余的全军覆没，
      而 `all` 会在第一个 reject 时丢掉其余的结果。
    */
    void Promise.allSettled(ids.map((id) => purgeScan(id, 'deleted')))
      .then((results) => {
        const failed = results.filter((r) => r.status === 'rejected')
        const okCount = results.length - failed.length
        setSelectedIds(new Set())
        if (failed.length === 0) {
          showToast({ message: `已永久删除 ${okCount} 项` })
          return
        }
        // 把后端那句中文原样带出来 —— 它指名了是哪份报告挡住的，
        // 而那是用户唯一能采取行动的线索。
        const first = (failed[0] as PromiseRejectedResult).reason
        showToast({
          message: `已删除 ${okCount} 项，${failed.length} 项被拒绝：${errorMessage(first)}`,
          tone: 'risk',
          // 这段文字通常比 5 秒能读完的长，给多一点时间
          durationMs: 9000,
        })
      })
      .finally(() => setBulkBusy(false))
  }

  const bulkRerun = () => {
    const ids = selectedIdList
    if (ids.length === 0) return
    setBulkBusy(true)
    void batchRerun(ids, { confirmed: true, statement: SCAN_AUTHORIZATION_STATEMENT })
      .then((result) => {
        setSelectedIds(new Set())
        const parts = [`已重新发起 ${result.created.length} 项`]
        if (result.failed.length > 0) parts.push(`${result.failed.length} 项失败`)
        showToast({
          message: parts.join(' · '),
          // 有失败时用 risk 色 —— 只说"成功 N 项"会掩盖掉那几条没起来的
          tone: result.failed.length > 0 ? 'risk' : 'ok',
        })
      })
      .catch((err) => showToast({ message: `重新扫描失败：${errorMessage(err)}`, tone: 'risk' }))
      .finally(() => setBulkBusy(false))
  }

  const bulkExport = () => {
    if (selectedVisible.length === 0) return
    // 前端生成，不新增后端端点 —— 理由见 lib/scan-export.ts
    downloadCsv(exportFilename(), scansToCsv(selectedVisible))
    showToast({ message: `已导出 ${selectedVisible.length} 项为 CSV` })
  }

  /*
    ── 键盘快捷键（任务 9，需求里标为"可选"）──

    | 键 | 作用 |
    | --- | --- |
    | `j` / `k` | 焦点下移 / 上移 |
    | `x` | 切换焦点行的选中 |
    | `a` | 归档选中项 |
    | `Esc` | 清空焦点与选择 |

    ⚠️ **必须在输入框里让路**：`/` 搜索框里打字时按 `j` 是要打字母 j，
    不是移动焦点。判据是 `event.target` 是不是可编辑元素 ——
    少了这一步，搜索"jquery"会变成"焦点跑掉 + 打出 uery"。

    ## ★ 这个 effect 的依赖问题（2026-10 修）

    原来它**没有依赖数组**，于是每次渲染都解绑再重绑一次 `document` 监听。
    而这一页有 2.5 秒轮询（`loadScans({ silent: true })` 每次都 commit 新快照）
    → 每 2.5 秒至少重绑一次，改个搜索词、勾一行更是每帧都绑。
    功能一直是对的（重绑的监听器闭包总是最新），纯粹是白烧。

    ### 先把它读到的东西列全（漏一个就是快捷键失灵，比性能问题严重）

    | 读的东西 | 谁提供的 | 变不变 |
    | --- | --- | --- |
    | `filtered` | `useMemo`（列表 + 筛选 + 排序） | 数据变了才变 |
    | `focusedId` | 由 `focusIndex` 与 `filtered` 派生 | j/k 每按一次就变 |
    | `selectedIds` | `useState` | 每次勾选都变 |
    | `selectedIdList` | `useMemo`（可见选中项） | 随上面两个变 |
    | `toggleRow` | `useCallback([])` | **稳定** |
    | `bulkArchive` | 普通函数，**每次渲染新建** | 每次都变 |

    ### 为什么最终选了「latest ref」而不是补依赖数组

    把上面这些原样填进依赖数组是**没用的**：`bulkArchive` 每次渲染都是新函数
    （它没有 `useCallback`），依赖数组里只要有它，effect 照样每帧重跑 ——
    等于什么都没改。要真修就得把 `bulkArchive` 连同它的依赖链
    （`runBulk` → `selectedIdList` → `filtered` …）整条 `useCallback` 化，
    那是一次改动面大得多的重构，而且**任何一环漏了 `useCallback` 就静默退回
    每次重绑** —— 是个"看起来修好了、其实没修"的陷阱。

    所以用 latest ref：监听器**只绑一次**，处理器每次渲染后刷新到 ref 里，
    闭包永远是最新那一帧的。这与项目里已有的两处是同一个模式
    （`useAutoScroll` 的 `onUserAwayRef`、`FlagConfirmDialog` 的 `onCancelRef`），
    不是这里新发明的写法。

    ⚠️ 写 ref 必须放在 **effect 里**，不能在渲染期直接
      `keyHandlerRef.current = onKeyDown` —— 那会撞 `react-hooks/refs`
      （渲染期不得读写 ref，见 `CompetitionPicker` 里记的那次实测）。
  */

  /**
   * 键盘处理器。
   *
   * 每次渲染重建，闭包里读到的是**这一帧**的 `filtered` / `focusedId` /
   * `selectedIds` —— 这正是下面 ref 要的效果。
   */
  const onKeyDown = (event: KeyboardEvent) => {
    /*
      ★ 用 `instanceof HTMLElement` 守卫，不要写 `target?.tagName.toLowerCase()`。

      后者有个真实的崩溃面：事件目标是 `document` 或 `window` 时，`target?.tagName`
      得到 `undefined`（`?.` 只挡住 target 为 null，挡不住「target 存在但没有 tagName」），
      紧接着的 `.toLowerCase()` 直接抛 TypeError。抛在 keydown 处理器里 = 这一轮
      所有分支静默失效，而且不冒泡到任何错误边界，排查时看起来像"快捷键根本没绑上"。
    */
    const target = event.target
    if (target instanceof HTMLElement) {
      const tag = target.tagName.toLowerCase()
      if (tag === 'input' || tag === 'textarea' || tag === 'select' || target.isContentEditable) {
        return
      }
    }
    // 带修饰键的一律不管：那是浏览器的快捷键（Ctrl+F 等）
    if (event.metaKey || event.ctrlKey || event.altKey) return

    switch (event.key) {
      case 'j':
        event.preventDefault()
        setFocusIndex((i) => Math.min(i + 1, filtered.length - 1))
        break
      case 'k':
        event.preventDefault()
        setFocusIndex((i) => Math.max(i - 1, 0))
        break
      case 'x': {
        if (!focusedId) return
        event.preventDefault()
        const scan = filtered.find((s) => s.id === focusedId)
        if (scan) toggleRow(scan.id, !selectedIds.has(scan.id))
        break
      }
      case 'a': {
        if (selectedIdList.length === 0) return
        event.preventDefault()
        bulkArchive()
        break
      }
      case 'Escape':
        setFocusIndex(-1)
        setSelectedIds(new Set())
        break
    }
  }

  /** 处理器的最新一份。初值用首次渲染的那个，之后由下面的 effect 刷新。 */
  const keyHandlerRef = useRef(onKeyDown)

  /*
    刷新 ref —— **故意不写依赖数组**。

    它每次渲染后跑一遍，但那只是"把一个函数引用赋给 ref"，
    比解绑/重绑 `document` 监听轻得多（那才是原来的浪费）。
    写依赖数组反而要枚举上面那张表，而表里有一项每次都变。
  */
  useEffect(() => {
    keyHandlerRef.current = onKeyDown
  })

  /*
    真正的绑定 —— **空依赖，只绑一次**。

    转发一层 `keyHandlerRef.current(...)` 而不是直接把 `onKeyDown` 绑上去：
    后者会把**首次渲染那个闭包**永久留在监听器上，j/k 就会一直拿着最初的
    `filtered`（空列表）算下标 —— 那正是"漏依赖导致快捷键失灵"的形态。
    多这一层间接，换的是"绑定次数"与"闭包新鲜度"解耦。
  */
  useEffect(() => {
    const listener = (event: KeyboardEvent) => keyHandlerRef.current(event)
    document.addEventListener('keydown', listener)
    return () => document.removeEventListener('keydown', listener)
  }, [])


  return (
    <>
      <PageHeader navId="scans" />

      <div className="px-gutter py-bay">
        {/* ── 工具条 ── */}
        <div className="mb-panel flex flex-wrap items-center gap-tight">
          <div role="group" aria-label="按状态筛选" className="flex flex-wrap items-center gap-1">
            {STATUS_FILTERS.map((filter) => {
              const count =
                filter.value === 'all'
                  ? scans.length
                  : scans.filter((s) => s.status === filter.value).length
              const activeChip = view === 'default' && statusFilter === filter.value

              return (
                <ToggleChip
                  key={filter.value}
                  active={activeChip}
                  onToggle={() => {
                    setView('default')
                    setStatusFilter(filter.value)
                  }}
                >
                  {filter.label}
                  <span
                    className={cn(
                      'tabular text-2xs',
                      activeChip ? 'text-accent-strong' : 'text-ink-faint',
                    )}
                    data-numeric
                  >
                    {count}
                  </span>
                </ToggleChip>
              )
            })}

            {/* 分组竖线：左边是状态筛选，右边是视图切换 —— 两类不是一回事 */}
            <span aria-hidden="true" className="mx-1 h-5 w-px bg-line" />

            {VIEW_TABS.map((tab) => {
              const Icon = tab.icon
              const activeTab = view === tab.value
              return (
                <ToggleChip
                  key={tab.value}
                  active={activeTab}
                  onToggle={() => switchView(tab.value)}
                >
                  <Icon size={12} strokeWidth={1.75} aria-hidden="true" />
                  {tab.label}
                </ToggleChip>
              )
            })}
          </div>

          <div className="flex-1" />

          {/* 目标搜索 */}
          <div className="relative">
            <ScanSearch
              size={13}
              strokeWidth={1.75}
              className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-ink-ghost"
              aria-hidden="true"
            />
            <input
              type="search"
              value={targetQuery}
              onChange={(e) => setTargetQuery(e.target.value)}
              placeholder="筛选目标"
              aria-label="按目标筛选"
              className={cn(
                'h-8 w-40 rounded-sm border border-line bg-surface-sunken pl-7 pr-2 text-xs text-ink',
                'transition-ui duration-fast placeholder:text-ink-faint',
                'hover:border-line-strong focus:outline-none focus:ring-2 focus:ring-accent/40',
              )}
            />
          </div>

          {/* 排序 */}
          <label className="flex h-8 items-center gap-1.5 rounded-sm border border-line px-2 text-xs text-ink-muted">
            <ArrowDownWideNarrow size={13} strokeWidth={1.75} aria-hidden="true" />
            <span className="sr-only">排序方式</span>
            <select
              value={sortKey}
              onChange={(e) => setSortKey(e.target.value as SortKey)}
              className="bg-transparent text-xs text-ink-muted focus:outline-none"
            >
              {(Object.keys(SORT_LABEL) as SortKey[]).map((key) => (
                <option key={key} value={key} className="bg-surface-overlay">
                  {SORT_LABEL[key]}
                </option>
              ))}
            </select>
            <IconButton
              label={sortDir === 'desc' ? '当前降序，点击切换升序' : '当前升序，点击切换降序'}
              /* 触摸目标：箭头字形只有 8px 宽，撑到 24px 见方。
                 它是工具条里唯一的纯图标按钮，太小会误触。 */
              className="ml-0.5"
              onClick={() => setSortDir((d) => (d === 'desc' ? 'asc' : 'desc'))}
            >
              {sortDir === 'desc' ? '↓' : '↑'}
            </IconButton>
          </label>

          {/* 我的视图 —— 放在排序右边、新建扫描左边：它存的就是"这一排筛选控件"的状态 */}
          <ScanViewsMenu currentQuery={currentViewQuery} onApply={handleApplyView} />

          <Button variant="primary" size="sm" onClick={() => setWizardOpen(true)}>
            <Plus size={14} strokeWidth={2.5} aria-hidden="true" />
            新建扫描
          </Button>
        </div>

        {/* ── 批量操作栏（选中 ≥1 项时出现，sticky 跟随滚动） ── */}
        <BulkActionBar
          selectedCount={selectedIdList.length}
          view={view}
          busy={bulkBusy}
          onArchive={bulkArchive}
          onUnarchive={bulkUnarchive}
          onRerun={bulkRerun}
          onExport={bulkExport}
          canCompare={selectedIdList.length === 2}
          onCompare={() =>
            setCompareIds([selectedIdList[0], selectedIdList[1]] as [string, string])
          }
          onDelete={() => setConfirm('delete')}
          onRestore={bulkRestore}
          onPurge={() => setConfirm('purge')}
          onClear={() => setSelectedIds(new Set())}
        />

        {/* ── 二次确认。删除与永久删除的话术**必须不同** ── */}
        <ConfirmDialog
          open={confirm === 'delete'}
          title={`删除 ${selectedIdList.length} 次扫描？`}
          description="它们会被移入回收站，可以随时恢复；只有「永久删除」才会真正抹掉数据。保留天数在设置里可调。"
          confirmLabel="移入回收站"
          busy={bulkBusy}
          onConfirm={bulkDelete}
          onCancel={() => setConfirm(null)}
        />
        <ConfirmDialog
          open={confirm === 'purge'}
          title={`永久删除 ${selectedIdList.length} 次扫描？`}
          description="这一步不可撤销。相关的漏洞、事件与智能体记录会一并删除；若某份报告引用了它们，该条会被拒绝并告诉你具体是哪份报告。"
          confirmLabel="永久删除"
          busy={bulkBusy}
          onConfirm={bulkPurge}
          onCancel={() => setConfirm(null)}
        />

        {/* ── 对比弹窗（恰好选 2 条时可用） ── */}
        {compareIds && (
          <CompareDialog
            aId={compareIds[0]}
            bId={compareIds[1]}
            onClose={() => setCompareIds(null)}
          />
        )}

        {/*
          ── 题解弹窗（CTF 夺旗） ──

          ★ **条件挂载**，而不是常驻 + `open` 开关。

          每次打开都是一个全新实例：内部状态（加载中 / 拿到内容 / 失败）
          天然是干净的初始值，不需要在 effect 里重置 —— 而"重置"这件事
          一旦漏写，打开第二条扫描时会先闪一眼上一条的题解。
        */}
        {writeupScan && (
          <WriteupDialog scan={writeupScan} onClose={() => setWriteupScan(null)} />
        )}

        {/* ── 新建向导（内联面板） ── */}
        {wizardOpen && (
          <div className="mb-bay animate-stream-in">
            <NewScanWizard onDone={handleCreated} onCancel={() => setWizardOpen(false)} />
          </div>
        )}

        {/* ── 刚创建成功的提示 ── */}
        {justCreatedId && (
          <div className="mb-panel flex animate-rise-in items-center justify-between gap-panel rounded-sm border border-signal-ok/40 bg-signal-ok/[0.06] px-panel py-2.5">
            <p className="flex items-center gap-2 text-sm text-ink">
              <CheckCircle2
                size={15}
                strokeWidth={2}
                className="shrink-0 animate-settle-in text-signal-ok"
                aria-hidden="true"
              />
              <span>
                扫描已加入队列
                <span className="mono-ui ml-2 text-ink-muted">{justCreatedId}</span>
              </span>
            </p>
            <div className="flex items-center gap-tight">
              <Link
                to={`/activity?scan=${encodeURIComponent(justCreatedId)}`}
                className="text-xs text-accent-strong underline hover:text-accent"
              >
                查看活动
              </Link>
              <IconButton label="关闭提示" onClick={() => setSearchParams({}, { replace: true })}>
                <X size={13} strokeWidth={2} aria-hidden="true" />
              </IconButton>
            </div>
          </div>
        )}

        {/* ── 列表 ── */}
        {/*
          三态（任务 7 要求）。顺序很重要：
            · idle/loading → 骨架。**不能**先渲染空状态 —— 那会先显示
              "还没有扫描记录"，几十毫秒后再跳出一堆记录，用户看到的是
              一个闪过去的**假**空状态；
            · error   → 可重试的错误；
            · ready   → 真正的列表（它自己再区分各种空）。
        */}
        {status === 'idle' || status === 'loading' ? (
          <SkeletonRows rows={4} rowClassName="h-20" />
        ) : status === 'error' ? (
          <ErrorState
            title={view === 'default' ? '扫描列表加载失败' : '加载失败'}
            error={error}
            onRetry={() => void loadScans({ view })}
          />
        ) : (
          <div className="rounded-md border border-line bg-surface">
            <ScanList
              scans={filtered}
              selectedIds={selectedIds}
              focusedId={focusedId}
              onToggleRow={toggleRow}
              onToggleAll={toggleAll}
              /*
                ★ 题解（CTF 夺旗）：这里只负责"把哪一条交给弹窗"。

                列表**不预取**题解 —— 一屏可能有十几条夺旗记录，
                而用户只会点开其中一条；预取等于替他把十几份长文档拉下来。

                ⚠️ `onWriteup` 不传也不会出错（ScanList 里它是可选 prop，
                   整项不渲染）—— 别的调用点因此不受这次改动影响。
              */
              onWriteup={setWriteupScan}
              /*
                终止是幂等的：数据层会拒绝已完成扫描的终止请求。
                那种情况下列表状态本来就没变，不需要额外提示。
              */
              /*
                ★ 行内动作的失败一律用 **toast**（2026-09-27 实测反馈）。

                这三处（终止/重跑/续跑）原来写的是 `setActionError(...)`，
                而那个值渲染在**列表最下方**的一行 `text-2xs` 小字里 ——
                列表一长（十几条扫描就够）就被顶到屏幕外。

                于是用户的实际体验是：点了「续跑」，**什么都没发生**。
                而服务端其实老老实实回了 409 和一句写得挺清楚的中文
                （"只有「失败」或「已终止」的扫描可以续跑"）——
                它只是被渲染在了看不见的地方。查的时候从服务端日志里
                能看到三次 409，前端却像压根没发过请求一样。

                批量动作那边（见上面那段注释）本来就是 toast，
                所以这不只是"换个提示方式"，是**把行内动作拉回同一个约定**。
              */
              onAbort={(scan) => {
                void abortScan(scan.id).catch((err) =>
                  showToast({ message: `终止失败：${errorMessage(err)}`, tone: 'risk' }),
                )
              }}
              onRerun={(scan) => {
                // 重跑同样是"真的发包"，所以照样要带授权确认（契约 §5）
                void rerunScan(scan.id, {
                  confirmed: true,
                  statement: SCAN_AUTHORIZATION_STATEMENT,
                })
                  .then((created) => {
                    setView('default')
                    setSearchParams({ created: created.id }, { replace: true })
                  })
                  .catch((err) =>
                    showToast({ message: `重新扫描失败：${errorMessage(err)}`, tone: 'risk' }),
                  )
              }}
              onResume={(scan) => {
                // 续跑同样会**继续对真实目标发包** —— 授权确认不能省。
                void resumeScan(scan.id, {
                  confirmed: true,
                  statement: SCAN_AUTHORIZATION_STATEMENT,
                })
                  .then(() => {
                    /*
                      ⚠️ 与重跑不同：这里**不跳转**。

                      重跑新建了一条记录，跳过去看它是合理的；
                      续跑回来的是**列表里原本那一条**（状态回到排队中），
                      跳转会把用户从他正在看的列表上拽走 —— 而他要看的就是
                      那一行的状态变化。
                    */
                    setView('default')
                  })
                  .catch((err) =>
                    showToast({ message: `续跑失败：${errorMessage(err)}`, tone: 'risk' }),
                  )
              }}
              emptyState={
                view === 'archived' ? (
                  <EmptyHint
                    icon={Archive}
                    title="没有已归档的扫描"
                    body="归档把不再关注的记录从默认列表里移走 —— 数据不会删，随时可以取消归档。可在「设置 → 扫描保留规则」里让失败与已终止的扫描到期自动归档。"
                  />
                ) : view === 'deleted' ? (
                  <EmptyHint
                    icon={Trash2}
                    title="回收站是空的"
                    body="从列表里删除的扫描会先到这里，保留期满后才会被真正清除 —— 保留天数在「设置 → 扫描保留规则」里可调。期间随时可以恢复。"
                  />
                ) : isFiltered ? (
                  // 筛选后为空 —— 与"从来没跑过"是两回事，提示必须不同
                  <div className="flex flex-col items-center gap-tight text-center">
                    <p className="text-sm text-ink-muted">没有符合条件的扫描</p>
                    <Button
                      variant="link"
                      size="sm"
                      onClick={() => {
                        setStatusFilter('all')
                        setTargetQuery('')
                      }}
                    >
                      清除筛选条件
                    </Button>
                  </div>
                ) : !hasAnyScan ? (
                  <div className="flex flex-col items-center gap-tight text-center">
                    <ScanSearch
                      size={20}
                      strokeWidth={1.5}
                      className="text-ink-ghost"
                      aria-hidden="true"
                    />
                    <p className="text-sm text-ink-muted">还没有扫描记录</p>
                    <p className="max-w-[46ch] text-xs text-ink-faint">
                      给它一个目标，它会先摸清攻击面、寻找薄弱点，再逐一尝试用可复现的利用方式证明。
                    </p>
                    <Button variant="primary" size="lg" onClick={() => setWizardOpen(true)}>
                      <Plus size={15} strokeWidth={2.5} aria-hidden="true" />
                      发起第一次扫描
                    </Button>
                  </div>
                ) : (
                  <div className="flex flex-col items-center gap-tight text-center">
                    <p className="text-sm text-ink-muted">这个状态下没有扫描</p>
                    <Button variant="link" size="sm" onClick={() => setStatusFilter('all')}>
                      查看全部
                    </Button>
                  </div>
                )
              }
            />
          </div>
        )}

        {/* 统计脚注：告诉用户"看到的是筛选后的还是全部" */}
        {hasAnyScan && (
          <p className="mt-panel text-2xs text-ink-faint">
            {isFiltered ? (
              <>
                筛选出 {filtered.length} 条，共 {scans.length} 次扫描 · 累计发现{' '}
                {scans.reduce((sum, s) => sum + totalFindings(s.findings), 0)} 条漏洞
              </>
            ) : (
              <>
                共 {scans.length} 次扫描 · 累计发现{' '}
                {scans.reduce((sum, s) => sum + totalFindings(s.findings), 0)} 条漏洞
              </>
            )}
          </p>
        )}
      </div>
    </>
  )
}

/** 归档 / 回收站的空状态。与"没扫过"共用同一种排版，只是文案不同。 */
function EmptyHint({
  icon: Icon,
  title,
  body,
}: {
  icon: typeof Archive
  title: string
  body: string
}) {
  return (
    <div className="flex flex-col items-center gap-tight text-center">
      <Icon size={20} strokeWidth={1.5} className="text-ink-ghost" aria-hidden="true" />
      <p className="text-sm text-ink-muted">{title}</p>
      <p className="max-w-[52ch] text-xs text-ink-faint">{body}</p>
    </div>
  )
}

export default ScansPage
