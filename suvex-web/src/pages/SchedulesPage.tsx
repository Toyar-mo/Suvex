import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { AlarmClock, Play, Plus, RefreshCw, Search, Trash2 } from 'lucide-react'
import { PageHeader } from '@/components/layout/AppShell'
import ConfirmDialog from '@/components/ui/ConfirmDialog'
import { Modal } from '@/components/ui/Modal'
import EmptyState from '@/components/ui/EmptyState'
import ErrorState from '@/components/ui/ErrorState'
import { Button } from '@/components/ui/Button'
import { SkeletonRows } from '@/components/ui/Skeleton'
import { cn } from '@/lib/cn'
import { showToast } from '@/data/toast-store'
import {
  INTERVAL_PRESETS,
  MAX_INTERVAL_MINUTES,
  MIN_INTERVAL_MINUTES,
  SCHEDULE_STATE_META,
  SCHEDULE_TARGET_KINDS,
  compareSchedules,
  formatInterval,
  formatRelative,
  scheduleMatchesQuery,
  scheduleState,
  type Schedule,
  type ScheduleState,
} from '@/data/schedule'
import {
  createSchedule,
  deleteSchedule,
  loadSchedules,
  toggleSchedule,
  useSchedulesState,
} from '@/data/schedules-store'
import {
  SCAN_AUTHORIZATION_STATEMENT,
  SCAN_GOAL_HINT,
  SCAN_GOAL_LABEL,
  TARGET_KIND_HINT,
  TARGET_KIND_LABEL,
  type TargetKind,
} from '@/data/scan'
import { defaultProviderId, useProviders } from '@/data/providers'

/**
 * 定时计划 —— 「哪些目标在自动复查，还靠不靠得住」。
 *
 * ## 这一页回答什么
 *
 * 渗透测试真正会腐坏的不是「这一轮扫出了什么」，而是**没人再去看它**。
 * 一个三个月前扫干净的目标，今天的结论已经不可信了 —— 而没有任何一条
 * 漏洞会告诉你这件事。定期复查就是对抗这种腐坏的唯一手段。
 *
 * 所以这一页的**主要职责不是「管理计划」**（增删改是次要动作），
 * 而是让「哪条计划已经不工作了」在第一屏可见：
 *
 *   · 连续失败的计划**看起来还在跑**（它有下次执行时间、有历史），
 *     但每次到点都没建出扫描来 —— 它并没有在帮你复查；
 *   · 停用三个月前随手关掉的计划，用户早就忘了自己关过；
 *   · 调度器没在运行时，所有计划的「下次执行时间」都会停在过去，
 *     而列表看起来完全正常。
 *
 * 这三种"静默失效"是这类功能的真正失效方式，所以状态列必须先于
 * 名字列被读到，排序也必须把有问题的排前面（见 `compareSchedules`）。
 *
 * ## 为什么用表格而不是卡片流
 *
 * 定时计划是**要被横向比较的**（哪条下次跑得最早、哪条一直在失败）。
 * 卡片流每张卡自成一格，比较只能靠眼睛来回跳；表格的列对齐让
 * "扫一眼找异常"这件事变便宜。计划数量也不会多到需要卡片来降密度。
 */

type StateFilter = 'all' | ScheduleState

/** 空列表的稳定常量 —— 避免每次渲染产生新数组引用。 */
const EMPTY_SCHEDULES: Schedule[] = []

/** 状态筛选按钮的顺序：先"要人管"的，最后才是"正常"。 */
const FILTERS: ReadonlyArray<{ value: StateFilter; label: string }> = [
  { value: 'all', label: '全部' },
  { value: 'broken', label: '数据异常' },
  { value: 'failing', label: '连续失败' },
  { value: 'due', label: '已到点' },
  { value: 'never-run', label: '待首次执行' },
  { value: 'ok', label: '正常' },
  { value: 'paused', label: '已停用' },
]

function StateBadge({ state }: { state: ScheduleState }) {
  const meta = SCHEDULE_STATE_META[state]
  return (
    <span className={cn('inline-flex items-center gap-1.5 text-xs', meta.className)} title={meta.hint}>
      <span className={cn('h-1.5 w-1.5 shrink-0 rounded-full', meta.dot)} aria-hidden="true" />
      {meta.label}
    </span>
  )
}

