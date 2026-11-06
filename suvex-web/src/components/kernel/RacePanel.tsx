import { useCallback, useEffect, useRef, useState } from 'react'
import { Flag, Loader2, Play, Square, Trophy } from 'lucide-react'

import { Button } from '@/components/ui/Button'
import { cn } from '@/lib/cn'
import {
  getRaceStatus,
  raceErrorMessage,
  reportOf,
  startRace,
  stopRace,
  RACE_STAGE_LABEL,
  type RaceStage,
  type RaceStatus,
} from '@/data/ctf-race'

/**
 * 「赛事托管」面板 —— 一整场比赛交给系统自动跑。
 *
 * ## 它比「一道道手动解」多做了什么（后端 `ctf_race` 包）
 *
 * 1. **多模型赛跑**：同一道题，`models` 里每个模型各开一个 solver，
 *    谁先拿到 flag 谁赢，其余立刻取消 —— 避免单模型卡死整题。
 * 2. **自动救卡住**：某个 solver 写完了却没解出来，会把它兄弟的中间
 *    发现（`message_bus`）喂回去重试（`bump`），且**有次数上限**防烧钱。
 * 3. **整场自动托管**：自动发现新题、跳过平台上已解/未开放的题、
 *    按并发上限排队 —— 你不用一题一题点。
 * 4. **成本可见**：实时显示总花费与 token。达到预算上限自动停。
 *
 * ## 为什么「停止」不是瞬间的
 *
 * 一个 solver 此刻可能正卡在一条几十秒的 `run_shell` 里，后端没法从
 * 半路把它掐断（同 CTF2 容器工具的老问题）。点停止只是让它**跑完当前
 * 这一步就收手**。所以按钮文案是「请求停止」而不是「停止」，并在
 * 已请求后禁用它 —— 否则用户会反复点、以为点了没反应。
 *
 * ## 为什么是轮询而不是 SSE
 *
 * 与 `ChatPage` 的服务端进度同源：赛事托管是**服务端在后台跑**，
 * 前端只订阅不了它的内部事件。这里用 2s 轮询 `GET /ctf-race/status`，
 * 只有在 `running` 时才轮询，不跑时零请求。
 */
const POLL_MS = 2000

const STAGE_TONE: Record<RaceStage, string> = {
  pending: 'text-ink-muted',
  running: 'text-accent',
  solved: 'text-emerald-600',
  failed: 'text-signal-risk',
  skipped: 'text-ink-muted',
}

