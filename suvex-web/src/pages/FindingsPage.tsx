import { cn } from '@/lib/cn'
import { PageHeader } from '@/components/layout/AppShell'
import BulkActionBar from '@/components/findings/BulkActionBar'
import AssignDialog from '@/components/findings/AssignDialog'
import FindingDetail, { FindingTransitionBar } from '@/components/findings/FindingDetail'
import FindingsTable from '@/components/findings/FindingsTable'
import FindingsGroups from '@/components/findings/FindingsGroups'
import { SeverityBadge, StatusBadge, VerificationBadge } from '@/components/findings/FindingBadges'
import Drawer from '@/components/ui/Drawer'
import ConfirmDialog from '@/components/ui/ConfirmDialog'
import ErrorState from '@/components/ui/ErrorState'
import { Button, IconButton } from '@/components/ui/Button'
import ToggleChip from '@/components/ui/ToggleChip'
import NoScansYet from '@/components/ui/NoScansYet'
import ReasonDialog from '@/components/ui/ReasonDialog'
import { Skeleton, SkeletonRows, SkeletonText } from '@/components/ui/Skeleton'
import {
  FINDING_CATEGORY_LABEL,
  FINDING_STATUS_LABEL,
  FINDING_STATUS_ORDER,
  FINDING_STATUS_TRANSITIONS,
  type Finding,
  type FindingCategory,
  type FindingStatus,
} from '@/data/finding'
import { SEVERITY_META, SEVERITY_ORDER } from '@/data/findings'
import {
  assignFindings,
  changeFindingStatus,
  deleteFindings,
  findingsExportUrl,
  loadFindingDetail,
  loadFindingGroups,
  loadFindings,
  purgeFinding,
  refreshFindingDetail,
  restoreFindings,
  useFindingDetail,
  useFindingGroups,
  useFindingsState,
  type FindingGroup,
} from '@/data/findings-store'
import { loadMembers, useMembers } from '@/data/members-store'
import { apiText, errorMessage } from '@/lib/api'
import { downloadCsv } from '@/lib/scan-export'
import { loadScans, useScansState } from '@/data/scans-store'
import { showToast } from '@/data/toast-store'
import {
  FINDING_SORT_LABEL,
  UNASSIGNED,
  emptyFilter,
  facetCounts,
  filterFindings,
  filterToExportQuery,
  isFilterActive,
  sortFindings,
  type FindingSortKey,
} from '@/lib/finding-query'
import { AlertTriangle, ArrowDownWideNarrow, Clock, Download, Filter, Search, ShieldCheck, UserCheck, X } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'

/**
 * Findings 漏洞发现库。
 *
 * 版面：筛选工具条 → 列表（表格）→ 详情抽屉。
 *
 * 详情用抽屉而不是常驻侧栏：表格需要横向宽度，常驻侧栏会把它压到不可读。
 * 抽屉从右侧滑出，关闭后表格恢复全宽，同时延续了任务 3 建立的
 * 「内容在右」的习惯。见 components/ui/Drawer.tsx 顶部的完整理由。
 *
 * ## 任务 7：数据全部来自后端，而且**列表与详情是两个请求**
 *
 *   · 列表 → `GET /findings`（含角标所需的全部条目）
 *   · 详情 → `GET /findings/{id}`，**打开抽屉时才发**
 *
 * ★ 筛选 chip 的角标**从已加载的列表本地算**（`lib/finding-query.facetCounts`），
 *   **不**读 `GET /findings/stats`。理由见 `findings-store.ts` 文件头与本文件
 *   `facets` 处的注释：角标必须跟随筛选，而后端 `facets` 参数收单值、
 *   表达不了这一页的多选。`/findings/stats` 仍然活着 —— Overview 页在服务端调它。
 *
 * ⚠️ 详情**必须**走 `useFindingDetail`，不能用列表项渲染抽屉：
 *    列表接口返回的是摘要，不含 `detail` 与 `timeline`。用列表项渲染
 *    会把「空描述 + 空证据」当成"这条漏洞没有内容"显示出来，
 *    而它和"还在加载"长得一模一样 —— 是最容易骗过验收的一种错。
 */