function SummaryCard({
  label,
  value,
  hint,
  tone = 'default',
}: {
  label: string
  value: number
  hint?: string
  tone?: 'default' | 'warn' | 'risk'
}) {
  return (
    <div className="rounded-md border border-line bg-surface px-panel py-3">
      <p className="text-xs text-ink-muted">{label}</p>
      <p
        className={cn(
          'mt-1 text-2xl font-semibold tabular-nums',
          tone === 'risk' ? 'text-signal-risk' : tone === 'warn' ? 'text-signal-warn' : 'text-ink',
        )}
      >
        {value}
      </p>
      {hint && <p className="mt-1 text-[11px] leading-snug text-ink-muted">{hint}</p>}
    </div>
  )
}

/**
 * 新建计划表单。
 *
 * 只有**新建**，没有"编辑全部字段"：改目标、改判据相当于换了一个任务，
 * 应该删了重建（旧计划的历史与失败计数对它没有意义）。改名和改间隔
 * 走行内的 PATCH，因为那两个才是真的"调整同一条计划"。
 *
 * ★ **组件只在打开时挂载**（父组件 `{creating && <CreateScheduleDialog/>}`）。
 *   这样"每次打开都是干净的"由 React 的挂载语义保证，不需要在 effect 里
 *   逐个 `setState('')` 重置 —— 那种写法会触发级联渲染（eslint
 *   `react-hooks/set-state-in-effect` 拦的就是它），而且漏掉一个字段就
 *   会把上次填了一半的内容留在那儿，用户会以为是自己填的。
 *
 * ★ 是 **Dialog（居中弹窗）不是 Drawer（右侧抽屉）**。六个字段 + 两个
 *   横向按钮组塞进 34rem 的竖长抽屉，只能一路竖排，右侧空掉大半、
 *   底部还要滚动才够得着「创建」。这个动作本身是"专心填完再建一条"，
 *   遮断列表反而更对 —— 判据与 `components/ui/Modal.tsx` 顶部注释一致。
 */