export function RacePanel() {
  const [status, setStatus] = useState<RaceStatus | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [models, setModels] = useState('deepseek-v4.1-flash')
  const [maxConcurrent, setMaxConcurrent] = useState(2)
  const [maxBudget, setMaxBudget] = useState<string>('')
  const [stopRequested, setStopRequested] = useState(false)
  const aliveRef = useRef(true)

  const running = status?.running ?? false

  const refresh = useCallback(async () => {
    try {
      const s = await getRaceStatus()
      if (aliveRef.current) setStatus(s)
    } catch {
      /* 轮询失败静默，下一轮再试 */
    }
  }, [])

  // 初次加载拿一次状态（可能服务端已在跑）。
  // setState 发生在 getRaceStatus() 的 `.then` 回调里（异步边界），
  // 不在 effect 体内的同步执行路径上 —— 因此不触发 react-hooks/set-state-in-effect。
  useEffect(() => {
    aliveRef.current = true
    getRaceStatus()
      .then((s) => {
        if (aliveRef.current) setStatus(s)
      })
      .catch(() => {
        /* 首屏拿不到就等轮询/手动刷新 */
      })
    return () => {
      aliveRef.current = false
    }
  }, [])

  // running 时轮询
  useEffect(() => {
    if (!running) return
    const timer = window.setInterval(() => {
      void refresh()
    }, POLL_MS)
    return () => window.clearInterval(timer)
  }, [running, refresh])

  // 一旦不再 running，就不再显示「已请求停止」。
  // 注意：不写成 `useEffect(() => { if (!running) setStopRequested(false) }, [running])`
  // —— 那是"effect 里同步 setState"，会触发级联渲染（eslint 也拦）。
  // 改成渲染期派生态：只有 running 时 stopRequested 才有意义。
  const effectiveStopRequested = running && stopRequested

  async function onStart() {
    setBusy(true)
    setError(null)
    try {
      const modelList = models
        .split(/[,，\s]+/)
        .map((m) => m.trim())
        .filter(Boolean)
      const budget = maxBudget.trim() === '' ? null : Number(maxBudget)
      if (budget !== null && (!Number.isFinite(budget) || budget < 0)) {
        setError('预算必须是 ≥0 的数字（留空=不限）')
        return
      }
      const s = await startRace({
        models: modelList.length ? modelList : undefined,
        maxConcurrent,
        maxBudgetUsd: budget,
      })
      setStatus(s)
      setStopRequested(false)
    } catch (err) {
      setError(raceErrorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  async function onStop() {
    setStopRequested(true)
    try {
      const s = await stopRace()
      setStatus(s)
    } catch (err) {
      setError(raceErrorMessage(err))
    }
  }

  const report = reportOf(status)
  const rows = report
    ? Object.values(report.by_challenge).sort((a, b) => {
        const order: Record<RaceStage, number> = {
          running: 0,
          pending: 1,
          failed: 2,
          solved: 3,
          skipped: 4,
        }
        return (order[a.stage] ?? 9) - (order[b.stage] ?? 9)
      })
    : []
  const totalCost = status?.cost?.totalCostUsd

  return (
    <section
      className="rounded-lg border border-line bg-surface-raised p-4"
      data-race-panel
    >
      <header className="mb-3 flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Trophy className="size-4 text-accent" aria-hidden />
          <h2 className="text-sm font-semibold">赛事托管</h2>
          <span className="text-xs text-ink-muted">
            整场比赛自动跑：多模型赛跑 · 卡住自动重试 · 成本可见
          </span>
        </div>
        {running ? (
          <Button
            variant="danger"
            size="sm"
            onClick={onStop}
            disabled={effectiveStopRequested}
            data-race-stop
          >
            {effectiveStopRequested ? (
              <>
                <Loader2 className="size-3.5 animate-spin" aria-hidden />
                正在停止…
              </>
            ) : (
              <>
                <Square className="size-3.5" aria-hidden />
                请求停止
              </>
            )}
          </Button>
        ) : null}
      </header>

      {/* 起跑表单（未跑时显示） */}
      {!running ? (
        <div className="mb-3 grid grid-cols-1 gap-2 sm:grid-cols-3" data-race-form>
          <label className="flex flex-col gap-1 text-xs">
            <span className="text-ink-muted">赛跑模型（逗号分隔，同 provider 换 model 名）</span>
            <input
              className="rounded border border-line bg-surface px-2 py-1 text-sm"
              value={models}
              onChange={(e) => setModels(e.target.value)}
              disabled={busy}
            />
          </label>
          <label className="flex flex-col gap-1 text-xs">
            <span className="text-ink-muted">并发题数</span>
            <input
              type="number"
              min={1}
              max={8}
              className="rounded border border-line bg-surface px-2 py-1 text-sm"
              value={maxConcurrent}
              onChange={(e) => setMaxConcurrent(Number(e.target.value) || 1)}
              disabled={busy}
            />
          </label>
          <label className="flex flex-col gap-1 text-xs">
            <span className="text-ink-muted">预算上限 USD（留空=不限）</span>
            <input
              inputMode="decimal"
              className="rounded border border-line bg-surface px-2 py-1 text-sm"
              value={maxBudget}
              onChange={(e) => setMaxBudget(e.target.value)}
              disabled={busy}
            />
          </label>
          <div className="sm:col-span-3">
            <Button variant="primary" size="sm" onClick={onStart} disabled={busy} data-race-start>
              <Play className="size-3.5" aria-hidden />
              {busy ? '启动中…' : '开始整场托管'}
            </Button>
          </div>
        </div>
      ) : (
        <div
          className={cn(
            'mb-3 rounded border px-3 py-2 text-xs',
            effectiveStopRequested
              ? 'border-signal-risk/40 bg-signal-risk/5 text-signal-risk'
              : 'border-accent/40 bg-accent/5 text-accent',
          )}
          data-race-running
        >
          {effectiveStopRequested
            ? '已请求停止：当前正在跑的步骤没法中途打断，等它返回后这一整场就会收手。不用再点停止。'
            : `赛事托管进行中（第 ${report?.rounds ?? 0} 轮）· 已解出 ${report?.solved ?? 0}/${report?.total ?? 0}`}
        </div>
      )}

      {error ? (
        <p className="mb-2 text-xs text-signal-risk" data-race-error>
          {error}
        </p>
      ) : null}

      {/* 进度表 */}
      {rows.length > 0 ? (
        <div className="overflow-hidden rounded border border-line">
          <table className="w-full text-xs">
            <thead className="bg-surface text-ink-muted">
              <tr>
                <th className="px-2 py-1 text-left font-medium">题目</th>
                <th className="px-2 py-1 text-left font-medium">状态</th>
                <th className="px-2 py-1 text-left font-medium">模型</th>
                <th className="px-2 py-1 text-left font-medium">Flag / 备注</th>
              </tr>
            </thead>
            <tbody data-race-rows>
              {rows.map((r) => (
                <tr key={r.remote_id} className="border-t border-line">
                  <td className="px-2 py-1">{r.title || r.remote_id}</td>
                  <td className={cn('px-2 py-1 font-medium', STAGE_TONE[r.stage])}>
                    {RACE_STAGE_LABEL[r.stage] ?? r.stage}
                  </td>
                  <td className="px-2 py-1 text-ink-muted">{r.winner_model ?? '—'}</td>
                  <td className="px-2 py-1">
                    {r.flag ? (
                      <span className="inline-flex items-center gap-1 text-emerald-600">
                        <Flag className="size-3" aria-hidden />
                        {r.flag}
                      </span>
                    ) : r.last_error ? (
                      <span className="text-signal-risk">{r.last_error}</span>
                    ) : (
                      <span className="text-ink-muted">—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      {/* 底部：成本 + 收场原因 */}
      <footer className="mt-2 flex flex-wrap items-center gap-3 text-xs text-ink-muted">
        {typeof totalCost === 'number' ? (
          <span data-race-cost>
            累计成本 ${totalCost.toFixed(4)}
            {typeof status?.cost?.totalTokens === 'number'
              ? ` · ${status.cost.totalTokens} tokens`
              : ''}
          </span>
        ) : null}
        {report?.stopped_reason ? (
          <span data-race-stopped>收场原因：{report.stopped_reason}</span>
        ) : null}
        {report?.elapsed_seconds ? <span>用时 {Math.round(report.elapsed_seconds)}s</span> : null}
      </footer>
    </section>
  )
}

export default RacePanel
