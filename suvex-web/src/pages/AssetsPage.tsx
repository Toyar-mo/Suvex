import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { ChevronRight, Layers, RefreshCw, Search } from 'lucide-react'
import { PageHeader } from '@/components/layout/AppShell'
import AssetCoverageDrawer from '@/components/assets/AssetCoverageDrawer'
import EmptyState from '@/components/ui/EmptyState'
import ErrorState from '@/components/ui/ErrorState'
import { Button } from '@/components/ui/Button'
import { SegmentedControl } from '@/components/ui/SegmentedControl'
import { SkeletonRows } from '@/components/ui/Skeleton'
import { cn } from '@/lib/cn'
import {
  ASSET_HEALTH_META,
  assetHealth,
  assetMatchesQuery,
  type Asset,
  type AssetHealth,
} from '@/data/asset'
import { buildHostAssets, hostMatchesQuery, parseTargetParts, type HostAsset } from '@/data/asset-host'
import { loadAssets, useAssetsState } from '@/data/assets-store'

/**
 * 资产 —— 「我有哪些机器，每台暴露了什么、现在能不能信」。
 *
 * ## 为什么默认是「主机」而不是「目标」
 *
 * 后端 `/assets` 按 target 聚合：一个 URL/路径就是一个资产。那对扫描
 * 统计是对的，但对"资产管理"不是 —— 同一台 `127.0.0.1:9001` 的
 * `/.env`、`/api/users`、`/app.py.bak` 会各占一行，195 个目标里
 * 真正的主机可能只有几十台。默认视图先回答"有几台机器"，展开再回答
 * "这台机器暴露了哪些面"。
 *
 * 这里的主机聚合是**前端纯函数**（`buildHostAssets`），不改变后端口径；
 * 「目标」视图仍然原样保留，扫描统计与漏洞归属不受影响。
 *
 * ## 健康度取最差
 *
 * 一台主机下只要有一条路径"从未跑完"，整台机器就不能被当成已验证 ——
 * 取最差档位，而不是平均值。平均值会把"一条扫完了、九条没跑"算成
 * 看起来健康。
 */

type HealthFilter = 'all' | AssetHealth
type AssetView = 'host' | 'target'

const EMPTY_ASSETS: Asset[] = []

const HEALTH_RANK: Record<AssetHealth, number> = {
  'never-completed': 0,
  failing: 1,
  'never-scanned': 2,
  stale: 3,
  ok: 4,
}

function lastConclusionTs(asset: Asset): number {
  if (asset.lastCompletedAt === null) return Number.NEGATIVE_INFINITY
  const ts = new Date(asset.lastCompletedAt).getTime()
  return Number.isFinite(ts) ? ts : Number.NEGATIVE_INFINITY
}

function compareAssets(a: Asset, b: Asset): number {
  const aBleeding = a.openCritical > 0 || a.openHigh > 0 ? 1 : 0
  const bBleeding = b.openCritical > 0 || b.openHigh > 0 ? 1 : 0
  if (aBleeding !== bBleeding) return bBleeding - aBleeding
  const aHealth = assetHealth(a)
  const bHealth = assetHealth(b)
  if (aHealth !== bHealth) return HEALTH_RANK[aHealth] - HEALTH_RANK[bHealth]
  const aTs = lastConclusionTs(a)
  const bTs = lastConclusionTs(b)
  if (aTs !== bTs) return aTs - bTs
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
    <span className="inline-flex items-center gap-tight whitespace-nowrap text-xs">
      <span className={cn('h-1.5 w-1.5 shrink-0 rounded-pill', meta.dot)} aria-hidden="true" />
      <span className={meta.className} title={meta.hint}>
        {meta.label}
      </span>
    </span>
  )
}

