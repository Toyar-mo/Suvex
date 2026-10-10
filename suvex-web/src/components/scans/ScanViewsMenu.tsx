import { Bookmark, ChevronDown, Plus, Trash2 } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

import {
  MAX_SCAN_VIEWS,
  MAX_SCAN_VIEW_NAME,
  addScanView,
  loadScanViews,
  removeScanView,
  useScanViews,
} from '@/data/scan-views'
import { cn } from '@/lib/cn'
import {
  decodeViewQuery,
  describeViewQuery,
  type ScanViewParams,
} from '@/lib/scan-view-query'

interface Props {
  /**
   * 当前筛选编码后的 query 串。**空串 = 什么都没筛**。
   *
   * 用「编码后再比较」而不是传一个 `isFiltered` 布尔，是因为它同时能回答
   * 两个问题：有没有东西可存（空串不能存），以及当前是不是正套着某条视图
   * （和 `view.query` 逐字符相等即命中）。一个值，两处用途，不会不一致。
   */
  currentQuery: string
  onApply: (params: ScanViewParams) => void
}

/**
 * 「我的视图」—— 保存/套用/删除筛选组合。
 *
 * ## 加载时机：打开时才拉，不用 `useEffect` 预热
 *
 * 两个原因，一个是规则上的、一个是体验上的：
 *
 * 1. `loadScanViews()` 会**同步**把状态置为 `loading` 并通知订阅者。
 *    在 effect 体里这么干会撞上 `react-hooks/set-state-in-effect`
 *    （effect 里同步 setState 会多一轮渲染，且容易写出循环）。
 * 2. 视图列表只有这个菜单用得到。页面一打开就为它发一个请求，
 *    在用户根本不看这个菜单时是纯浪费 —— 而按需拉取的代价只是
 *    第一次展开时多等一次本地往返。
 *
 * `status === 'ready'` 时不重复拉，避免每次展开都闪一下骨架。
 */
