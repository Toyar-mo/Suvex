import { Loader2, RefreshCw, TriangleAlert } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'

import { listRemoteCompetitions, type Ctf2RemoteCompetition } from '@/data/ctf2'
import { errorMessage } from '@/lib/api'
import { cn } from '@/lib/cn'

/*
  ─────────────────────────────────────────────────────────────
  选择要同步的赛事
  ─────────────────────────────────────────────────────────────

  ★ 为什么要有这个弹窗

  「同步题目」默认只同步**练习集**（平台的开放接口没有"列出练习集题目"
  的端点，练习集那部分同步回来只有元信息）。赛事那条链路是完整的
  （stages → challenges 都在），但它要求**逐场指定** —— 而"可选赛事"
  不能来自本地库：本地库里的赛事正是上一次同步的结果，第一次用的时候
  它是空的。所以这个弹窗直接问平台要列表（`GET /ctf2/competitions/remote`）。

  ⛔ 一条必须写在界面上的事实

  赛事题目同步回来**只能查看题面与附件，不能在这里解题和提交** ——
  CTF2 的开放接口没有开放赛事侧的提交与起环境（实测那 8 条路径全部
  返回 404）。不写清楚的话，用户会以为同步完就能做题，然后吃一个 409。

  ⚠️ 也因为它需要真的打平台接口，那个端点要 **scan** 权限（不是 read）。
*/