function CreateScheduleDialog({ onClose }: { onClose: () => void }) {
  const providers = useProviders()
  const [name, setName] = useState('')
  const [target, setTarget] = useState('')
  const [targetKind, setTargetKind] = useState<string>('url')
  const [goal, setGoal] = useState<string>('findVulns')
  const [intervalMinutes, setIntervalMinutes] = useState(1440)
  const [pickedProviderId, setPickedProviderId] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  /*
    用户没选过时用"第一个可用的 provider"，而不是在挂载时把默认值
    `setState` 进去。后者有个真问题：`useProviders()` 首次渲染可能还是
    空数组，effect 里取到的默认值是 `null`，等 provider 列表到位后
    也不会再更新 —— 于是"默认选中"永远不生效，而用户会看到空白下拉框。
    派生值没有这个时序问题。
  */
  const providerId = pickedProviderId ?? defaultProviderId(providers)

  const trimmedTarget = target.trim()
  const disabledReason =
    trimmedTarget === ''
      ? '请先填写要定期复查的目标'
      : providerId === null
        ? '还没有可用的模型服务，请先在设置里配置'
        : null

  const submit = async () => {
    if (disabledReason !== null || providerId === null) return
    setBusy(true)
    setError(null)
    try {
      await createSchedule({
        name: name.trim() === '' ? trimmedTarget : name.trim(),
        target: trimmedTarget,
        targetKind,
        goal,
        intervalMinutes,
        providerId,
        skills: [],
        brief: null,
        authorizationStatement: SCAN_AUTHORIZATION_STATEMENT,
      })
      showToast({ message: '定时计划已创建' })
      onClose()
    } catch (caught) {
      // 不关抽屉：用户填的东西还在，改一处再提交比重新填一遍省事
      setError(caught instanceof Error ? caught.message : '创建失败')
    } finally {
      setBusy(false)
    }
  }

  const kindHint = TARGET_KIND_HINT[targetKind as TargetKind] ?? null
  const goalHint = SCAN_GOAL_HINT[goal as keyof typeof SCAN_GOAL_HINT] ?? null

  return (
    <Modal
      open
      onClose={onClose}
      widthClass="max-w-3xl"
      title="新建定时计划"
      titleAside={
        <span className="rounded-sm border border-line px-1.5 py-0.5 text-[11px] text-ink-muted">
          {formatInterval(intervalMinutes)}
        </span>
      }
      footer={
        <div className="flex items-center justify-between gap-tight">
          <p className="min-w-0 flex-1 truncate text-xs text-ink-muted">
            {disabledReason ?? `每 ${formatInterval(intervalMinutes).replace('每 ', '')} 自动发起一次扫描`}
          </p>
          <div className="flex shrink-0 gap-tight">
            <Button variant="secondary" size="sm" onClick={onClose} disabled={busy}>
              取消
            </Button>
            <Button
              variant="primary"
              size="sm"
              onClick={() => void submit()}
              loading={busy}
              disabled={disabledReason !== null}
            >
              创建
            </Button>
          </div>
        </div>
      }
    >
      {/*
        ★ 两列栅格而不是单列堆叠。
        原先 544px 宽的抽屉里六个字段一路竖排，每行右边空掉一大半，
        而整页要滚动才能看到"创建"按钮 —— 用户得反复上下找自己填到哪了。
        改成 3xl（768px）弹窗 + 两列之后：名称与目标并排、形态与判据各占
        一行（它们都是横向一组按钮），一整屏能看全，不用滚。
      */}
      <div className="flex flex-col gap-panel">
        <div className="grid grid-cols-1 gap-panel sm:grid-cols-2">
          <label className="flex min-w-0 flex-col gap-1">
            <span className="text-xs text-ink-muted">名称</span>
            <input
              type="text"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="留空则用目标名"
              className="rounded-sm border border-line bg-canvas px-2.5 py-1.5 text-sm text-ink placeholder:text-ink-muted focus:border-accent focus:outline-none"
            />
          </label>

          <label className="flex min-w-0 flex-col gap-1">
            <span className="text-xs text-ink-muted">目标</span>
            <input
              type="text"
              value={target}
              onChange={(event) => setTarget(event.target.value)}
              placeholder={kindHint?.placeholder ?? ''}
              className="rounded-sm border border-line bg-canvas px-2.5 py-1.5 text-sm text-ink placeholder:text-ink-muted focus:border-accent focus:outline-none"
            />
          </label>
        </div>

        <div className="flex flex-col gap-1">
          <span className="text-xs text-ink-muted">目标形态</span>
          <div className="flex flex-wrap gap-tight">
            {/*
              ★ 遍历 `SCHEDULE_TARGET_KINDS` 而不是 `TARGET_KIND_LABEL` 的全部键。
              「题目附件」在定时计划里立不起来（附件是一次性暂存，计划存不下），
              放出来只会让用户选完、填完、点创建，然后收到一个 400。
              为什么不在按钮上做 disabled：一个永远点不动的选项比不出现更让人
              困惑 —— 用户会以为是权限问题或页面没加载完。
              后端 `POST /schedules` 同样拒绝，两处口径一致。
            */}
            {SCHEDULE_TARGET_KINDS.map((value) => (
              <button
                key={value}
                type="button"
                onClick={() => setTargetKind(value)}
                aria-pressed={targetKind === value}
                className={cn(
                  'rounded-sm border px-2.5 py-1 text-xs transition-colors',
                  targetKind === value
                    ? 'border-accent bg-accent/10 text-accent'
                    : 'border-line text-ink-muted hover:text-ink',
                )}
              >
                {TARGET_KIND_LABEL[value]}
              </button>
            ))}
          </div>
          {kindHint !== null && (
            <p className="text-[11px] text-ink-muted">例如 {kindHint.example}</p>
          )}
        </div>

        <div className="flex flex-col gap-1">
          <span className="text-xs text-ink-muted">这次复查要达成什么</span>
          <div className="grid grid-cols-1 gap-1 sm:grid-cols-3">
            {Object.entries(SCAN_GOAL_LABEL).map(([value, label]) => (
              <button
                key={value}
                type="button"
                onClick={() => setGoal(value)}
                aria-pressed={goal === value}
                className={cn(
                  'rounded-sm border px-2.5 py-1.5 text-left text-xs transition-colors',
                  goal === value
                    ? 'border-accent bg-accent/10 text-accent'
                    : 'border-line text-ink-muted hover:text-ink',
                )}
              >
                {label}
              </button>
            ))}
          </div>
          {goalHint !== null && <p className="text-[11px] text-ink-muted">{goalHint}</p>}
        </div>

        <div className="flex flex-col gap-1">
          <span className="text-xs text-ink-muted">复查间隔</span>
          <div className="flex flex-wrap gap-tight">
            {INTERVAL_PRESETS.map((preset) => (
              <button
                key={preset.minutes}
                type="button"
                onClick={() => setIntervalMinutes(preset.minutes)}
                aria-pressed={intervalMinutes === preset.minutes}
                className={cn(
                  'rounded-sm border px-2.5 py-1 text-xs transition-colors',
                  intervalMinutes === preset.minutes
                    ? 'border-accent bg-accent/10 text-accent'
                    : 'border-line text-ink-muted hover:text-ink',
                )}
              >
                {preset.label}
              </button>
            ))}
          </div>
          <p className="text-[11px] text-ink-muted">
            允许范围 {MIN_INTERVAL_MINUTES} 分钟 ~ {Math.round(MAX_INTERVAL_MINUTES / 1440)} 天。
            再密就不是定期复查，而是持续压测了。
          </p>
        </div>

        <label className="flex flex-col gap-1">
          <span className="text-xs text-ink-muted">使用哪个模型服务</span>
          <select
            value={providerId ?? ''}
            onChange={(event) => setPickedProviderId(event.target.value || null)}
            className="rounded-sm border border-line bg-canvas px-2.5 py-1.5 text-sm text-ink focus:border-accent focus:outline-none"
          >
            {providers.map((provider) => (
              <option key={provider.id} value={provider.id} disabled={!provider.configured}>
                {provider.label}
                {provider.configured ? '' : '（未配置）'}
              </option>
            ))}
          </select>
        </label>

        <p className="rounded-sm border border-line bg-canvas px-2.5 py-2 text-[11px] leading-relaxed text-ink-muted">
          创建即表示你确认：{SCAN_AUTHORIZATION_STATEMENT}。每到一个周期，系统会以你的身份
          对上述目标发起一次真实扫描 —— 它会发包、会尝试利用，可能触发目标侧的告警。
        </p>

        {error !== null && (
          <p role="alert" className="rounded-sm border border-signal-risk/40 bg-signal-risk/10 px-2.5 py-2 text-xs text-signal-risk">
            {error}
          </p>
        )}
      </div>
    </Modal>
  )
}