export default function ScanViewsMenu({ currentQuery, onApply }: Props) {
  const { views, status, saving, error } = useScanViews()
  const [open, setOpen] = useState(false)
  const [name, setName] = useState('')
  const wrapRef = useRef<HTMLDivElement>(null)
  const buttonRef = useRef<HTMLButtonElement>(null)

  // 点击外部 / Esc 关闭。菜单挂着不关会挡住下面的列表行
  useEffect(() => {
    if (!open) return
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node
      if (wrapRef.current?.contains(target)) return
      setOpen(false)
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      setOpen(false)
      buttonRef.current?.focus()
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  const activeView = views.find((view) => view.query === currentQuery) ?? null
  const hasFilter = currentQuery.length > 0
  const atLimit = views.length >= MAX_SCAN_VIEWS
  const duplicate = views.some((view) => view.name === name.trim())
  const canSave = hasFilter && !atLimit && !saving && name.trim().length > 0 && !duplicate

  function toggle() {
    const next = !open
    setOpen(next)
    // 失败过就再给一次机会 —— 否则用户只能刷新页面才能重试
    if (next && status !== 'ready') void loadScanViews()
  }

  async function handleSave() {
    if (!canSave) return
    const ok = await addScanView(name, currentQuery)
    if (ok) setName('')
  }

  return (
    <div ref={wrapRef} className="relative">
      <button
        ref={buttonRef}
        type="button"
        onClick={toggle}
        aria-expanded={open}
        aria-haspopup="menu"
        className={cn(
          'inline-flex h-8 items-center gap-1.5 rounded-pill border px-2.5 text-xs',
          'transition-ui duration-fast',
          activeView
            ? 'border-accent/50 bg-accent-wash text-ink'
            : 'border-line text-ink-muted hover:border-line-strong hover:text-ink',
        )}
        title={
          activeView
            ? `当前视图：${activeView.name}（${describeViewQuery(activeView.query)}）`
            : '把当前筛选条件存下来，下次一键套用'
        }
      >
        <Bookmark size={12} strokeWidth={1.75} aria-hidden="true" />
        {activeView ? activeView.name : '我的视图'}
        <ChevronDown
          size={11}
          strokeWidth={2}
          aria-hidden="true"
          className={cn('transition-ui duration-fast', open && 'rotate-180')}
        />
      </button>

      {open && (
        <div
          role="menu"
          className="absolute right-0 top-full z-30 mt-1 w-72 animate-stream-in rounded-md border border-line bg-surface-overlay p-1.5 shadow-overlay"
        >
          {/* ── 保存当前筛选 ── */}
          <div className="border-b border-line/60 px-1 pb-2 pt-1">
            <label
              htmlFor="scan-view-name"
              className="mb-1 block text-2xs text-ink-faint"
            >
              保存当前筛选
            </label>
            <div className="flex items-center gap-1.5">
              <input
                id="scan-view-name"
                value={name}
                onChange={(event) => setName(event.target.value)}
                onKeyDown={(event) => {
                  // 输入框里回车即保存。这里拦下事件，别让它冒到全局快捷键去
                  if (event.key !== 'Enter') return
                  event.preventDefault()
                  event.stopPropagation()
                  void handleSave()
                }}
                maxLength={MAX_SCAN_VIEW_NAME}
                placeholder={hasFilter ? '例如：内网高危未修' : '先设置筛选条件'}
                disabled={!hasFilter}
                className={cn(
                  'h-7 min-w-0 flex-1 rounded-sm border border-line bg-surface px-2 text-xs text-ink',
                  'placeholder:text-ink-ghost disabled:opacity-50',
                )}
              />
              <button
                type="button"
                onClick={() => void handleSave()}
                disabled={!canSave}
                aria-label="保存为我的视图"
                className={cn(
                  'inline-flex h-7 shrink-0 items-center gap-1 rounded-sm border border-line px-2 text-2xs',
                  'transition-ui duration-fast',
                  canSave
                    ? 'text-ink-muted hover:border-line-strong hover:text-accent-strong'
                    : 'cursor-not-allowed text-ink-ghost',
                )}
              >
                <Plus size={11} strokeWidth={2} aria-hidden="true" />
                保存
              </button>
            </div>

            {/*
              条件不满足时说清是**哪一条**不满足。
              只把按钮置灰而不说原因，用户会反复点它。
            */}
            {!hasFilter && (
              <p className="mt-1 text-2xs text-ink-ghost">当前没有筛选条件，存下来等于没存。</p>
            )}
            {hasFilter && atLimit && (
              <p className="mt-1 text-2xs text-signal-warn">
                最多 {MAX_SCAN_VIEWS} 个视图，先删掉不用的。
              </p>
            )}
            {hasFilter && duplicate && (
              <p className="mt-1 text-2xs text-signal-warn">已有同名视图。</p>
            )}
            {error && status === 'ready' && (
              <p className="mt-1 text-2xs text-signal-risk">{error.message}</p>
            )}
          </div>

          {/* ── 已存视图 ── */}
          <div className="max-h-72 overflow-y-auto pt-1">
            {status === 'loading' || status === 'idle' ? (
              <p className="px-2 py-3 text-2xs text-ink-faint">加载中…</p>
            ) : status === 'error' ? (
              <div className="px-2 py-3">
                <p className="text-2xs text-signal-risk">{error?.message ?? '加载失败'}</p>
                <button
                  type="button"
                  onClick={() => void loadScanViews()}
                  className="mt-1 text-2xs text-accent-strong hover:underline"
                >
                  重试
                </button>
              </div>
            ) : views.length === 0 ? (
              <p className="px-2 py-3 text-2xs text-ink-faint">
                还没有保存的视图。设好筛选条件后在上面起个名字即可。
              </p>
            ) : (
              views.map((view) => {
                const isActive = view.query === currentQuery
                return (
                  /*
                    菜单项是 div 不是 button：里面还要放一个「删除」按钮，
                    button 套 button 是非法 HTML，浏览器会把它拆开，
                    结果是删除按钮跑到菜单项外面、点删除会连带套用视图。
                  */
                  <div
                    key={view.id}
                    className={cn(
                      'group flex items-center gap-1 rounded-sm pr-1',
                      'transition-ui duration-fast',
                      isActive ? 'bg-accent-wash' : 'hover:bg-surface-raised',
                    )}
                  >
                    <button
                      role="menuitem"
                      type="button"
                      onClick={() => {
                        onApply(decodeViewQuery(view.query))
                        setOpen(false)
                      }}
                      className="min-w-0 flex-1 px-2 py-1.5 text-left"
                    >
                      <span
                        className={cn(
                          'block truncate text-xs',
                          isActive ? 'text-ink' : 'text-ink-muted',
                        )}
                      >
                        {view.name}
                      </span>
                      <span className="block truncate text-2xs text-ink-ghost">
                        {describeViewQuery(view.query)}
                      </span>
                    </button>

                    <button
                      type="button"
                      onClick={() => void removeScanView(view.id)}
                      disabled={saving}
                      aria-label={`删除视图 ${view.name}`}
                      title="删除这个视图（不影响扫描数据）"
                      className={cn(
                        'inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-sm',
                        'text-ink-ghost transition-ui duration-fast',
                        'hover:bg-signal-risk/10 hover:text-signal-risk',
                        'disabled:cursor-not-allowed disabled:opacity-40',
                      )}
                    >
                      <Trash2 size={11} strokeWidth={1.75} aria-hidden="true" />
                    </button>
                  </div>
                )
              })
            )}
          </div>
        </div>
      )}
    </div>
  )
}