export function FindingsPage() {
  const { findings, status, error, scope } = useFindingsState()
  /*
    这一页还要知道"有没有扫描过" —— 两种空态的含义**完全相反**：
      · 从来没跑过  → "还没开始"，要给下一步
      · 跑过但没发现 → "好消息"，什么都不用做
    用同一句"还没有发现任何漏洞"盖住两者，会让新用户以为
    "扫描过了，什么都没找到" —— 而他从没点过那个按钮。
  */
  const scansState = useScansState()

  // ── 筛选与排序 ──
  const [filter, setFilter] = useState(emptyFilter)
  const [sortKey, setSortKey] = useState<FindingSortKey>('severity')
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc')

  /**
   * 列表 / 分组两种切法（任务 10 P4）。
   *
   * 默认列表：它是"有哪些漏洞"的直接答案，而分组是"哪一类影响面最大"
   * —— 后者是第二问。进来先答第一问。
   */
  const [view, setView] = useState<'list' | 'groups'>('list')

  // ── 选择与详情 ──
  const [selectedIds, setSelectedIds] = useState<Set<string>>(() => new Set())
  const [openId, setOpenId] = useState<string | null>(null)
  /**
   * 「标记为误报」的理由弹窗。
   *
   * ★ 非空 = 弹窗打开，且带着**这一次要改哪些条**。
   *
   * 为什么不只存一个布尔：理由提交时要用到这个列表，而那一刻
   * `selectedFindings` 可能已经因为别的原因变了。存下来的这份是
   * "用户点按钮那一刻的选择"—— 那才是他确认过的范围。
   */
  const [reasonTargets, setReasonTargets] = useState<Finding[] | null>(null)
  /** 理由提交中。提交期间**保持弹窗打开**，失败时用户填的字还在 */
  const [submitting, setSubmitting] = useState(false)
  /**
   * 指派弹窗（任务 10 P2）。
   *
   * 与 `reasonTargets` 同一个模式：存的是"点按钮那一刻选中的那几条"，
   * 而不是一个布尔 —— 提交要用到这个列表，而那一刻 `selectedFindings`
   * 可能已经因为别的原因变了。
   */
  const [assignTargets, setAssignTargets] = useState<Finding[] | null>(null)

  /** CSV 导出进行中（任务 10 P6-c）—— 防重复点击，并给按钮一个忙碌态 */
  const [exporting, setExporting] = useState(false)

  /**
   * 永久删除的确认（任务 10 P6-a）。
   *
   * ★ 这个**必须**先确认，与软删除不同：它不可逆，而且可能被服务端
   *   以 409 拒绝（那条漏洞被某份报告引用了）。确认框里要说清这两点。
   */
  const [purgeTargets, setPurgeTargets] = useState<Finding[] | null>(null)

  /**
   * 处理人名单（任务 10 P2）。
   *
   * ★ **按需加载**：只在真正要指派时（或筛选下拉要渲染时）拉一次。
   * 列表上"负责人"那一列读的是 `finding.assigneeName`（后端已经拼好），
   * 所以进页面**不需要**这个请求 —— 那是它与漏洞列表分开的理由。
   */
  const membersState = useMembers()
  const members = membersState.members

  /** 跳报告页（任务 10 P5）—— 只用于「生成报告」，不参与筛选/渲染 */
  const navigate = useNavigate()

  /** 分组数据（任务 10 P4）。切到分组视图时才由 effect 拉。 */
  const groupsState = useFindingGroups()
  /**
   * 操作失败。
   *
   * ★ 状态流转失败**必须显示出来**：批量标记 50 条时报错却只把按钮恢复原状，
   *   用户会以为改成功了 —— 而"以为漏洞被标成误报"是有后果的。
   */
  const [actionError, setActionError] = useState<string | null>(null)

  // 进页面拉一次。重新挂载（切走再切回）时会再拉一次，这正是想要的：
  // 扫描在后台跑，列表要能看到新增的漏洞。
  useEffect(() => {
    void loadFindings()
    // 空态的分支需要它（见上面 scansState 的说明）
    void loadScans()
    /*
      处理人名单也要拉（任务 10 P2）。

      ⚠️ 它**不是**为了渲染列表上那一列 —— 那一列读 `finding.assigneeName`，
      后端已经拼好了。拉它是为了"负责人"筛选下拉：
      用户要按人筛，就得知道有哪几个人可选，而那个下拉是常驻的。

      代价很小（一份几十条的名单，且 `loadMembers` 自己会去重，
      同一个会话里只真拉一次）。
    */
    void loadMembers()
  }, [])

  // 抽屉打开时才取详情（按需加载，见 store 的文件头）
  useEffect(() => {
    if (openId) void loadFindingDetail(openId)
  }, [openId])

  /**
   * 切到分组视图时才拉分组（任务 10 P4）。
   *
   * `loadFindingGroups` 自己是幂等的（已 `ready` 就直接返回），
   * 所以切来切去不会重复请求。
   *
   * ## ★ 依赖里必须有 `groupsState.status`（2026-10-10 修）
   *
   * 原来只依赖 `[view]`，而这里有个**死锁**：
   *
   *   1. 状态流转会调 `invalidateGroups()`，它把状态置回 `'idle'`（标脏）；
   *   2. 而 `idle` 在渲染分支里**和 `loading` 一样被画成骨架屏**；
   *   3. `view` 没变 → 这个 effect 不重跑 → 永远没人去拉 → **骨架屏永不消失**。
   *
   * 用户唯一的出路是切到「列表」再切回「分组」—— 而他不知道要这么做。
   *
   * 加上 `status` 之后，"被标脏"这件事本身就会触发重拉：
   * `ready` → `idle`（标脏）→ effect 重跑 → `loadFindingGroups()` 看到
   * 不是 `ready` 于是真的去拉 → `loading` → `ready`。
   *
   * ⚠️ 不会死循环：`loadFindingGroups` 的幂等判据是
   *    `if (!force && groupsStatus === 'ready') return`，所以
   *    `loading` / `ready` 两态都不会再次触发请求。依赖变化只发生在
   *    `idle`（标脏）与终态之间，是一次性的。
   */
  useEffect(() => {
    if (view === 'groups') void loadFindingGroups()
  }, [view, groupsState.status])

  const detailState = useFindingDetail(openId)

  const visible = useMemo(
    () => sortFindings(filterFindings(findings, filter), sortKey, sortDir),
    [findings, filter, sortKey, sortDir],
  )

  /** 选中的条目对象 —— 批量操作栏需要读它们的当前状态 */
  const selectedFindings = useMemo(
    () => findings.filter((f) => selectedIds.has(f.id)),
    [findings, selectedIds],
  )

  /**
   * 分组视图按 id 取当前状态与目标。
   *
   * ★ 从**当前列表数据**取，而不是用分组响应里的 `byStatus` ——
   * 后者是拉分组那一刻的快照，而用户可能刚在列表里改了状态。
   * 以列表为准才不会显示过时的东西（分组本身就是"同一批数据换个切法"）。
   *
   * Map 化一次，避免每组每行都 `findings.find(...)`（那是 O(n·m)）。
   *
   * ## ★ 值存**整条 `Finding`**，不只是一对字段（2026-10 修）
   *
   * 原来这里存的是 `{ status, target }` 这样一个**摘出来的小对象**，
   * 于是 `handleGroupAction` 想拿整条记录做状态流转时用不上它，
   * 只好回头 `group.findingIds.map((id) => findings.find(...))` ——
   * 又一次 O(n·m)，而**同一份数据刚刚才建过索引**。
   * 那句"避免 O(n·m)"的注释与实现正好相反，属于最容易被放过的一类：
   * 注释描述的意图是对的，代码只是没用上它。
   *
   * 存整条之后两个用法共用这一个 Map：
   *   · `findingOf`（分组列表的每一行）—— 读 `status` / `target`；
   *   · `handleGroupAction`（组级批量流转）—— 要的就是完整的 `Finding`。
   *
   * ⚠️ 不额外裁剪字段：`findings` 里本来就是完整对象，Map 只是把引用
   *    换个地方存，**不复制**数据 —— 所以"存整条"并不比"存两个字段"
   *    多占内存，反而省掉了每行一次的小对象分配。
   *    这也意味着 Map 的值会随列表刷新而整体换新，与 `findings` 同步。
   */
  const findingById = useMemo(() => new Map(findings.map((f) => [f.id, f])), [findings])
  const findingOf = useCallback(
    (id: string) => findingById.get(id) ?? null,
    [findingById],
  )

  /** 选中整组 —— 把这一组的 id 并进选择集，之后就能用批量栏操作 */
  const handleSelectGroup = useCallback((group: FindingGroup) => {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      for (const id of group.findingIds) next.add(id)
      return next
    })
  }, [])

  /**
   * 抽屉要显示的那条。
   *
   * 详情还没到就用列表项撑住**头部**（标题 + 三个徽标）—— 那几项列表里就有，
   * 先显示出来用户立刻知道打开的是哪条，正文再单独显示骨架。
   * 正文永远不用列表项渲染（见文件头）。
   */
  const listItem = openId ? (findings.find((f) => f.id === openId) ?? null) : null
  const openFinding = detailState.finding ?? listItem

  // ── 计数（筛选 chip 的角标）──
  //
  // ★ 任务 10 P6-b「统计随筛选联动」：角标改成 **facet 计数**，
  //   从**已加载的列表**算（见 `lib/finding-query.facetCounts`）。
  //
  // 之前的做法是读 `stats`（后端全量口径）。那样有两个问题：
  //   1. 角标**不跟随筛选** —— 搜索"登录"之后，角标仍指着全量，
  //      用户点进去发现数量对不上；
  //   2. 更要紧的：后端 `facets` 参数收的是**单值**，而这一页的筛选是
  //      **多选**（可以同时勾「高危 + 中危」），单值 API 表达不了。
  //
  // 从已加载的列表算还有个实际好处：零延迟，且与表格**必然一致**
  // （同一个数组算出来的，不可能出现"表格 3 条、角标 7 条"）。
  //
  // ⚠️ `stats` 仍然在用 —— Overview 页与它的图表读的是后端那一份
  //    （契约 §3.4.1 的"唯一数据源"针对的是**跨页一致性**，那是它要防的）。
  //    无筛选时两者数值相同（同一批数据）。
  const facets = useMemo(() => facetCounts(findings, filter), [findings, filter])
  const bySeverity = facets.bySeverity
  const byStatus = facets.byStatus as Record<FindingStatus, number>
  const byCategory = facets.byCategory as Record<FindingCategory, number>

  // ── 操作 ──

  const toggleSelect = useCallback((id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }, [])

  /** 全选/取消全选：只作用于**当前可见的**条目，不是全部数据 */
  const toggleSelectAll = useCallback(() => {
    setSelectedIds((prev) => {
      const allSelected = visible.every((f) => prev.has(f.id))
      if (allSelected) {
        // 取消只移除可见的这些，保留用户在别的筛选下的选择
        const next = new Set(prev)
        for (const f of visible) next.delete(f.id)
        return next
      }
      const next = new Set(prev)
      for (const f of visible) next.add(f.id)
      return next
    })
  }, [visible])

  /**
   * 状态流转的**统一提交路径**（单条与批量共用）。
   *
   * 两者的差异只有"选中了哪些"，其余（错误处理、反馈、撤销）完全一样。
   * 分成两份写必然漂移，而漂移的表现是「单条能撤销、批量不能」这种
   * 说不清、也难被发现的差异。
   */
  const runTransition = useCallback(
    async (targets: Finding[], to: FindingStatus, note?: string) => {
      setActionError(null)
      try {
        const { changed, skipped } = await changeFindingStatus(
          targets.map((f) => f.id),
          to,
          note,
        )
        setSelectedIds(new Set())
        setReasonTargets(null)

        const label = FINDING_STATUS_LABEL[to]
        const message =
          skipped === 0
            ? `已把 ${changed} 条标记为「${label}」`
            : `已把 ${changed} 条标记为「${label}」，${skipped} 条本就是这个状态`

        /*
          ★ 撤销**不是总能提供的** —— 状态机不对称（任务 10 定的流转表）。

          比如 `fixed → closed` 之后想撤销回 `fixed`：而 `closed` 只能到
          `confirmed`，没有回到 `fixed` 的边。这不是缺陷，是刻意的：
          "关闭"意味着验证过了，退回"已修复"等于否认那次验证。

          所以这里逐条检查"能不能从新状态回到它的原状态"，
          **全部能回才给撤销按钮**。给一个点下去会 400 的撤销，
          比不给更糟 —— 用户会以为数据已经回退，而它没有。
        */
        const undoable = targets.every((f) =>
          FINDING_STATUS_TRANSITIONS[to]?.includes(f.status),
        )

        showToast({
          message,
          action: undoable
            ? {
                label: '撤销',
                onAction: () => {
                  /*
                    按**原状态分组**提交：同一批里各条的来源状态常常不同
                    （比如同时选中了"待确认"和"已确认"两条去标误报），
                    而撤销是"各自回到各自的原处"，不是统一设成某一个值。
                  */
                  const groups = new Map<FindingStatus, string[]>()
                  for (const f of targets) {
                    const list = groups.get(f.status) ?? []
                    list.push(f.id)
                    groups.set(f.status, list)
                  }
                  void Promise.all(
                    [...groups].map(([status, ids]) => changeFindingStatus(ids, status)),
                  ).catch((err) =>
                    showToast({ message: `撤销失败：${errorMessage(err)}`, tone: 'risk' }),
                  )
                },
              }
            : undefined,
        })
      } catch (err) {
        setActionError(errorMessage(err))
      }
    },
    [],
  )

  /**
   * 单条状态流转。
   *
   * 操作后**不关闭抽屉** —— 用户可能想连续改几次（比如先确认再标记修复），
   * 关掉会强制他重新找到这一条。抽屉里的内容会随 store 更新自动刷新。
   */
  const transitionOne = useCallback(
    (id: string, to: FindingStatus) => {
      const finding = findings.find((f) => f.id === id)
      if (!finding) return
      // 单条标误报同样要理由 —— 后端的规则不区分单条还是批量
      if (to === 'false_positive') {
        setReasonTargets([finding])
        return
      }
      void runTransition([finding], to)
    },
    [findings, runTransition],
  )

  /**
   * 批量流转。
   *
   * 后端返回实际改了几条、跳过几条 —— 必须如实反馈。
   * 只说"操作成功"会让用户以为 10 条都改了，实际可能只改了 3 条
   * （其余本来就是那个状态）。
   *
   * ★ 「标记为误报」走弹窗先问理由（后端强制，见 `ReasonDialog` 的文件头）；
   *   其余状态直接执行 —— 它们不需要解释自己。
   */
  const applyBulk = useCallback(
    (to: FindingStatus) => {
      if (selectedFindings.length === 0) return
      if (to === 'false_positive') {
        setReasonTargets(selectedFindings)
        return
      }
      void runTransition(selectedFindings, to)
    },
    [selectedFindings, runTransition],
  )

  /** 理由确认 —— 填一次，应用到全部选中项（后端会逐条写进时间线）。 */
  const confirmFalsePositive = useCallback(
    (reason: string) => {
      if (!reasonTargets) return
      setSubmitting(true)
      void runTransition(reasonTargets, 'false_positive', reason).finally(() =>
        setSubmitting(false),
      )
    },
    [reasonTargets, runTransition],
  )

  /**
   * 指派确认 —— `assigneeId = null` 是**取消指派**（契约把 null 定成
   * 一个明确的值，不是"没传"）。
   *
   * 与状态流转用同一套反馈（toast）。**不提供撤销**：改派回去是一个
   * 有效操作（换人接手很正常），而把它做成"撤销"会让"谁在什么时候
   * 接了手"这件事在时间线上变得含糊。
   */
  const confirmAssign = useCallback(
    (assigneeId: string | null) => {
      const targets = assignTargets
      if (!targets) return
      setSubmitting(true)
      void assignFindings(
        targets.map((f) => f.id),
        assigneeId,
      )
        .then(({ changed, skipped }) => {
          setSelectedIds(new Set())
          setAssignTargets(null)
          const name = assigneeId ? members.find((m) => m.id === assigneeId)?.name : null
          const head = name ? `已把 ${changed} 条指派给「${name}」` : `已取消 ${changed} 条的指派`
          showToast({
            message: skipped === 0 ? head : `${head}，${skipped} 条本就是这个人`,
          })
        })
        .catch((err) => setActionError(errorMessage(err)))
        .finally(() => setSubmitting(false))
    },
    [assignTargets, members],
  )

  /**
   * 组级操作 —— 把整组当一批做状态流转（任务 10 P4）。
   *
   * ★ 复用 `runTransition`（与单条/批量同一条路径），所以撤销、toast、
   * 错误处理的行为完全一致。误报同样会走理由弹窗 —— 后端的规则不区分
   * 你是从哪进来的。
   *
   * ⚠️ 这个函数**必须定义在 `runTransition` 之后**：`const` 声明不提升，
   * 放在前面会在渲染时抛 "used before declaration"。项目里为同一类问题
   * 已经踩过一次（键盘快捷键的 effect 曾引用后面声明的 handler）。
   *
   * ★ 取整组记录走 `findingById`（上面那个 Map），**不再逐个 `find`**。
   *   原来的 `group.findingIds.map((id) => findings.find(...))` 是 O(n·m)：
   *   一个组几十条、列表几千条时，点一次「全组标记为已修复」要扫几十万次。
   *   而这份索引在同一渲染里**已经建好了**，只是当时它只存了两个字段，
   *   装不下这里要的整条记录（见 `findingById` 的说明）。
   */
  const handleGroupAction = useCallback(
    (group: FindingGroup, status: FindingStatus) => {
      const targets = group.findingIds
        .map((id) => findingById.get(id))
        .filter((f): f is Finding => Boolean(f))
      if (targets.length === 0) return
      if (status === 'false_positive') {
        setReasonTargets(targets)
        return
      }
      void runTransition(targets, status)
    },
    [findingById, runTransition],
  )

  const clearFilter = () => setFilter(emptyFilter)
  const filterActive = isFilterActive(filter)

  // ── 回收站（任务 10 P6-a）──
  //
  // `scope` 由 store 持有（快照里带着它），页面读的是快照那份 ——
  // 这样自动刷新不会把范围换掉（见 `FindingsSnapshot.scope` 的说明）。

  /**
   * 移入回收站。
   *
   * ★ 与扫描模块**同一套交互**：立即执行 + toast 给一个撤销窗口，
   *   不先弹确认框。误删不丢数据（只是打标记），所以"先做再撤销"
   *   比"先问一遍"更顺 —— 而确认框点多了会被无脑确认，那时它就不起作用了。
   *
   * ⚠️ 撤销走**同一条** `restoreFindings`，而不是另写一份 ——
   *   两条路径会让"撤销"与"从回收站恢复"的行为迟早分叉。
   */
  const handleBulkDelete = useCallback(async () => {
    const ids = [...selectedIds]
    if (ids.length === 0) return

    setActionError(null)
    try {
      const result = await deleteFindings(ids)
      setSelectedIds(new Set())
      /*
        ★ 静默重拉而不是本地移除。

        记录"离开当前视图"本来可以乐观移除，但删除有个细节：后端返回的
        `changed` 可能**小于**选中的条数（已经在回收站里的算 `skipped`）。
        本地按 `ids` 全删会和服务的真实状态对不上 —— 而那个差值会在
        下次刷新时"自己冒出来"，看起来像列表无故多了一条。
        重拉一次拿到的就是服务端的真相。
      */
      void loadFindings({ silent: true, scope: 'active' })

      showToast({
        message:
          result.skipped > 0
            ? `已移入回收站 ${result.changed} 条（${result.skipped} 条本来就在里面）`
            : `已移入回收站 ${result.changed} 条`,
        action: {
          label: '撤销',
          onAction: () => {
            void restoreFindings(ids)
              .then(() => {
                void loadFindings({ silent: true, scope: 'active' })
                showToast({ message: `已恢复 ${result.changed} 条` })
              })
              .catch((err) =>
                showToast({ message: `撤销失败：${errorMessage(err)}`, tone: 'risk' }),
              )
          },
        },
      })
    } catch (err) {
      setActionError(errorMessage(err))
    }
  }, [selectedIds])

  /** 从回收站恢复 */
  const handleBulkRestore = useCallback(async () => {
    const ids = [...selectedIds]
    if (ids.length === 0) return
    setActionError(null)
    try {
      const result = await restoreFindings(ids)
      setSelectedIds(new Set())
      showToast({
        message:
          result.skipped > 0
            ? `已恢复 ${result.changed} 条（${result.skipped} 条本来就不在回收站）`
            : `已恢复 ${result.changed} 条`,
      })
    } catch (err) {
      setActionError(errorMessage(err))
    }
  }, [selectedIds])

  /**
   * 永久删除（确认之后调用）。
   *
   * ★ **逐条调用**而不是批量端点：契约里 purge 就是单条（与
   *   `purge_scan` 一致）。这样"3 条里有 1 条被报告引用"时，
   *   另外 2 条仍然能删掉，而错误消息精确指向被挡住的那一条 ——
   *   批量端点做不到这一点（要么全成功要么全失败，而用户无法
   *   分辨是哪一条有问题）。
   */
  const handlePurge = useCallback(async () => {
    const targets = purgeTargets ?? []
    setPurgeTargets(null)
    if (targets.length === 0) return

    setActionError(null)
    let ok = 0
    const blocked: string[] = []
    for (const finding of targets) {
      try {
        await purgeFinding(finding.id)
        ok += 1
      } catch (err) {
        blocked.push(errorMessage(err))
      }
    }
    setSelectedIds(new Set())

    if (blocked.length === 0) {
      showToast({ message: `已永久删除 ${ok} 条` })
    } else {
      // 被挡住的原因原样透传 —— 后端那句中文说清了是哪几份报告
      setActionError(
        ok > 0
          ? `已永久删除 ${ok} 条，另有 ${blocked.length} 条被拒绝：${blocked[0]}`
          : blocked[0],
      )
    }
  }, [purgeTargets])

  /**
   * 导出当前筛选结果为 CSV（任务 10 P6-c）。
   *
   * ★ **导出必须表达与列表相同的筛选** —— 否则用户筛出 3 条高危、
   *   点导出拿到全部 200 条，而他会把那份表**发出去**。
   *   "错误的数据比没有数据更糟。"
   *
   * ⚠️ 两种情况下我选择**拦住**而不是导出：
   *   · 负责人那一维选了多个 —— 后端的 `assigneeId` 与 `unassigned`
   *     是两个参数，逗号表达不了"或"。此时导出会**范围更大**，
   *     所以宁可让用户缩小选择，也不给他一份悄悄变宽的表。
   *   · 当前筛选下 0 条 —— 导出一份只有表头的文件没有意义，
   *     而它会看起来像"导出成功了"。
   */
  const handleExport = useCallback(async () => {
    const { query, unexpressableDimensions } = filterToExportQuery(filter)

    if (unexpressableDimensions.length > 0) {
      showToast({
        message: `导出一次只能筛选一个${unexpressableDimensions.join('、')}，请先缩小选择。`,
        tone: 'info',
      })
      return
    }
    if (visible.length === 0) {
      showToast({ message: '当前筛选下没有可导出的条目。', tone: 'info' })
      return
    }

    setExporting(true)
    try {
      /*
        ⚠️ 走 `apiText` 而**不是** `window.open` / `<a href>`：
        后者拿不到失败原因 —— 后端出错时用户会看到一个空白页或
        一个没下载的文件，而不知道是权限问题还是服务没起。
        `apiText` 会把后端那句中文错误带回来。
      */
      const download = await apiText(findingsExportUrl(query))
      /*
        ★ 文件名优先用**后端给的那个**（`apiText` 从 `Content-Disposition`
        解析出来的），前端只做兜底。

        两个理由：文件名是"这份导出是什么"的一部分，而它由服务端
        决定（契约 §3.14 规定 `suvex-findings-<日期>.csv`）；
        前端再拼一遍就是第二处会漂移的口径 —— 而漂移的表现是
        "同一个导出在两个入口下叫不同的名字"。

        兜底只在反代剥掉了响应头时才用得上（那时服务端已经无法
        告诉我们它想叫什么了，有个名字总比 `download` 强）。
      */
      downloadCsv(download.filename ?? 'suvex-findings.csv', download.text)
      showToast({ message: `已导出 ${visible.length} 条漏洞` })
    } catch (err) {
      showToast({ message: `导出失败：${errorMessage(err)}`, tone: 'risk' })
    } finally {
      setExporting(false)
    }
  }, [filter, visible.length])

  return (
    <>
      <PageHeader navId="findings" />

      <div
        className={cn(
          'px-gutter py-bay',
          // 批量操作栏是固定定位的，选中时给内容留出空间，避免遮住最后一行
          selectedIds.size > 0 && 'pb-24',
        )}
      >
        {/*
          ── 三态（任务 7 要求）──
          顺序很重要：loading 必须先于空状态。反过来的话，进页面会先闪一下
          「还没有发现任何漏洞」—— 那是**好消息**，而真相是数据还在路上。

          筛选工具条也一起放进 ready 分支：它的角标从已加载的列表算
          （见 `facetCounts`），在数据到达前列表是空的，渲染出来会是一排 0，
          且每个 chip 都因 count===0 被禁用，看起来像"这个筛选坏了"。
        */}
        {status === 'loading' ? (
          <div className="flex flex-col gap-panel">
            <SkeletonRows rows={1} rowClassName="h-16" />
            <SkeletonRows rows={5} rowClassName="h-14" />
          </div>
        ) : status === 'error' ? (
          <ErrorState title="漏洞列表加载失败" error={error} onRetry={() => void loadFindings()} />
        ) : (
          <>
            {/* ── 筛选工具条 ──
                所有字段统一「标签在上、内容在下」。

                ★ 为什么窄屏用两列网格、宽屏才用 flex-wrap：
                  flex-wrap 是**贪心装箱且不回溯**的 —— 它把「严重度」(342px) 放满一行后，
                  后面的字段各自都塞不进剩余空间，于是每个字段独占一行，
                  390px 下整块筛选区被拉到约 340px 高，把列表挤到首屏之外。
                  网格则由浏览器统一分配列宽，不看顺序，窄屏固定两列后
                  高度立刻减半，且所有字段天然共享同一条左基准（这正是重构的目的）。
                  宽屏字段总宽放得下，才换回 flex-wrap 让它自然流动。 */}
            <div className="mb-panel grid grid-cols-2 items-start gap-x-tight gap-y-tight min-[380px]:grid-cols-3 sm:flex sm:flex-wrap">
              <FilterGroup label="严重度">
                {SEVERITY_ORDER.map((severity) => {
                  const count = bySeverity[severity] ?? 0
                  const active = filter.severities.has(severity)
                  return (
                    <ToggleChip
                      key={severity}
                      size="xs"
                      active={active}
                      disabled={count === 0}
                      onToggle={() =>
                        setFilter((prev) => ({
                          ...prev,
                          severities: toggled(prev.severities, severity),
                        }))
                      }
                    >
                      <SeverityBadge severity={severity} variant="dot" />
                      <span className={SEVERITY_META[severity].text}>
                        {SEVERITY_META[severity].label}
                      </span>
                      <span className="tabular text-ink-faint" data-numeric>
                        {count}
                      </span>
                    </ToggleChip>
                  )
                })}
              </FilterGroup>

              <FilterGroup label="状态">
                {FINDING_STATUS_ORDER.map((status_) => {
                  const count = byStatus[status_] ?? 0
                  const active = filter.statuses.has(status_)
                  return (
                    <ToggleChip
                      key={status_}
                      size="xs"
                      active={active}
                      disabled={count === 0}
                      onToggle={() =>
                        setFilter((prev) => ({ ...prev, statuses: toggled(prev.statuses, status_) }))
                      }
                    >
                      {FINDING_STATUS_LABEL[status_]}
                      <span className="tabular text-ink-faint" data-numeric>
                        {count}
                      </span>
                    </ToggleChip>
                  )
                })}
              </FilterGroup>

              <FilterField label="漏洞类型" htmlFor="finding-category">
                {/* 内层用 div 不用 label：外层 FilterField 的 label[htmlFor] 已经
                    关联到这个 select 了，再套一层 label 会形成 label 嵌套（无效 HTML）。
                    这里只负责边框和图标这些视觉外壳。 */}
                <div className="flex h-8 items-center gap-1.5 rounded-sm border border-line px-2 text-xs text-ink-muted">
                  <Filter size={13} strokeWidth={1.75} className="shrink-0" aria-hidden="true" />
                  <select
                    id="finding-category"
                    value={[...filter.categories][0] ?? ''}
                    onChange={(e) => {
                      const value = e.target.value
                      setFilter((prev) => ({
                        ...prev,
                        // 单选下拉，所以集合里最多一个
                        categories: value ? new Set([value]) : new Set<string>(),
                      }))
                    }}
                    className="h-full bg-transparent text-xs text-ink-muted focus:outline-none"
                  >
                    <option value="" className="bg-surface-overlay">
                      全部类型
                    </option>
                    {(Object.keys(FINDING_CATEGORY_LABEL) as FindingCategory[])
                      .filter((category) => (byCategory[category] ?? 0) > 0)
                      .map((category) => (
                        <option key={category} value={category} className="bg-surface-overlay">
                          {FINDING_CATEGORY_LABEL[category]}（{byCategory[category]}）
                        </option>
                      ))}
                  </select>
                </div>
              </FilterField>

              {/*
                ── 负责人筛选（任务 10 P2）──

                用下拉而不是 chips：人员名单可能几十个，chips 会把筛选区
                撑成两屏，而其他维度（严重度 5 个、状态 6 个）是**固定的
                小集合**，chips 在那里才合适。

                ★ 选项里带「未指派」：它是这个维度的另一个取值
                （"还没人认领"），而不是"没有筛选"。少了它，
                用户没法回答"还有哪些没人管"—— 而那是最该被问的问题。
              */}
              <FilterField label="负责人" htmlFor="finding-assignee">
                <div className="flex h-8 items-center gap-1.5 rounded-sm border border-line px-2 text-xs text-ink-muted">
                  <UserCheck size={13} strokeWidth={1.75} className="shrink-0" aria-hidden="true" />
                  <select
                    id="finding-assignee"
                    value={[...filter.assignees][0] ?? ''}
                    onChange={(e) => {
                      const value = e.target.value
                      setFilter((prev) => ({
                        ...prev,
                        assignees: value ? new Set([value]) : new Set<string>(),
                      }))
                    }}
                    className="h-full bg-transparent text-xs text-ink-muted focus:outline-none"
                  >
                    <option value="" className="bg-surface-overlay">
                      全部负责人
                    </option>
                    <option value={UNASSIGNED} className="bg-surface-overlay">
                      未指派
                    </option>
                    {/*
                      名单按名字排（store 已经排好）。已停用的人**也列出来** ——
                      他名下的漏洞还在，"这个人手上有几条"仍然是个有效问题。
                    */}
                    {members.map((member) => (
                      <option key={member.id} value={member.id} className="bg-surface-overlay">
                        {member.name}
                        {member.active ? '' : '（已停用）'}
                      </option>
                    ))}
                  </select>
                </div>
              </FilterField>

              {/*
                ── 修复时限筛选（任务 10 P3）──

                三档对应后端给的 `slaState`：超期 / 临期 / 还早。
                不含"不计时"—— 那是"这个档位不设时限"或"已终结"，
                用户不会想专门筛出它们（而且它们本来就没有标记可看）。
              */}
              <FilterField label="时限" htmlFor="finding-sla">
                <div className="flex h-8 items-center gap-1.5 rounded-sm border border-line px-2 text-xs text-ink-muted">
                  <Clock size={13} strokeWidth={1.75} className="shrink-0" aria-hidden="true" />
                  <select
                    id="finding-sla"
                    value={[...filter.slaStates][0] ?? ''}
                    onChange={(e) => {
                      const value = e.target.value
                      setFilter((prev) => ({
                        ...prev,
                        slaStates: value ? new Set([value]) : new Set<string>(),
                      }))
                    }}
                    className="h-full bg-transparent text-xs text-ink-muted focus:outline-none"
                  >
                    <option value="" className="bg-surface-overlay">全部时限</option>
                    <option value="overdue" className="bg-surface-overlay">已超期</option>
                    <option value="dueSoon" className="bg-surface-overlay">临期</option>
                    <option value="onTrack" className="bg-surface-overlay">还早</option>
                  </select>
                </div>
              </FilterField>
              <FilterField label="搜索" htmlFor="finding-search">                <div className="relative">
                  <Search
                    size={13}
                    strokeWidth={1.75}
                    className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-ink-ghost"
                    aria-hidden="true"
                  />
                  <input
                    id="finding-search"
                    type="search"
                    value={filter.query}
                    onChange={(e) => setFilter((prev) => ({ ...prev, query: e.target.value }))}
                    placeholder="标题 / 目标"
                    className={cn(
                      // w-full 只在窄屏网格下生效（宽屏回到固定 w-52，避免输入框被拉得过宽）
                      'h-8 w-full rounded-sm border border-line bg-surface-sunken pl-7 pr-2 text-xs text-ink sm:w-52',
                      'transition-ui duration-fast placeholder:text-ink-faint',
                      'hover:border-line-strong focus:outline-none focus:ring-2 focus:ring-accent/40',
                    )}
                  />
                </div>
              </FilterField>

              <FilterField label="排序" htmlFor="finding-sort">
                {/* 内层同样用 div。里面还嵌着方向切换按钮，
                    若外壳是 label，点那个按钮会连带触发 label 的聚焦行为 —— 焦点会跳。 */}
                <div className="flex h-8 items-center gap-1.5 rounded-sm border border-line px-2 text-xs text-ink-muted">
                  <ArrowDownWideNarrow size={13} strokeWidth={1.75} aria-hidden="true" />
                  <select
                    id="finding-sort"
                    value={sortKey}
                    onChange={(e) => setSortKey(e.target.value as FindingSortKey)}
                    className="h-full bg-transparent text-xs text-ink-muted focus:outline-none"
                  >
                    {(Object.keys(FINDING_SORT_LABEL) as FindingSortKey[]).map((key) => (
                      <option key={key} value={key} className="bg-surface-overlay">
                        {FINDING_SORT_LABEL[key]}
                      </option>
                    ))}
                  </select>
                  <IconButton
                    label={sortDir === 'desc' ? '当前降序，点击切换升序' : '当前升序，点击切换降序'}
                    className="ml-0.5"
                    onClick={() => setSortDir((d) => (d === 'desc' ? 'asc' : 'desc'))}
                  >
                    {sortDir === 'desc' ? '↓' : '↑'}
                  </IconButton>
                </div>
              </FilterField>

              {/*
                ── 视图切换（任务 10 P4）──

                与"排序"放在一起：它和排序是同类的 ——
                都改变"这批数据怎么呈现"，而不改变"看到哪些"。
                筛选类控件（严重度/状态/类型/搜索）改的是范围，所以它们在前。

                ★ 分组视图**不做成另一个 tab 页**：它是同一批数据的另一种切法，
                  筛选条件完全共用。做成独立页面会让用户以为筛选要重设一遍。

                ⚠️ 这里**不用 `FilterField`**：那个组件天生是给
                `<label htmlFor>` + 单个表单控件配对的，而视图切换是一组按钮，
                没有可关联的控件。硬塞一个 id 会生成指向空元素的 label ——
                点标签没反应，屏幕阅读器也会读出个不存在的控件。
                所以标签用纯文本，分组语义交给内层的 `role="group"`。
              */}
              <div className="flex flex-col gap-1">
                <span className="text-2xs text-ink-faint">视图</span>
                <div
                  role="group"
                  aria-label="列表或分组"
                  className="flex h-8 items-center rounded-sm border border-line p-0.5"
                >
                  {(
                    [
                      { key: 'list' as const, label: '列表' },
                      { key: 'groups' as const, label: '分组' },
                    ]
                  ).map((option) => (
                    <ToggleChip
                      key={option.key}
                      variant="segment"
                      active={view === option.key}
                      onToggle={() => setView(option.key)}
                    >
                      {option.label}
                    </ToggleChip>
                  ))}
                </div>
              </div>

              {/* 清除筛选：只在真的有筛选时出现。
                  它没有标签，所以底部对齐到控件那一行，不跟标签行。 */}
              {filterActive && (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={clearFilter}
                  /* 底部对齐到控件那一行（它没有标签行）；`px-2` 沿用原间距，
                     与相邻的「导出 CSV」按钮保持同宽节奏 */
                  className="self-end px-2"
                >
                  <X size={12} strokeWidth={2} aria-hidden="true" />
                  清除筛选
                </Button>
              )}

              {/*
                ── 回收站入口（任务 10 P6-a）──

                与扫描模块**同一套语义**：默认视图排除已删除的条目，
                回收站是另一个范围（不是筛选器的一部分）。

                ★ 用 `aria-pressed` 的切换按钮而不是又一个 tab：
                它切换的是"看哪一批数据"，与「列表/分组」那个
                "怎么切这批数据"是**两个轴**，长得一样会让人以为
                它们是同一组里的选项。

                ⚠️ 切换时调 `loadFindings({ scope })` —— 范围由 store 持有，
                而它会**清空列表再拉**：留着旧范围的数据会让用户在加载的
                那一瞬间看到"回收站里有 13 条"（其实是默认视图那些）。
              */}
              <div className="flex flex-col gap-1">
                <span className="text-2xs text-ink-faint">范围</span>
                <div
                  role="group"
                  aria-label="默认视图或回收站"
                  className="flex h-8 items-center rounded-sm border border-line p-0.5"
                >
                  {(
                    [
                      { key: 'active' as const, label: '未删除' },
                      { key: 'trash' as const, label: '回收站' },
                    ]
                  ).map((option) => (
                    <ToggleChip
                      key={option.key}
                      variant="segment"
                      active={scope === option.key}
                      onToggle={() => {
                        // 换范围时清掉选择：上一个范围里选中的 id 在新范围里
                        // 多半不存在，留着它们会让批量栏显示"已选 3 条"而
                        // 列表里一条都没选中。
                        setSelectedIds(new Set())
                        setOpenId(null)
                        void loadFindings({ scope: option.key })
                      }}
                    >
                      {option.label}
                    </ToggleChip>
                  ))}
                </div>
              </div>

              {/*
                ── 导出 CSV（任务 10 P6-c）──

                ★ 导出的是**当前筛选结果**，不是全部 —— 见 `handleExport`。
                  这一点在按钮上就写清楚（"导出筛选结果"），
                  而不是只写"导出"让用户自己猜范围。
              */}
              <Button
                variant="secondary"
                size="sm"
                onClick={() => void handleExport()}
                disabled={exporting}
                title={filterActive ? '导出当前筛选结果（与列表一致）' : '导出全部漏洞'}
                className="self-end px-2"
              >
                <Download size={12} strokeWidth={2} aria-hidden="true" />
                {exporting ? '导出中…' : filterActive ? '导出筛选结果' : '导出全部'}
              </Button>
            </div>

            {/* ── 操作失败 ──
                与成功横幅同一位置：用户的视线在操作区，反馈就该出现在那儿。
                用 signal-risk 而不是警告黄 —— 这是"没改成"，不是"注意一下"。 */}
            {actionError && (
              <div
                role="alert"
                className="mb-panel flex items-center gap-tight rounded-sm border border-signal-risk/40 bg-signal-risk/[0.08] px-panel py-2 text-sm text-ink"
              >
                <AlertTriangle
                  size={14}
                  strokeWidth={2}
                  className="shrink-0 text-signal-risk"
                  aria-hidden="true"
                />
                {actionError}
              </div>
            )}

            {/*
              ── 原来的"批量操作反馈"横幅已移除（任务 10 P1）──

              它是一块**页面内**的提示条，2.5 秒后自己消失、且**不可撤销**；
              而扫描任务页用的是全局 toast + 5 秒撤销窗口。同一个产品里
              两套批量操作的反馈方式不同，用户在两页之间切换时要重新学一遍 ——
              这正是需求里明确要求拉齐的那一条。

              现在统一走 `data/toast-store`：成功/失败、单条/批量都从
              `runTransition` 一个出口发，不再需要在页面里维护 `feedback` 状态。
            */}

            {/* ── 分组视图（任务 10 P4）── */}
            {view === 'groups' ? (
              <div className="rounded-md border border-line bg-surface">
                {groupsState.status === 'loading' || groupsState.status === 'idle' ? (
                  <SkeletonRows rows={5} rowClassName="h-14" />
                ) : groupsState.status === 'error' ? (
                  <div className="px-panel py-bay">
                    <ErrorState
                      title="分组加载失败"
                      error={groupsState.error}
                      onRetry={() => void loadFindingGroups({ force: true })}
                    />
                  </div>
                ) : groupsState.groups.length === 0 ? (
                  /*
                    空态与列表视图**说不同的话**：
                    列表空是"没有符合条件的漏洞"，分组空是"没有可归类的实例"。
                    后者在同一批数据下几乎总是因为筛选 —— 所以直接给清除入口。
                  */
                  <div className="flex flex-col items-center gap-tight px-panel py-bay text-center">
                    <p className="text-sm text-ink-muted">没有可分组的结果</p>
                    <p className="max-w-[46ch] text-xs text-ink-faint">
                      分组按漏洞的标题归类。当前筛选下没有任何条目，
                      或者它们还没被归类。
                    </p>
                    {filterActive && (
                      <Button variant="link" size="sm" onClick={clearFilter}>
                        清除筛选条件
                      </Button>
                    )}
                  </div>
                ) : (
                  <FindingsGroups
                    groups={groupsState.groups}
                    findingOf={findingOf}
                    onGroupAction={handleGroupAction}
                    onSelectGroup={handleSelectGroup}
                  />
                )}
              </div>
            ) : (
              /* ── 列表 ── */
              <div className="rounded-md border border-line bg-surface">
                <FindingsTable
                findings={visible}
                selectedIds={selectedIds}
                onToggleSelect={toggleSelect}
                onToggleSelectAll={toggleSelectAll}
                onOpen={(finding) => setOpenId(finding.id)}
                activeId={openId}
                emptyState={
                  findings.length > 0 ? (
                    // 筛选后为空 —— 和"一条都没有"是两回事，提示必须不同
                    <div className="flex flex-col items-center gap-tight text-center">
                      <p className="text-sm text-ink-muted">没有符合条件的漏洞</p>
                      <Button variant="link" size="sm" onClick={clearFilter}>
                        清除筛选条件
                      </Button>
                    </div>
                  ) : scansState.status === 'ready' && scansState.scans.length === 0 ? (
                    /*
                      从没扫过。**这是任务 7 特意回头验的那条路径** ——
                      新用户第一次打开系统时最可能先落在这一页
                      （"漏洞发现"在导航里比"扫描任务"更靠后，但它更吸引人点）。

                      ⚠️ 原来这里无论哪种情况都写「还没有发现任何漏洞」。
                        对一个从没点过"发起扫描"的人来说，这句话是**错的** ——
                        它听起来像"扫过了，很干净"，而那是一件让人放心地
                        关掉页面的事。真相是他还没开始。
                    */
                    <NoScansYet
                      bordered={false}
                      title="还没有发现任何漏洞"
                      description="发起第一次扫描之后，发现的问题会出现在这里 —— 每条都带有证据与可复现的验证脚本。"
                    />
                  ) : (
                    <div className="flex flex-col items-center gap-tight text-center">
                      <ShieldCheck
                        size={20}
                        strokeWidth={1.5}
                        className="text-signal-ok"
                        aria-hidden="true"
                      />
                      {/* 跑过了、什么都没有 —— 这确实是"好消息"，不显示 0 值表格 */}
                      <p className="text-sm text-ink-muted">还没有发现任何漏洞</p>
                      <p className="max-w-[46ch] text-xs text-ink-faint">
                        扫描发现的问题会出现在这里。每条都带有证据与可复现的验证脚本。
                      </p>
                    </div>
                  )
                }
              />
              </div>
            )}

            {/* 统计脚注：区分"筛选后"与"全部"，口径必须写清 */}
            {findings.length > 0 && (
              <p className="mt-panel text-2xs text-ink-faint">
                {filterActive
                  ? `筛选出 ${visible.length} 条，共 ${findings.length} 条`
                  : `共 ${findings.length} 条漏洞`}
                {byStatus.new > 0 && ` · ${byStatus.new} 条待确认`}
              </p>
            )}

            {/*
              批量操作栏必须在内容流的**最后** —— sticky 只有在这个位置才能
              正确停靠在视口底部（见 BulkActionBar 顶部关于 fixed/sticky 的说明）。
            */}
            <BulkActionBar
              selected={selectedFindings}
              inTrash={scope === 'trash'}
              onApply={applyBulk}
              onAssign={() => setAssignTargets(selectedFindings)}
              /*
                带着选中项去报告页（任务 10 P5）。

                id 走 URL 而不是某个全局状态：刷新页面后选择还在、
                也能把链接直接发给别人。而 `encodeURIComponent` 是必须的 ——
                id 里有逗号之外的字符时，不编码会切错分段。
              */
              onExportReport={() =>
                navigate(`/reports?findings=${encodeURIComponent(selectedFindings.map((f) => f.id).join(','))}`)
              }
              onDelete={() => void handleBulkDelete()}
              onRestore={() => void handleBulkRestore()}
              onPurge={() => setPurgeTargets(selectedFindings)}
              onClear={() => setSelectedIds(new Set())}
            />
          </>
        )}
      </div>

      {/* ── 详情抽屉 ── */}
      <Drawer
        open={openId !== null}
        onClose={() => setOpenId(null)}
        labelledBy="finding-detail-title"
        title={openFinding?.title ?? ''}
        titleAside={
          openFinding && (
            /*
              三个徽标都放在**固定不滚动的头部** —— 读证据和 PoC 时它们始终可见。
              正文里因此不再重复（见 FindingDetail 结论区的说明）。
              这也是「验证程度」和「处置状态」两个轴并排展示的地方：
              用户能一眼看出"证据很硬但还没人处理"。
            */
            <>
              <SeverityBadge severity={openFinding.severity} />
              <StatusBadge status={openFinding.status} />
              <VerificationBadge verification={openFinding.verification} />
            </>
          )
        }
        footer={
          /*
            操作区只在**详情已到**时渲染。
            提前渲染的话，用户可能在详情还没加载完时就点到流转按钮 ——
            而那时他看到的是空的描述与证据，"确认为漏洞"就是在盲签。
          */
          detailState.status === 'ready' && openFinding ? (
            <FindingTransitionBar
              finding={openFinding}
              onTransition={(to) => transitionOne(openFinding.id, to)}
            />
          ) : null
        }
      >
        {openId === null ? null : detailState.status === 'error' ? (
          <div className="px-panel py-bay">
            <ErrorState
              title="漏洞详情加载失败"
              error={detailState.error}
              onRetry={() => void refreshFindingDetail(openId)}
            />
          </div>
        ) : detailState.status === 'ready' && detailState.finding ? (
          <FindingDetail finding={detailState.finding} />
        ) : (
          <DetailSkeleton />
        )}
      </Drawer>

      {/*
        ── 永久删除确认（任务 10 P6-a）──

        与"移入回收站"不同，这一步**必须**先问：不可逆，而且可能被
        服务端拒绝（那条漏洞被某份报告引用了 —— 那时错误消息里
        会点名是哪几份报告，见契约 §3.13）。

        确认文案里把这两件事都说出来，而不是只写"确定吗？"。
      */}
      <ConfirmDialog
        open={purgeTargets !== null}
        title="永久删除这些漏洞？"
        description={
          `将永久删除 ${purgeTargets?.length ?? 0} 条漏洞，此操作**不可撤销**。` +
          '被报告引用的条目会被拒绝，并提示是哪几份报告引用了它。'
        }
        confirmLabel="永久删除"
        danger
        onConfirm={() => void handlePurge()}
        onCancel={() => setPurgeTargets(null)}
      />

      {/*
        ── 误报理由弹窗 ──

        单条与批量共用（`reasonTargets` 装着这一次要改的那几条）：
        后端的规则不区分单条还是批量，界面也不该分。
      */}
      <ReasonDialog
        open={reasonTargets !== null}
        count={reasonTargets?.length ?? 0}
        busy={submitting}
        onConfirm={confirmFalsePositive}
        // 提交中不许关 —— 关掉会让用户以为没提交，而请求其实在路上
        onCancel={() => {
          if (submitting) return
          setReasonTargets(null)
        }}
      />

      {/*
        ── 指派弹窗（任务 10 P2）──

        `currentNames` 传的是这批里**已有负责人**的那几个名字，
        用于在弹窗里说清"你会覆盖掉谁" —— 改派是常见操作（换人接手），
        但用户必须知道自己在覆盖什么。
      */}
      <AssignDialog
        open={assignTargets !== null}
        count={assignTargets?.length ?? 0}
        members={members}
        currentNames={[
          ...new Set(
            (assignTargets ?? [])
              .map((f) => f.assigneeName)
              .filter((name): name is string => Boolean(name)),
          ),
        ]}
        busy={submitting}
        onConfirm={confirmAssign}
        onCancel={() => {
          if (submitting) return
          setAssignTargets(null)
        }}
      />
    </>
  )
}