export function CompetitionPicker({
  open,
  busy = false,
  onConfirm,
  onCancel,
}: {
  open: boolean
  /** 外部正在同步中（确认之后、弹窗关闭之前） */
  busy?: boolean
  onConfirm: (remoteIds: string[]) => void
  onCancel: () => void
}) {
  const [items, setItems] = useState<Ctf2RemoteCompetition[] | null>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [error, setError] = useState<string | null>(null)
  const cancelRef = useRef<HTMLButtonElement>(null)

  /*
    ⚠️ 这里**没有** `loading` 这个 state，它是**派生**的。

    原来的写法是 `load()` 里先 `setLoading(true)` —— 而 `load` 是在
    effect 里被调用的，于是 React 19 的 `react-hooks/set-state-in-effect`
    规则会报"在 effect 里同步 setState 会引发级联渲染"（实测撞到）。
    把"还没结果、也还没出错"当成加载中，既去掉了那次同步 setState，
    也少了一个可能与 `items` 不同步的 state。
  */
  const loading = items === null && error === null

  /*
    ★ 两个 setState 都包在 `useCallback` 里，**并且只作为回调传给
    `.then()/.catch()`** —— 这样 effect 体内就没有同步 setState 了
    （那条 React 19 规则禁的正是这个）。

    ⚠️ 另外**不能**在渲染期写 ref（`busyRef.current = busy` 那种写法会撞
    `react-hooks/refs`：Cannot access refs during render）。本来想用
    ref 让 Esc 在同步中不生效，但同步一旦开始这个弹窗就已经关掉了
    （见页面里 `onConfirm` 先关弹窗再同步），所以那个判断本来也没有用武之地。
  */
  const applyRows = useCallback((rows: Ctf2RemoteCompetition[]) => {
    setItems(rows)
    setError(null)
    // 默认勾上第一场：多数场景就是"同步我正在打的那场"，
    // 而全选会让一次同步变成几十场外部请求。
    setSelected(rows.length ? new Set([rows[0].remoteId]) : new Set())
  }, [])

  const applyError = useCallback((err: unknown) => {
    setError(errorMessage(err))
    setItems(null)
  }, [])

  /*
    ⚠️ 每次打开都重新拉一次（而不是缓存）：
    平台上的赛事列表会变（新的比赛开始了），而一次陈旧的选择会让用户
    同步到一场已经结束的赛事 —— 他看到的提示却是"同步完成"。
  */
  useEffect(() => {
    if (!open) return
    void listRemoteCompetitions().then(applyRows).catch(applyError)
    cancelRef.current?.focus()

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onCancel()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [open, applyRows, applyError, onCancel])

  if (!open) return null

  const toggle = (remoteId: string) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(remoteId)) next.delete(remoteId)
      else next.add(remoteId)
      return next
    })
  }

  const empty = !loading && !error && items !== null && items.length === 0

  return (
    <div
      onClick={() => !busy && onCancel()}
      className="fixed inset-0 z-50 flex items-center justify-center bg-canvas/70 p-gutter backdrop-blur-sm"
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="competition-picker-title"
        onClick={(e) => e.stopPropagation()}
        className="flex max-h-[80vh] w-full max-w-xl animate-rise-in flex-col gap-panel rounded-md border border-line bg-surface-overlay p-panel shadow-overlay"
      >
        <header className="flex flex-col gap-1">
          <h2 id="competition-picker-title" className="text-sm text-ink">
            同步赛事题目
          </h2>
          <p className="text-xs text-ink-muted">
            这里列出的是<strong className="text-ink">平台上</strong>的赛事（即时查询，不是本地已有的）。
            勾选后会把该赛事的阶段与题目同步到本地。
          </p>
        </header>

        {/* ── 一条必须说清楚的事实 ── */}
        <p className="flex items-start gap-1.5 rounded-sm border border-signal-warn/40 bg-signal-warn/[0.06] px-2.5 py-2 text-2xs text-ink-muted">
          <TriangleAlert
            size={12}
            strokeWidth={2}
            className="mt-0.5 shrink-0 text-signal-warn"
            aria-hidden="true"
          />
          <span>
            赛事题目同步回来后<strong className="text-ink">只能查看题面与附件，不能在这里解题和提交</strong>
            —— CTF2 的开放接口没有提供赛事侧的提交与起环境接口。另外，团队赛还要求你的账号
            <strong className="text-ink">先加入对应战队</strong>，否则那场会同步失败（失败原因会写在结果里）。
          </span>
        </p>

        {/* ── 列表 ── */}
        <div className="min-h-24 flex-1 overflow-y-auto rounded-sm border border-line bg-surface-sunken">
          {loading && (
            <p className="flex items-center justify-center gap-2 p-6 text-xs text-ink-muted">
              <Loader2 size={13} strokeWidth={2} className="animate-spin" aria-hidden="true" />
              正在向 CTF2 查询赛事…
            </p>
          )}

          {!loading && error && (
            <div className="flex flex-col items-center gap-2 p-6">
              <p className="text-xs text-signal-risk">读取失败：{error}</p>
              <button
                type="button"
                onClick={() => {
                  // 回到"加载中"（派生状态）再拉一次。
                  // ⚠️ 这两次 setState 在**事件处理器**里 —— 与 effect 里
                  //    同步 setState 是两回事，不触发那条规则。
                  setItems(null)
                  setError(null)
                  void listRemoteCompetitions().then(applyRows).catch(applyError)
                }}
                className="inline-flex h-7 items-center gap-1.5 rounded-sm border border-line px-2.5 text-2xs text-ink-muted transition-ui duration-fast hover:border-line-strong hover:text-ink"
              >
                <RefreshCw size={11} strokeWidth={2} aria-hidden="true" />
                重试
              </button>
            </div>
          )}

          {empty && (
            <p className="p-6 text-center text-xs text-ink-muted">
              平台上没有这场账号可见的赛事。
            </p>
          )}

          {!loading &&
            !error &&
            (items ?? []).map((item) => (
              <label
                key={item.remoteId}
                className={cn(
                  'flex cursor-pointer items-start gap-2.5 border-b border-line/60 px-3 py-2.5 last:border-b-0',
                  'transition-ui duration-fast hover:bg-surface',
                  busy && 'cursor-not-allowed opacity-60',
                )}
              >
                <input
                  type="checkbox"
                  checked={selected.has(item.remoteId)}
                  disabled={busy}
                  onChange={() => toggle(item.remoteId)}
                  /*
                    原来写的是 `accent-[var(--accent)]`，但全站只定义
                    `--suvex-accent`（`src/index.css:46` 起），`--accent` 从未定义
                    → 解析成无效值，浏览器回落成默认蓝，两套主题下都不是主色。
                    统一走 `accent-accent`（= `rgb(var(--suvex-accent))`）。
                  */
                  className="mt-0.5 h-3.5 w-3.5 shrink-0 accent-accent"
                />
                <span className="flex min-w-0 flex-col gap-0.5">
                  <span className="truncate text-xs text-ink">{item.name}</span>
                  <span className="text-2xs text-ink-faint">
                    {item.startAt ? item.startAt.slice(0, 10) : '时间未知'}
                    {item.endAt ? ` → ${item.endAt.slice(0, 10)}` : ''}
                    {item.status ? ` · ${item.status}` : ''}
                  </span>
                </span>
              </label>
            ))}
        </div>

        {/* ── 动作 ── */}
        <footer className="flex items-center justify-between gap-panel">
          <button
            type="button"
            disabled={busy || loading || (items ?? []).length === 0}
            onClick={() =>
              setSelected(
                selected.size === (items ?? []).length
                  ? new Set()
                  : new Set((items ?? []).map((item) => item.remoteId)),
              )
            }
            className={cn(
              'text-2xs text-ink-muted underline transition-ui duration-fast hover:text-ink',
              (busy || loading || (items ?? []).length === 0) && 'cursor-not-allowed opacity-50',
            )}
          >
            {selected.size === (items ?? []).length && selected.size > 0 ? '取消全选' : '全选'}
          </button>

          <div className="flex items-center gap-2">
            <button
              ref={cancelRef}
              type="button"
              disabled={busy}
              onClick={onCancel}
              className={cn(
                'inline-flex h-8 items-center rounded-sm border border-line px-3 text-xs text-ink-muted',
                'transition-ui duration-fast hover:border-line-strong hover:text-ink',
                busy && 'cursor-not-allowed opacity-50',
              )}
            >
              取消
            </button>
            <button
              type="button"
              disabled={busy || selected.size === 0}
              onClick={() => onConfirm([...selected])}
              className={cn(
                'press inline-flex h-8 items-center gap-1.5 rounded-sm bg-accent px-3 text-xs font-medium text-canvas',
                'transition-ui duration-fast hover:bg-accent-strong',
                (busy || selected.size === 0) && 'cursor-not-allowed opacity-50',
              )}
            >
              {busy && <Loader2 size={12} strokeWidth={2} className="animate-spin" aria-hidden="true" />}
              同步选中的 {selected.size} 场
            </button>
          </div>
        </footer>
      </div>
    </div>
  )
}

export default CompetitionPicker