export default function SchedulesPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const state = useSchedulesState()
  const [query, setQuery] = useState('')
  const [stateFilter, setStateFilter] = useState<StateFilter>('all')
  const [now, setNow] = useState(() => Date.now())
  /*
    ── 新建抽屉的开关：draft 覆盖 URL 派生值 ──

    ★ 为什么不是 `useState(false)` + effect 里看 `?new=1` 再 `setCreating(true)`：

      那会撞上项目的 `react-hooks/set-state-in-effect` 规则（effect 里同步
      setState 会多一轮渲染、容易写出循环）。规则挡得对 —— 但更重要的是
      这种写法的**语义**是错的：它让「抽屉开没开」变成两份需要手动对齐的
      真相（URL 里一份、state 里一份），而同步的时机永远差一帧。

      这里用 `ScanRetentionPanel` 同一个模式：
        · 没被用户碰过 → 由 URL 派生（`?new=1` 就是开着）
        · 用户点了「新建」或「关闭」→ `draft` 接管
      于是"从命令面板跳过来自动打开"和"用户自己开关"是同一个表达式算出来的，
      没有第二条需要维护的路径。
  */
  const [creatingDraft, setCreatingDraft] = useState<boolean | null>(null)
  const creating = creatingDraft ?? searchParams.get('new') === '1'
  const setCreating = setCreatingDraft
  const [pendingDelete, setPendingDelete] = useState<Schedule | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [togglingId, setTogglingId] = useState<string | null>(null)

  // 首次进入拉一次
  const loadedOnce = useRef(false)
  useEffect(() => {
    if (loadedOnce.current) return
    loadedOnce.current = true
    if (state.status === 'idle') void loadSchedules()
  }, [state.status])

  /*
    ── 把 `?new=1` 从地址栏抹掉 ──

    命令面板的「新建定时复查」跳到这里时带 `?new=1`。

    ★ 抹掉的理由：参数留在地址栏里，用户关掉抽屉后刷新页面、或者
      从别的页面按后退回来，抽屉会**自己再弹一次** —— 那时他已经不记得
      自己几秒前点过什么，只觉得"这玩意儿自己弹"。

    ★ 这里只有导航（`setSearchParams`），没有 setState ——
      抽屉是否打开已经由上面的派生存量表达，不需要在 effect 里纠正。
  */
  useEffect(() => {
    if (searchParams.get('new') !== '1') return
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev)
        next.delete('new')
        return next

      },
      { replace: true },
    )
  }, [searchParams, setSearchParams])

  /*
    ★ 轮询：每 30 秒静默刷新一次。

    与资产页刻意不同 —— 资产是台账，变化只来自用户自己的操作；
    定时计划是**活的**：`nextRunAt` 到点后被调度器推进、`failureCount`
    在后台涨。不轮询的话，页面上的"下次执行"会停在过去，用户会以为
    调度器挂了（而它可能好好的），或者以为计划还在跑（而它早就连续失败了）。

    `silent: true` 是必须的：轮询失败不该把用户正在看的表格变成错误页。
  */
  useEffect(() => {
    const timer = window.setInterval(() => {
      void loadSchedules({ silent: true })
    }, 30_000)
    return () => window.clearInterval(timer)
  }, [])

  // 相对时间文案要跟着走（30 秒一次，与轮询同频）
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 30_000)
    return () => window.clearInterval(timer)
  }, [])

  const refresh = useCallback(() => {
    void loadSchedules({ silent: state.items !== null })
  }, [state.items])

  const items = useMemo(() => state.items ?? EMPTY_SCHEDULES, [state.items])

  const filtered = useMemo(() => {
    return items
      .filter((schedule) => stateFilter === 'all' || scheduleState(schedule, now) === stateFilter)
      .filter((schedule) => scheduleMatchesQuery(schedule, query))
      .sort((a, b) => compareSchedules(a, b, now))
  }, [items, stateFilter, query, now])

  const summary = useMemo(() => {
    let failing = 0
    let due = 0
    let paused = 0
    for (const schedule of items) {
      const value = scheduleState(schedule, now)
      if (value === 'failing' || value === 'broken') failing += 1
      else if (value === 'due') due += 1
      else if (value === 'paused') paused += 1
    }
    return { total: items.length, failing, due, paused }
  }, [items, now])

  const onToggle = async (schedule: Schedule) => {
    setTogglingId(schedule.id)
    try {
      const next = await toggleSchedule(schedule.id, !schedule.enabled)
      showToast({
        message: next.enabled ? `已启用「${next.name}」` : `已停用「${next.name}」`,
        tone: 'ok',
      })
    } catch (caught) {
      showToast({
        message: caught instanceof Error ? caught.message : '启停失败',
        tone: 'risk',
      })
    } finally {
      setTogglingId(null)
    }
  }

  const onConfirmDelete = async () => {
    if (pendingDelete === null) return
    setDeleting(true)
    try {
      await deleteSchedule(pendingDelete.id)
      showToast({ message: `已删除「${pendingDelete.name}」` })
      setPendingDelete(null)
    } catch (caught) {
      showToast({
        message: caught instanceof Error ? caught.message : '删除失败',
        tone: 'risk',
      })
    } finally {
      setDeleting(false)
    }
  }

  if (state.status === 'loading' && state.items === null) {
    return (
      <>
        <PageHeader navId="schedules" />
        <div className="px-gutter py-bay">
          <SkeletonRows rows={4} />
        </div>
      </>
    )
  }

  if (state.status === 'error' && state.items === null) {
    return (
      <>
        <PageHeader navId="schedules" />
        <div className="px-gutter py-bay">
          <ErrorState error={state.error} onRetry={refresh} title="定时计划加载失败" />
        </div>
      </>
    )
  }

  return (
    <>
      <PageHeader navId="schedules" />

      <div className="px-gutter py-bay">
        {/* ── 统计条 ─────────────────────────────────────────── */}
        {items.length > 0 && (
          <div className="mb-bay grid grid-cols-2 gap-panel sm:grid-cols-4">
            <SummaryCard label="计划总数" value={summary.total} />
            <SummaryCard
              label="已经不工作了"
              value={summary.failing}
              hint="到点在跑，但每次都建不出扫描 —— 它没在帮你复查"
              tone={summary.failing > 0 ? 'risk' : 'default'}
            />
            <SummaryCard
              label="已到点未派发"
              value={summary.due}
              hint="超过下次执行时间还没发起 —— 调度器可能没在运行"
              tone={summary.due > 0 ? 'warn' : 'default'}
            />
            <SummaryCard
              label="已停用"
              value={summary.paused}
              hint="确认过不用再自动复查，还是三个月前随手关掉忘了"
            />
          </div>
        )}

        {/* ── 工具条 ─────────────────────────────────────────── */}
        <div className="mb-panel flex flex-wrap items-center gap-tight">
          <div className="relative min-w-[12rem] flex-1">
            <Search
              size={14}
              className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-ink-muted"
              aria-hidden="true"
            />
            <input
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="按名称或目标筛选…"
              aria-label="按名称或目标筛选定时计划"
              className="w-full rounded-sm border border-line bg-surface py-1.5 pl-8 pr-2.5 text-sm text-ink placeholder:text-ink-muted focus:border-accent focus:outline-none"
            />
          </div>

          <div className="flex flex-wrap gap-tight">
            {FILTERS.map((filter) => (
              <button
                key={filter.value}
                type="button"
                onClick={() => setStateFilter(filter.value)}
                aria-pressed={stateFilter === filter.value}
                className={cn(
                  'rounded-sm border px-2 py-1 text-xs transition-colors',
                  stateFilter === filter.value
                    ? 'border-accent bg-accent/10 text-accent'
                    : 'border-line text-ink-muted hover:text-ink',
                )}
              >
                {filter.label}
              </button>
            ))}
          </div>

          <Button
            variant="secondary"
            size="sm"
            onClick={refresh}
            disabled={state.status === 'loading'}
            title="重新拉取定时计划"
          >
            <RefreshCw size={13} aria-hidden="true" />
            刷新
          </Button>

          <Button variant="primary" size="sm" onClick={() => setCreating(true)}>
            <Plus size={13} aria-hidden="true" />
            新建计划
          </Button>
        </div>

        {/* ── 积压提示：调度器可能没在运行 ───────────────────── */}
        {summary.due > 0 && (
          <p
            role="status"
            className="mb-panel rounded-sm border border-signal-warn/40 bg-signal-warn/10 px-3 py-2 text-xs text-signal-warn"
          >
            有 {summary.due} 条计划已经过了下次执行时间却还没派发出去。
            如果这个数字持续不降，说明扫描调度器没在运行 —— 定期复查此时是停摆的。
          </p>
        )}

        {/* ── 表格 ───────────────────────────────────────────── */}
        {filtered.length === 0 ? (
          <EmptyState
            icon={AlarmClock}
            title={items.length === 0 ? '还没有定时计划' : '没有符合条件的计划'}
            description={
              items.length === 0
                ? '定期复查让「上次扫描是三个月前」这种事不会悄悄发生。先给最重要的目标排一条。'
                : '换个关键词或切回「全部」看看。'
            }
            action={
              items.length === 0 ? (
                <Button variant="primary" size="sm" onClick={() => setCreating(true)}>
                  <Plus size={13} aria-hidden="true" />
                  新建计划
                </Button>
              ) : undefined
            }
          />
        ) : (
          <div className="overflow-x-auto rounded-md border border-line">
            <table className="w-full min-w-[54rem] border-collapse text-sm">
              <thead>
                <tr className="border-b border-line bg-surface text-left text-xs text-ink-muted">
                  <th className="px-3 py-2 font-medium">状态</th>
                  <th className="px-3 py-2 font-medium">名称 / 目标</th>
                  <th className="px-3 py-2 font-medium">间隔</th>
                  <th className="px-3 py-2 font-medium">下次执行</th>
                  <th className="px-3 py-2 font-medium">上次执行</th>
                  <th className="px-3 py-2 font-medium">最近一次扫描</th>
                  <th className="px-3 py-2 font-medium text-right">操作</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((schedule) => {
                  const value = scheduleState(schedule, now)
                  return (
                    <tr key={schedule.id} className="border-b border-line last:border-0 hover:bg-surface/60">
                      <td className="px-3 py-2 align-top">
                        <StateBadge state={value} />
                        {schedule.failureCount > 0 && (
                          <p className="mt-0.5 text-[11px] text-signal-risk">
                            连续 {schedule.failureCount} 次失败
                          </p>
                        )}
                        {schedule.lastFailureReason !== null && (
                          <p className="mt-0.5 max-w-[16rem] truncate text-[11px] text-ink-muted" title={schedule.lastFailureReason}>
                            {schedule.lastFailureReason}
                          </p>
                        )}
                      </td>
                      <td className="px-3 py-2 align-top">
                        <p className="font-medium text-ink">{schedule.name}</p>
                        <p className="mt-0.5 text-xs text-ink-muted">
                          {TARGET_KIND_LABEL[schedule.targetKind as TargetKind] ?? schedule.targetKind} · {schedule.target}
                        </p>
                      </td>
                      <td className="px-3 py-2 align-top text-xs text-ink-muted">
                        {formatInterval(schedule.intervalMinutes)}
                      </td>
                      <td className="px-3 py-2 align-top text-xs text-ink-muted">
                        {formatRelative(schedule.nextRunAt, now)}
                      </td>
                      <td className="px-3 py-2 align-top text-xs text-ink-muted">
                        {formatRelative(schedule.lastRunAt, now)}
                      </td>
                      <td className="px-3 py-2 align-top text-xs">
                        {schedule.lastScanId === null ? (
                          <span className="text-ink-muted">—</span>
                        ) : (
                          <Link
                            to={`/scans?scan=${encodeURIComponent(schedule.lastScanId)}`}
                            className="text-accent hover:underline"
                          >
                            查看
                          </Link>
                        )}
                      </td>
                      <td className="px-3 py-2 align-top">
                        <div className="flex justify-end gap-1">
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => void onToggle(schedule)}
                            loading={togglingId === schedule.id}
                            title={schedule.enabled ? '停用这条计划' : '启用这条计划'}
                          >
                            {schedule.enabled ? '停用' : '启用'}
                          </Button>
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => setPendingDelete(schedule)}
                            title="删除这条计划"
                          >
                            <Trash2 size={13} aria-hidden="true" />
                          </Button>
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}

        <p className="mt-3 text-[11px] leading-relaxed text-ink-muted">
          排序口径：数据异常 → 连续失败 → 已到点 → 待首次执行 → 正常 → 已停用，
          同档内按下次执行时间从近到远。
          <Play size={10} className="mx-1 inline align-baseline" aria-hidden="true" />
          计划本身不会自动停用 —— 连续失败会一直重试，直到你决定修它还是关掉它。
        </p>
      </div>

      {/* 只在打开时挂载 —— 这样"每次打开都是干净的"由挂载语义保证 */}
      {creating && <CreateScheduleDialog onClose={() => setCreating(false)} />}

      <ConfirmDialog
        open={pendingDelete !== null}
        title="删除这条定时计划？"
        description={
          pendingDelete === null
            ? ''
            : `「${pendingDelete.name}」（${pendingDelete.target}）将不再自动复查。已经跑过的扫描记录会保留。`
        }
        confirmLabel="删除"
        onConfirm={() => void onConfirmDelete()}
        onCancel={() => setPendingDelete(null)}
        busy={deleting}
      />
    </>
  )
}