/**
 * 详情正文的骨架。
 *
 * 逐节对应 `FindingDetail` 的真实结构（结论栏 → 描述 → 推理 → 证据 → 时间线），
 * 而不是随便几根灰条：骨架的**形状**是它唯一的语言，
 * 形状对不上就等于什么都没说，数据到达时还会整块跳动。
 */
function DetailSkeleton() {
  return (
    <div className="flex flex-col gap-bay px-panel py-bay" role="status" aria-live="polite">
      <span className="sr-only">正在加载漏洞详情</span>

      {/* 结论栏：四行「标签 + 值」 */}
      <div className="flex flex-col gap-1.5">
        {['w-24', 'w-40', 'w-16', 'w-32'].map((width, index) => (
          <div key={index} className="flex gap-tight">
            <Skeleton className="h-3.5 w-16 shrink-0" />
            <Skeleton className={cn('h-3.5', width)} />
          </div>
        ))}
      </div>

      {/* 描述 */}
      <div className="flex flex-col gap-tight">
        <Skeleton className="h-3.5 w-20" />
        <SkeletonText lines={3} />
      </div>

      {/* 推理（它有自己的强调框，骨架也照做，否则数据到时整块位移） */}
      <div className="flex flex-col gap-tight">
        <Skeleton className="h-3.5 w-32" />
        <div className="rounded-sm border border-accent/25 bg-accent-wash/40 px-3 py-2.5">
          <SkeletonText lines={2} />
        </div>
      </div>

      {/* 证据代码块 */}
      <div className="flex flex-col gap-tight">
        <Skeleton className="h-3.5 w-12" />
        <Skeleton className="h-32 rounded-sm" />
      </div>
    </div>
  )
}

