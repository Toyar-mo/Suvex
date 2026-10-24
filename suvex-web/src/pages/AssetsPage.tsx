import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Layers, RefreshCw, Search } from 'lucide-react'
import { PageHeader } from '@/components/layout/AppShell'
import EmptyState from '@/components/ui/EmptyState'
import ErrorState from '@/components/ui/ErrorState'
import { Button } from '@/components/ui/Button'
import { SkeletonRows } from '@/components/ui/Skeleton'
import { cn } from '@/lib/cn'
import {
  ASSET_HEALTH_META,
  assetHealth,
  assetMatchesQuery,
  type Asset,
  type AssetHealth,
} from '@/data/asset'
import { loadAssets, useAssetsState } from '@/data/assets-store'

/**
 * 资产视图 —— 「我有哪些目标，每个现在能不能信」。
 *
 * ## 为什么这一页必须存在（而不是把漏洞页按 target 分组就够了）
 *
 * 漏洞页回答的是「我发现了什么」。但渗透测试里有一类问题它**永远答不上来**：
 * 「有哪些机器我根本没检查过」「哪台机器上次跑完是三个月前」。
 * 这两个问题的答案不在任何一条漏洞里 —— 它们在**扫描记录**里。
 *
 * 最危险的形态是「看起来干净」：一个目标扫了 5 次全挂，漏洞数是 0。
 * 在漏洞页它和「扫了 5 次确实什么都没发现」长得一模一样。
 * 这一页的主要职责就是把这两者分开（见 `assetHealth` 的档位）。
 *
 * ## 排序：把「不能信」的排前面
 *
 * 默认不按目标名字母序，按**风险**：
 *   带开着的高危 → 从未跑完 → 连续失败 → 从未扫描 → 结论陈旧 → 其余
 * 同档内再按「上次拿结论的时间」升序（越久越靠前）。
 * 名字序只作为最后的确定性 tie-break。
 *
 * 「已修复」不参与提权：一个修干净了的目标不该和带病的排在一起。
 */

type HealthFilter = 'all' | AssetHealth

/**
 * 空列表的稳定常量。
 *
 * 写 `state.items ?? []` 每次渲染都会产生一个新数组，导致下游 `useMemo`
 * 的依赖恒变、每次重渲都重算排序。这里用一个模块级空数组把引用固定住。
 */
const EMPTY_ASSETS: Asset[] = []

/** 风险档位 —— 数字越小越靠前。 */
const HEALTH_RANK: Record<AssetHealth, number> = {
  'never-completed': 0,
  failing: 1,
  'never-scanned': 2,
  stale: 3,
  ok: 4,
}

/**
 * 排序用的「上次拿结论」时间戳。
 *
 * 从未成功 → `-Infinity`（最靠前，因为它最需要被检查）。
 * 时间戳非法 → 同样按最靠前处理，不能静默沉底。
 */
function lastConclusionTs(asset: Asset): number {
  if (asset.lastCompletedAt === null) return Number.NEGATIVE_INFINITY
  const ts = new Date(asset.lastCompletedAt).getTime()
  return Number.isFinite(ts) ? ts : Number.NEGATIVE_INFINITY
}

function compareAssets(a: Asset, b: Asset): number {
  // ① 带开着的高危永远排最前 —— 这是唯一"已经在出血"的那种
  const aBleeding = a.openCritical > 0 || a.openHigh > 0 ? 1 : 0
  const bBleeding = b.openCritical > 0 || b.openHigh > 0 ? 1 : 0
  if (aBleeding !== bBleeding) return bBleeding - aBleeding

  // ② 健康度档位
  const aHealth = assetHealth(a)
  const bHealth = assetHealth(b)
  if (aHealth !== bHealth) return HEALTH_RANK[aHealth] - HEALTH_RANK[bHealth]

  // ③ 同档内：越久没拿到结论越靠前
  const aTs = lastConclusionTs(a)
  const bTs = lastConclusionTs(b)
  if (aTs !== bTs) return aTs - bTs

  // ④ 确定性 tie-break —— 没有它，同样两条数据每次刷新可能换位置
  return a.target.localeCompare(b.target)
}

