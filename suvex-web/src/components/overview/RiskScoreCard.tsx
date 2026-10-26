import { cn } from '@/lib/cn'
import type { RiskLevel, SeverityBreakdownEntry } from '@/data/overview'
import type { Severity } from '@/data/findings'

/**
 * 风险分卡（v0.95，学 CyberStrikeAI 的 `renderSeverityInsights`）。
 *
 * ## 这个数字到底在说什么 —— 界面上必须说清楚
 *
 * 它只算**还没处置完**的发现。所以：
 *   - 全修完之后它归零（这是修复的正反馈，也是它存在的理由）；
 *   - 与「本周新增高危」不是一回事（那个是增量，这个是存量）；
 *   - 与下面的「严重级别分布」不是一回事（那个含已修复项）。
 *
 * 三者口径不同却都出现在同一屏 —— 不写清就会变成"三个数字互相矛盾"。
 *
 * ## 为什么等级文字与进度条要一起显示
 *
 * 只用进度条：10 分与 30 分在 0-100 的条上差 20%，肉眼几乎看不出，
 * 而这个差别是「中」与「高」。
 * 只用文字：用户看不到"离下一档还有多远"。
 * 两者一起给，并把**下一档的阈值**写出来，这个数字才可行动。
 */

export interface RiskScoreCardProps {
  score: number
  level: RiskLevel
  total: number
  breakdown: SeverityBreakdownEntry[]
  className?: string
}

/**
 * 等级 → 文案与颜色。
 *
 * ⚠️ 五档**必须都有**，包括 `safe`。漏掉 safe 的写法（比如只写四档、
 *    然后 `META[level] ?? META.high` 兜底）会让"没有风险"显示成"高危"——
 *    这是最坏的一种兜底方向。
 */
const LEVEL_META: Record<RiskLevel, { label: string; text: string; bar: string; note: string }> = {
  safe: {
    label: '无待处置',
    text: 'text-signal-ok',
    bar: 'bg-signal-ok',
    note: '没有待处置的发现。已修复与误报的项不计入这个分数。',
  },
  low: {
    label: '低',
    text: 'text-ink-muted',
    bar: 'bg-ink-ghost',
    note: '只有零星的低危或中危项，可以攒着批量处理。',
  },
  medium: {
    label: '中',
    text: 'text-signal-warn',
    bar: 'bg-signal-warn',
    note: '有待处置项积压，建议排进本轮迭代。',
  },
  high: {
    label: '高',
    text: 'text-signal-risk',
    bar: 'bg-signal-risk',
    note: '积压已较重，优先处置严重与高危项。',
  },
  severe: {
    label: '极高',
    text: 'text-signal-risk',
    bar: 'bg-signal-risk',
    note: '待处置项的数量或等级已超出可接受范围，应立即处理。',
  },
}

/** 严重度在条形里的颜色 —— 与 SeverityChart 共用同一套语义色。 */
const SEVERITY_BAR: Record<Severity, string> = {
  critical: 'bg-signal-risk',
  high: 'bg-signal-risk/70',
  medium: 'bg-signal-warn',
  low: 'bg-signal-info/60',
  info: 'bg-line-strong',
}

const SEVERITY_LABEL: Record<Severity, string> = {
  critical: '严重',
  high: '高危',
  medium: '中危',
  low: '低危',
  info: '信息',
}

export function RiskScoreCard({
  score,
  level,
  total,
  breakdown,
  className,
}: RiskScoreCardProps) {
  // ★ 认不出的等级**退回 safe 的视觉、但文案不撒谎**：显示原始等级字符串。
  //   兜底到 high 会让一次后端新增档位变成一次"全站告警"。
  const meta = LEVEL_META[level]
  const label = meta?.label ?? level

  // 进度条宽度：分数直接当百分比（后端权重设计成 100 分以内）。
  // ⚠️ `Math.min(100, ...)` 是必需的：分数可以超过 100（比如 20 个严重），
  //    不夹住的话 `width: 200%` 会让条溢出卡片。
  const width = Math.max(0, Math.min(100, score))

  // 只显示非零档 —— 这里与「严重级别分布」相反：
  // 那张图要 5 档齐全（缺柱子会被读成漏了），这里是构成比例，
  // 0 的档位写出来只是噪音，而总条数已经写在标题上了。
  const present = breakdown.filter((entry) => entry.count > 0)

  return (
    <section
      aria-labelledby="risk-score-title"
      className={cn('flex flex-col rounded-md border border-line bg-surface p-panel', className)}
    >
      <div className="flex items-baseline justify-between gap-tight">
        <h2 id="risk-score-title" className="text-sm font-medium text-ink">
          待处置风险
        </h2>
        <span className="mono-ui tabular text-ink-faint" data-numeric>
          {total} 项
        </span>
      </div>

      <div className="mt-panel flex items-baseline gap-tight">
        <span className={cn('tabular text-3xl font-medium', meta?.text ?? 'text-ink')} data-numeric>
          {level === 'safe' ? '0' : score}
        </span>
        <span className={cn('text-sm font-medium', meta?.text ?? 'text-ink-muted')}>{label}</span>
      </div>

      {/* 进度条。aria-hidden —— 上面的数字已经说完了，读屏器不需要再听一遍百分比 */}
      <div
        className="mt-tight h-1.5 w-full overflow-hidden rounded-full bg-canvas"
        aria-hidden="true"
      >
        <div
          className={cn('h-full rounded-full transition-ui duration-base', meta?.bar ?? 'bg-ink-ghost')}
          style={{ width: `${width}%` }}
        />
      </div>

      {present.length > 0 ? (
        <>
          {/* 构成条：一眼看出这 20 分是"20 个低危"还是"2 个严重" */}
          <div className="mt-tight flex h-1 w-full overflow-hidden rounded-full" aria-hidden="true">
            {present.map((entry) => (
              <div
                key={entry.severity}
                className={SEVERITY_BAR[entry.severity] ?? 'bg-line-strong'}
                style={{ width: `${(entry.count / total) * 100}%` }}
              />
            ))}
          </div>
          <ul className="mt-tight flex flex-wrap gap-x-3 gap-y-1">
            {present.map((entry) => (
              <li key={entry.severity} className="flex items-center gap-1 text-2xs text-ink-muted">
                <span
                  className={cn('size-1.5 rounded-full', SEVERITY_BAR[entry.severity] ?? 'bg-line-strong')}
                  aria-hidden="true"
                />
                {SEVERITY_LABEL[entry.severity] ?? entry.severity}
                <span className="tabular text-ink-faint" data-numeric>
                  {entry.count}
                </span>
              </li>
            ))}
          </ul>
        </>
      ) : null}

      <p className="mt-tight text-2xs leading-relaxed text-ink-faint">{meta?.note ?? ''}</p>
    </section>
  )
}

export default RiskScoreCard