// ─────────────────────────────────────────────
// 小组件
// ─────────────────────────────────────────────

/** 集合的切换 —— 不改原集合，返回新集合（React 依赖引用比较） */
function toggled(set: Set<string>, value: string): Set<string> {
  const next = new Set(set)
  if (next.has(value)) next.delete(value)
  else next.add(value)
  return next
}

/**
 * 筛选组：**标签在上，内容在下**。
 *
 * 早先是「标签在左、胶囊在右」的行内混排，问题不止一处：
 *   1. 标签宽度不同 → 各组的胶囊起点就不同（严重度 3 字 vs 状态 2 字，差一个字宽）；
 *   2. 标签占掉首行开头，换行后的胶囊回落到**组**的左缘而不是**上一行胶囊**的左缘，
 *      三行胶囊变成 62 / 24 / 50 的台阶；
 *   3. 带标签的组（严重度/状态）和不带标签的控件（类型/搜索/排序）混在一屏，
 *      两套对齐基准并存，整块筛选区看起来是散的。
 *
 * 改成标签独占一行之后：全条**只有一个左对齐基准**，胶囊无论折几行都落在同一条竖线上，
 * 分组之间也不再有"谁的标签更长"的偶然差异。
 * 代价是多占一行高度 —— 值得，筛选区本来就是"扫一眼就懂"比"省一行"重要。
 */