/** 「3 天前」「刚刚」。 */
function relativeTime(iso: string | null, now: number): string {
  if (iso === null) return '从未'
  const ts = new Date(iso).getTime()
  if (!Number.isFinite(ts)) return '—'

  const diff = now - ts
  if (diff < 0) return '刚刚'
  const minutes = Math.floor(diff / 60000)
  if (minutes < 1) return '刚刚'
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} 小时前`
  const days = Math.floor(hours / 24)
  if (days < 30) return `${days} 天前`
  const months = Math.floor(days / 30)
  if (months < 12) return `${months} 个月前`
  return `${Math.floor(months / 12)} 年前`
}

function HealthBadge({ health }: { health: AssetHealth }) {
  const meta = ASSET_HEALTH_META[health]
  return (
    <span className="inline-flex items-center gap-tight whitespace-nowrap text-sm">
      <span
        className={cn('h-1.5 w-1.5 shrink-0 rounded-pill', meta.dot)}
        aria-hidden="true"
      />
      <span className={meta.className} title={meta.hint}>
        {meta.label}
      </span>
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
    <div className="rounded-md border border-line bg-surface p-panel" title={hint}>
      <p className="text-xs text-ink-muted">{label}</p>
      <p
        className={cn(
          'mt-tight text-xl font-medium tabular-nums',
          tone === 'default' && 'text-ink',
          tone === 'warn' && 'text-signal-warn',
          tone === 'risk' && 'text-signal-risk',
        )}
      >
        {value}
      </p>
    </div>
  )
}

export default function AssetsPage() {
  const state = useAssetsState()
  const [query, setQuery] = useState('')
  const [healthFilter, setHealthFilter] = useState<HealthFilter>('all')
  const [now, setNow] = useState(() => Date.now())

  // 首次进入时拉一次；之后靠「刷新」按钮和切回页面时拉。
  const loadedOnce = useRef(false)
  useEffect(() => {
    if (loadedOnce.current) return
    loadedOnce.current = true
    if (state.status === 'idle') void loadAssets()
  }, [state.status])

  // 相对时间要跟着走，但不是每秒重渲染：30 秒一次足够，
  // 而且 `now` 只影响文案，不参与排序键（排序用绝对值）。
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 30_000)
    return () => window.clearInterval(timer)
  }, [])

  const refresh = useCallback(() => {
    void loadAssets({ silent: state.items !== null })
  }, [state.items])

  // `state.items ?? []` 每次渲染都是新数组引用，会让下面 useMemo 的依赖恒变。
  // 用常量兜底 + useMemo 把引用稳定住（`EMPTY_ASSETS` 是模块级单例）。
  const items = useMemo(() => state.items ?? EMPTY_ASSETS, [state.items])

  const filtered = useMemo(() => {
    return items
      .filter((asset) => healthFilter === 'all' || assetHealth(asset) === healthFilter)
      .filter((asset) => assetMatchesQuery(asset, query))
      .sort(compareAssets)
  }, [items, healthFilter, query])

  // 首次加载 —— 只有真的还没数据时才显示骨架屏
  if (state.status === 'loading' && state.items === null) {
    return (
      <>
        <PageHeader navId="assets" />
        <div className="px-gutter py-bay">
          <SkeletonRows rows={5} />
        </div>
      </>
    )
  }

  if (state.status === 'error' && state.items === null) {
    return (
      <>
        <PageHeader navId="assets" />
        <div className="px-gutter py-bay">
          <ErrorState error={state.error} onRetry={refresh} title="资产列表加载失败" />
        </div>
      </>
    )
  }

  const summary = state.summary

  return (
    <>
      <PageHeader navId="assets" />

      <div className="px-gutter py-bay">
        {/* ── 统计条 ─────────────────────────────────────────── */}
        {summary && (
          <div className="mb-bay grid grid-cols-2 gap-panel sm:grid-cols-3 lg:grid-cols-5">
            <SummaryCard label="目标总数" value={summary.targetCount} />
            <SummaryCard
              label="带开着的高危"
              value={summary.targetsWithOpenHigh}
              hint="机器台数，不是漏洞条数"
              tone={summary.targetsWithOpenHigh > 0 ? 'risk' : 'default'}
            />
            <SummaryCard
              label="从未扫描"
              value={summary.neverScanned}
              hint="只有历史导入的漏洞，一次都没扫过"
              tone={summary.neverScanned > 0 ? 'warn' : 'default'}
            />
            <SummaryCard
              label="从未跑完"
              value={summary.neverCompleted}
              hint="扫过但没有一次出结论 —— 「看起来干净」只是因为没被检查过"
              tone={summary.neverCompleted > 0 ? 'risk' : 'default'}
            />
            <SummaryCard label="未处置漏洞" value={summary.openFindingCount} />
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
              placeholder="按目标筛选…"
              aria-label="按目标筛选资产"
              className="w-full rounded-sm border border-line bg-surface py-1.5 pl-8 pr-2.5 text-sm text-ink placeholder:text-ink-muted focus:border-accent focus:outline-none"
            />
          </div>

          <div className="flex flex-wrap gap-tight">
            {(
              [
                ['all', '全部'],
                ['never-completed', '从未跑完'],
                ['failing', '连续失败'],
                ['never-scanned', '从未扫描'],
                ['stale', '结论陈旧'],
                ['ok', '正常'],
              ] as const
            ).map(([value, label]) => (
              <button
                key={value}
                type="button"
                onClick={() => setHealthFilter(value)}
                aria-pressed={healthFilter === value}
                className={cn(
                  'rounded-sm border px-2 py-1 text-xs transition-colors',
                  healthFilter === value
                    ? 'border-accent bg-accent/10 text-accent'
                    : 'border-line text-ink-muted hover:text-ink',
                )}
              >
                {label}
              </button>
            ))}
          </div>

          <Button
            variant="secondary"
            size="sm"
            onClick={refresh}
            disabled={state.status === 'loading'}
            title="重新拉取资产列表"
          >
            <RefreshCw size={13} aria-hidden="true" />
            刷新
          </Button>
        </div>

        {/* ── 截断提示：不能让用户以为「总共就这些」 ──────────── */}
        {state.truncated && (
          <p
            role="status"
            className="mb-panel rounded-sm border border-line bg-surface px-3 py-2 text-xs text-signal-warn"
          >
            只显示了前 {state.items?.length ?? 0} 个目标，共 {state.total} 个。
            统计数字覆盖全量，但列表是截断的。
          </p>
        )}

        {/* ── 表格 ───────────────────────────────────────────── */}
        {filtered.length === 0 ? (
          <EmptyState
            icon={Layers}
            title={items.length === 0 ? '还没有任何目标' : '没有符合条件的资产'}
            description={
              items.length === 0
                ? '发起第一次扫描后，这里会按目标聚合出现在的情况。'
                : '换个关键词或切回「全部」看看。'
            }
          />
        ) : (
          <div className="overflow-x-auto rounded-md border border-line">
            <table className="w-full min-w-[52rem] border-collapse text-sm">
              <thead>
                <tr className="border-b border-line bg-sunken text-left text-xs text-ink-muted">
                  <th scope="col" className="px-panel py-2 font-normal">
                    目标
                  </th>
                  <th scope="col" className="px-panel py-2 font-normal">
                    状态
                  </th>
                  <th scope="col" className="px-panel py-2 text-right font-normal">
                    未处置
                  </th>
                  <th scope="col" className="px-panel py-2 text-right font-normal">
                    高危 / 中危+
                  </th>
                  <th scope="col" className="px-panel py-2 font-normal">
                    上次扫描
                  </th>
                  <th scope="col" className="px-panel py-2 font-normal">
                    上次拿到结论
                  </th>
                  <th scope="col" className="px-panel py-2 text-right font-normal">
                    扫描次数
                  </th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((asset) => {
                  const health = assetHealth(asset)
                  return (
                    <tr
                      key={asset.target}
                      className="border-b border-line last:border-b-0 hover:bg-sunken/60"
                    >
                      <td className="px-panel py-2">
                        <Link
                          to={`/scans?target=${encodeURIComponent(asset.target)}`}
                          className="break-all font-mono text-sm text-ink hover:text-accent"
                        >
                          {asset.target}
                        </Link>
                      </td>
                      <td className="px-panel py-2">
                        <HealthBadge health={health} />
                      </td>
                      <td className="px-panel py-2 text-right tabular-nums">
                        <span
                          className={cn(
                            asset.openFindingCount > 0 ? 'text-ink' : 'text-ink-muted',
                          )}
                        >
                          {asset.openFindingCount}
                        </span>
                        {asset.findingCount > asset.openFindingCount && (
                          <span className="ml-1 text-xs text-ink-muted">
                            / {asset.findingCount}
                          </span>
                        )}
                      </td>
                      <td className="px-panel py-2 text-right tabular-nums">
                        <span
                          className={cn(
                            asset.openCritical > 0 || asset.openHigh > 0
                              ? 'text-signal-risk'
                              : 'text-ink-muted',
                          )}
                        >
                          {asset.openHigh + asset.openCritical}
                        </span>
                      </td>
                      <td className="px-panel py-2 text-ink-muted" title={asset.lastScanAt ?? '从未扫描'}>
                        {relativeTime(asset.lastScanAt, now)}
                        {asset.consecutiveFailures > 0 && (
                          <span className="ml-1 text-xs text-signal-risk">
                            连续失败 {asset.consecutiveFailures} 次
                          </span>
                        )}
                      </td>
                      <td
                        className="px-panel py-2 text-ink-muted"
                        title={asset.lastCompletedAt ?? '从未跑完'}
                      >
                        {relativeTime(asset.lastCompletedAt, now)}
                      </td>
                      <td className="px-panel py-2 text-right tabular-nums text-ink-muted">
                        {asset.scanCount}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}

        {/* ── 排序口径说明：把规则写出来，否则用户不知道第一行为什么是它 ── */}
        {filtered.length > 0 && (
          <p className="mt-panel text-xs text-ink-muted">
            按风险排序：带开着的高危 → 从未跑完 → 连续失败 → 从未扫描 → 结论陈旧；同档内越久没拿到结论越靠前。
          </p>
        )}
      </div>
    </>
  )
}
