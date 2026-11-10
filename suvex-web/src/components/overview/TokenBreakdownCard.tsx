import type { QualityCostMetrics } from '@/data/quality'

function compact(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`
  return String(value)
}

/**
 * LLM Token 消耗（学 ARTEX 总览的 Token 面板）。
 *
 * 只显示**有记录**的调用：`cost` 桶来自 `KernelSolveUsage`，其中
 * `usage_reported=false` 的行 token 记 0，而 0 会被单独计数成
 * “未上报调用”。不把两者混成一个数字 —— 与后端同一条纪律。
 */
export function TokenBreakdownCard({ cost }: { cost?: QualityCostMetrics }) {
  const total = cost?.totalTokens ?? 0
  const prompt = cost?.promptTokens ?? 0
  const completion = cost?.completionTokens ?? 0
  const calls = cost?.calls ?? 0
  const unreported = cost?.unreportedCalls ?? 0
  const usd = cost?.usd ?? 0
  const unpriced = cost?.unpricedTokens ?? 0
  const ratio = (value: number) => (total > 0 ? Math.min(100, (value / total) * 100) : 0)

  return (
    <section className="rounded-lg border border-line bg-surface p-panel">
      <div className="flex items-start justify-between">
        <div>
          <h3 className="text-sm font-medium text-ink">LLM Token 消耗</h3>
          <p className="mt-0.5 text-2xs text-ink-muted">解题侧记录；0 与“未上报”分开计数。</p>
        </div>
        <span className="text-2xs text-ink-faint">{calls} 次调用</span>
      </div>

      <div className="mt-3 flex items-baseline gap-1.5">
        <span className="tabular text-3xl font-medium text-ink">{compact(total)}</span>
        <span className="text-2xs text-ink-faint">tokens</span>
      </div>

      <dl className="mt-3 flex flex-col gap-2 text-2xs">
        <div className="flex items-center justify-between gap-3">
          <dt className="text-ink-muted">输入</dt>
          <dd className="tabular text-ink">{compact(prompt)}</dd>
        </div>
        <div className="h-1 overflow-hidden rounded-pill bg-surface-raised">
          <div className="h-full rounded-pill bg-accent" style={{ width: `${ratio(prompt)}%` }} />
        </div>
        <div className="flex items-center justify-between gap-3">
          <dt className="text-ink-muted">输出</dt>
          <dd className="tabular text-ink">{compact(completion)}</dd>
        </div>
        <div className="h-1 overflow-hidden rounded-pill bg-surface-raised">
          <div className="h-full rounded-pill bg-signal-ok" style={{ width: `${ratio(completion)}%` }} />
        </div>
      </dl>

      <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-2xs text-ink-faint">
        <span>估算 ${usd < 0.01 ? usd.toFixed(4) : usd.toFixed(2)}</span>
        {unreported > 0 && <span>· {unreported} 次未上报</span>}
        {unpriced > 0 && <span>· {compact(unpriced)} token 未定价</span>}
        {!cost && <span>质量数据未加载</span>}
      </div>
    </section>
  )
}