function FilterGroup({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div
      role="group"
      aria-label={label}
      // 网格下胶囊组跨满整宽：胶囊最小 ~62px，一格放不下两个就会叠成多行。
      // 列数分档（2 列 / 3 列）与 col-span 必须同步，否则会残留空列。
      className="col-span-2 flex flex-col gap-1.5 min-[380px]:col-span-3 sm:col-span-1"
    >
      <span className="text-2xs text-ink-faint">{label}</span>
      <div className="flex flex-wrap items-center gap-1">{children}</div>
    </div>
  )
}

/**
 * 单个控件的筛选字段。和 FilterGroup 结构完全一致，
 * 区别是用真 `<label htmlFor>` 关联控件 —— 点标签能聚焦过去，比 `sr-only` 更实在。
 */
function FilterField({
  label,
  htmlFor,
  children,
}: {
  label: string
  htmlFor: string
  children: React.ReactNode
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={htmlFor} className="text-2xs text-ink-faint">
        {label}
      </label>
      {children}
    </div>
  )
}

/*
  ── 原来的本地 `Chip` 已收编进 `components/ui/ToggleChip`（`variant="pill" size="xs"`）──

  它被两处筛选组共用（严重度、状态），形态与 `Kernel.tsx` / `ScansPage.tsx`
  的状态筛选是同一个 —— 放在页面里时三个地方各写一遍，改一次就得记着改三处。
  收编后 `disabled` 那条「不叠加 opacity」的规矩成了基元的固定行为，不会再被抄漏。
*/
export type { Finding }
export default FindingsPage