function Stat({
  label,
  value,
  hint,
  tone = 'default',
}: {
  label: string
  value: number | string
  hint?: string
  tone?: 'default' | 'warn' | 'risk'
}) {
  return (
    <div className="px-panel py-panel" title={hint}>
      <p className="text-2xs text-ink-muted">{label}</p>
      <p
        className={cn(
          'mt-tight text-lg font-medium tabular-nums',
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

function PortChips({ ports }: { ports: string[] }) {
  if (ports.length === 0) return <span className="text-2xs text-ink-faint">默认端口</span>
  const visible = ports.slice(0, 3)
  return (
    <span className="flex flex-wrap gap-1">
      {visible.map((port) => (
        <span
          key={port}
          className="rounded-xs border border-line bg-surface-sunken px-1.5 py-0.5 font-mono text-2xs text-ink-muted tabular-nums"
        >
          :{port}
        </span>
      ))}
      {ports.length > visible.length && (
        <span className="text-2xs text-ink-faint">+{ports.length - visible.length}</span>
      )}
    </span>
  )
}

/**
 * 主机行。展开后列出这台机器下的路径。
 *
 * `memo` 只对"没展开、props 不变"的行有效；展开态是页面级 Set，
 * 切换时只有该行前后两行会重渲染。
 */
const HostRow = memo(function HostRow({
  host,
  expanded,
  nowMinute,
  onToggle,
  onOpenCoverage,
}: {
  host: HostAsset
  expanded: boolean
  nowMinute: number
  onToggle: (host: string) => void
  onOpenCoverage: (target: string) => void
}) {
  const now = nowMinute * 60_000
  return (
    <>
      <tr className="border-b border-line last:border-b-0 hover:bg-sunken/60">
        <td className="px-panel py-2.5">
          <button
            type="button"
            onClick={() => onToggle(host.host)}
            aria-expanded={expanded}
            className="flex items-center gap-2 text-left"
          >
            <ChevronRight
              size={14}
              strokeWidth={2}
              aria-hidden="true"
              className={cn('shrink-0 text-ink-faint transition-transform duration-fast', expanded && 'rotate-90')}
            />
            <span className="break-all font-mono text-sm text-ink hover:text-accent">{host.host}</span>
          </button>
        </td>
        <td className="px-panel py-2.5">
          <PortChips ports={host.ports} />
        </td>
        <td className="px-panel py-2.5 text-right text-xs tabular-nums text-ink-muted">
          {host.assets.length}
        </td>
        <td className="px-panel py-2.5">
          <HealthBadge health={host.health} />
        </td>
        <td className="px-panel py-2.5 text-right tabular-nums">
          <span className={cn(host.openFindingCount > 0 ? 'text-ink' : 'text-ink-muted')}>
            {host.openFindingCount}
          </span>
        </td>
        <td className="px-panel py-2.5 text-right tabular-nums">
          <span className={cn(host.openCritical + host.openHigh > 0 ? 'text-signal-risk' : 'text-ink-muted')}>
            {host.openCritical + host.openHigh}
          </span>
        </td>
        <td className="px-panel py-2.5 text-xs text-ink-muted" title={host.lastCompletedAt ?? '从未跑完'}>
          {relativeTime(host.lastCompletedAt, now)}
        </td>
      </tr>
      {expanded && (
        <tr className="border-b border-line bg-sunken/40">
          <td colSpan={7} className="px-panel py-2">
            <div className="flex flex-col gap-1">
              {host.assets.map((asset) => {
                const health = assetHealth(asset, now)
                const parts = parseTargetParts(asset.target)
                return (
                  <div
                    key={asset.target}
                    className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-xs px-2 py-1.5 text-xs hover:bg-surface"
                  >
                    <Link
                      to={`/scans?target=${encodeURIComponent(asset.target)}`}
                      title={asset.target}
                      className="min-w-0 flex-1 break-all font-mono text-ink-muted hover:text-accent"
                    >
                      {parts.port && <span className="text-ink-faint">:{parts.port} </span>}
                      {parts.path && parts.path !== '/' ? parts.path : asset.target}
                    </Link>
                    <HealthBadge health={health} />
                    <span className="w-16 text-right tabular-nums text-ink-muted">
                      未处置 {asset.openFindingCount}
                    </span>
                    <span
                      className={cn(
                        'w-20 text-right tabular-nums',
                        asset.openCritical + asset.openHigh > 0 ? 'text-signal-risk' : 'text-ink-muted',
                      )}
                    >
                      高危 {asset.openCritical + asset.openHigh}
                    </span>
                    <span className="w-24 text-right text-ink-faint">
                      {relativeTime(asset.lastCompletedAt, now)}
                    </span>
                    <button
                      type="button"
                      onClick={() => onOpenCoverage(asset.target)}
                      className="rounded-xs border border-line px-1.5 py-0.5 text-2xs text-ink-muted transition-ui hover:border-line-strong hover:text-ink"
                      aria-label={`查看 ${asset.target} 的覆盖度与事实图`}
                    >
                      覆盖度
                    </button>
                  </div>
                )
              })}
            </div>
          </td>
        </tr>
      )}
    </>
  )
})

const AssetRow = memo(function AssetRow({
  asset,
  nowMinute,
  onOpenCoverage,
}: {
  asset: Asset
  nowMinute: number
  onOpenCoverage: (target: string) => void
}) {
  const now = nowMinute * 60_000
  const health = assetHealth(asset, now)
  return (
    <tr className="border-b border-line last:border-b-0 hover:bg-sunken/60">
      <td className="px-panel py-2">
        <div className="flex flex-wrap items-center gap-2">
          <Link
            to={`/scans?target=${encodeURIComponent(asset.target)}`}
            className="break-all font-mono text-sm text-ink hover:text-accent"
          >
            {asset.target}
          </Link>
          <button
            type="button"
            onClick={() => onOpenCoverage(asset.target)}
            className="rounded-xs border border-line px-1.5 py-0.5 text-2xs text-ink-muted hover:border-line-strong hover:text-ink"
            aria-label={`查看 ${asset.target} 的覆盖度与事实图`}
          >
            覆盖度
          </button>
        </div>
      </td>
      <td className="px-panel py-2">
        <HealthBadge health={health} />
      </td>
      <td className="px-panel py-2 text-right tabular-nums">
        <span className={cn(asset.openFindingCount > 0 ? 'text-ink' : 'text-ink-muted')}>
          {asset.openFindingCount}
        </span>
        {asset.findingCount > asset.openFindingCount && (
          <span className="ml-1 text-xs text-ink-muted">/ {asset.findingCount}</span>
        )}
      </td>
      <td className="px-panel py-2 text-right tabular-nums">
        <span className={cn(asset.openCritical + asset.openHigh > 0 ? 'text-signal-risk' : 'text-ink-muted')}>
          {asset.openCritical + asset.openHigh}
        </span>
      </td>
      <td className="px-panel py-2 text-ink-muted" title={asset.lastScanAt ?? '从未扫描'}>
        {relativeTime(asset.lastScanAt, now)}
        {asset.consecutiveFailures > 0 && (
          <span className="ml-1 text-xs text-signal-risk">连续失败 {asset.consecutiveFailures} 次</span>
        )}
      </td>
      <td className="px-panel py-2 text-ink-muted" title={asset.lastCompletedAt ?? '从未跑完'}>
        {relativeTime(asset.lastCompletedAt, now)}
      </td>
      <td className="px-panel py-2 text-right tabular-nums text-ink-muted">{asset.scanCount}</td>
    </tr>
  )
})

export default function AssetsPage() {
  const state = useAssetsState()
  const [coverageTarget, setCoverageTarget] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [healthFilter, setHealthFilter] = useState<HealthFilter>('all')
  const [view, setView] = useState<AssetView>('host')
  const [expandedHosts, setExpandedHosts] = useState<Set<string>>(() => new Set())
  const [now, setNow] = useState(() => Date.now())
  const nowMinute = Math.floor(now / 60_000)

  const loadedOnce = useRef(false)
  useEffect(() => {
    if (loadedOnce.current) return
    loadedOnce.current = true
    if (state.status === 'idle') void loadAssets()
  }, [state.status])

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 30_000)
    return () => window.clearInterval(timer)
  }, [])

  const refresh = useCallback(() => {
    void loadAssets({ silent: state.items !== null })
  }, [state.items])

  const items = useMemo(() => state.items ?? EMPTY_ASSETS, [state.items])

  const hosts = useMemo(() => buildHostAssets(items, nowMinute * 60_000), [items, nowMinute])

  const filteredHosts = useMemo(
    () =>
      hosts
        .filter((host) => healthFilter === 'all' || host.health === healthFilter)
        .filter((host) => hostMatchesQuery(host, query)),
    [hosts, healthFilter, query],
  )

  const filteredAssets = useMemo(
    () =>
      items
        .filter((asset) => healthFilter === 'all' || assetHealth(asset) === healthFilter)
        .filter((asset) => assetMatchesQuery(asset, query))
        .sort(compareAssets),
    [items, healthFilter, query],
  )

  const toggleHost = useCallback((host: string) => {
    setExpandedHosts((current) => {
      const next = new Set(current)
      if (next.has(host)) next.delete(host)
      else next.add(host)
      return next
    })
  }, [])

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
  const bleedingHosts = hosts.filter((host) => host.openCritical + host.openHigh > 0).length
  const hostCount = state.truncated ? `≥${hosts.length}` : String(hosts.length)
  const neverVerified = summary ? summary.neverScanned + summary.neverCompleted : 0
  const visible = view === 'host' ? filteredHosts.length : filteredAssets.length

  return (
    <>
      <PageHeader navId="assets" />

      <div className="px-gutter py-bay">
        {/* ── 统计条：一行读完，不占五张卡的垂直空间 ─────────── */}
        {summary && (
          <div className="mb-bay grid grid-cols-2 divide-x divide-line overflow-hidden rounded-md border border-line bg-surface sm:grid-cols-3 lg:grid-cols-5">
            <Stat
              label="主机"
              value={hostCount}
              hint={state.truncated ? '列表被截断，这里只统计返回部分' : '按 host 聚合'}
            />
            <Stat label="暴露面" value={summary.targetCount} hint="目标 / 路径数量" />
            <Stat
              label="带高危主机"
              value={state.truncated ? `≥${bleedingHosts}` : bleedingHosts}
              hint="有未处置 critical/high 的主机台数"
              tone={bleedingHosts > 0 ? 'risk' : 'default'}
            />
            <Stat
              label="从未验证"
              value={neverVerified}
              hint={`从未扫描 ${summary.neverScanned} · 从未跑完 ${summary.neverCompleted}`}
              tone={neverVerified > 0 ? 'warn' : 'default'}
            />
            <Stat label="未处置漏洞" value={summary.openFindingCount} />
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
              placeholder="按主机或路径筛选…"
              aria-label="按目标筛选资产"
              className="w-full rounded-sm border border-line bg-surface py-1.5 pl-8 pr-2.5 text-sm text-ink placeholder:text-ink-muted focus:border-accent focus:outline-none"
            />
          </div>

          <SegmentedControl
            size="sm"
            ariaLabel="资产视图"
            value={view}
            onChange={setView}
            options={[
              { id: 'host', label: '主机' },
              { id: 'target', label: '目标' },
            ]}
          />

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

        {state.truncated && (
          <p
            role="status"
            className="mb-panel rounded-sm border border-line bg-surface px-3 py-2 text-xs text-signal-warn"
          >
            只显示了前 {state.items?.length ?? 0} 个目标，共 {state.total} 个。
            统计数字覆盖全量，但列表是截断的。
          </p>
        )}

        {/* ── 列表 ───────────────────────────────────────────── */}
        <div role="tabpanel" aria-label="资产列表">
        {visible === 0 ? (
          <EmptyState
            icon={Layers}
            title={items.length === 0 ? '还没有任何目标' : '没有符合条件的资产'}
            description={
              items.length === 0
                ? '发起第一次扫描后，这里会按主机聚合出现在的情况。'
                : '换个关键词或切回「全部」看看。'
            }
          />
        ) : view === 'host' ? (
          <div className="overflow-x-auto rounded-md border border-line">
            <table className="w-full min-w-[56rem] border-collapse text-sm">
              <thead>
                <tr className="border-b border-line bg-sunken text-left text-xs text-ink-muted">
                  <th scope="col" className="px-panel py-2 font-normal">
                    主机
                  </th>
                  <th scope="col" className="px-panel py-2 font-normal">
                    端口
                  </th>
                  <th scope="col" className="px-panel py-2 text-right font-normal">
                    暴露面
                  </th>
                  <th scope="col" className="px-panel py-2 font-normal">
                    状态
                  </th>
                  <th scope="col" className="px-panel py-2 text-right font-normal">
                    未处置
                  </th>
                  <th scope="col" className="px-panel py-2 text-right font-normal">
                    高危
                  </th>
                  <th scope="col" className="px-panel py-2 font-normal">
                    最近结论
                  </th>
                </tr>
              </thead>
              <tbody>
                {filteredHosts.map((host) => (
                  <HostRow
                    key={host.host}
                    host={host}
                    expanded={expandedHosts.has(host.host)}
                    nowMinute={nowMinute}
                    onToggle={toggleHost}
                    onOpenCoverage={setCoverageTarget}
                  />
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="overflow-x-auto rounded-md border border-line">
            <table className="w-full min-w-[52rem] border-collapse text-sm">
              <thead>
                <tr className="border-b border-line bg-sunken text-left text-xs text-ink-muted">
                  <th scope="col" className="whitespace-nowrap px-panel py-2 font-normal">
                    目标
                  </th>
                  <th scope="col" className="whitespace-nowrap px-panel py-2 font-normal">
                    状态
                  </th>
                  <th scope="col" className="whitespace-nowrap px-panel py-2 text-right font-normal">
                    未处置
                  </th>
                  <th scope="col" className="whitespace-nowrap px-panel py-2 text-right font-normal">
                    高危 / 中危+
                  </th>
                  <th scope="col" className="whitespace-nowrap px-panel py-2 font-normal">
                    上次扫描
                  </th>
                  <th scope="col" className="whitespace-nowrap px-panel py-2 font-normal">
                    上次拿到结论
                  </th>
                  <th scope="col" className="whitespace-nowrap px-panel py-2 text-right font-normal">
                    扫描次数
                  </th>
                </tr>
              </thead>
              <tbody>
                {filteredAssets.map((asset) => (
                  <AssetRow
                    key={asset.target}
                    asset={asset}
                    nowMinute={nowMinute}
                    onOpenCoverage={setCoverageTarget}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
        </div>

        {visible > 0 && (
          <p className="mt-panel text-xs text-ink-muted">
            {view === 'host'
              ? '按风险排序：带高危主机 → 从未跑完 → 连续失败 → 从未扫描 → 结论陈旧；展开主机看具体路径。'
              : '按风险排序：带开着的高危 → 从未跑完 → 连续失败 → 从未扫描 → 结论陈旧；同档内越久没拿到结论越靠前。'}
          </p>
        )}
      </div>
      <AssetCoverageDrawer
        key={coverageTarget ?? 'none'}
        target={coverageTarget}
        onClose={() => setCoverageTarget(null)}
      />
    </>
  )
}
