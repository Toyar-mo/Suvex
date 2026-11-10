import { useEffect, useState } from 'react'
import Drawer from '@/components/ui/Drawer'
import { SkeletonRows } from '@/components/ui/Skeleton'
import { toApiError } from '@/lib/api'
import {
  loadCoverage,
  loadSurfaceMap,
  type CoverageItem,
  type CoverageSnapshot,
  type SurfaceMap,
} from '@/data/assetCoverage'

/**
 * 资产的覆盖度与事实图。
 *
 * 只读抽屉：左侧/上方是"测过什么、排除了什么"，下方是"事实之间怎么连"。
 * 加载失败与空态分开表达 —— "没有记录"不等于"没测过"，更不等于"没缺口"，
 * 所以空态文案必须说清这一点。
 */
export default function AssetCoverageDrawer({
  target,
  onClose,
}: {
  target: string | null
  onClose: () => void
}) {
  const [coverage, setCoverage] = useState<CoverageSnapshot | null>(null)
  const [surface, setSurface] = useState<SurfaceMap | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    if (!target) return
    let alive = true
    /*
      放进宏任务：effect 体内同步 setState 会触发
      `react-hooks/set-state-in-effect`（级联渲染）。切 target 时的
      "清空旧数据"由调用处的 key 重挂载负责，不在这里同步做。
    */
    const timer = window.setTimeout(() => {
      setLoading(true)
      setError(null)
      void Promise.all([loadCoverage(target), loadSurfaceMap(target)])
        .then(([nextCoverage, nextSurface]) => {
          if (!alive) return
          setCoverage(nextCoverage)
          setSurface(nextSurface)
        })
        .catch((err) => {
          if (!alive) return
          setError(toApiError(err).message)
        })
        .finally(() => {
          if (alive) setLoading(false)
        })
    }, 0)
    return () => {
      alive = false
      window.clearTimeout(timer)
    }
  }, [target])

  return (
    <Drawer
      open={target !== null}
      onClose={onClose}
      title="覆盖度与事实图"
      titleAside={
        <span className="max-w-[18rem] truncate font-mono text-xs text-ink-muted" title={target ?? ''}>
          {target ?? ''}
        </span>
      }
    >
      {loading ? (
        <SkeletonRows rows={5} rowClassName="h-8" />
      ) : error ? (
        <p className="text-sm text-signal-risk">{error}</p>
      ) : (
        <div className="flex flex-col gap-panel">
          <section>
            <h3 className="text-sm font-medium text-ink">覆盖度</h3>
            <p className="mt-1 text-2xs text-ink-muted">
              只列<strong>有记录</strong>的面；没有记录不等于没测过，也不等于没有缺口。
            </p>
            <div className="mt-2 grid grid-cols-2 gap-2 text-sm">
              <span className="text-ink-muted">测过的面</span>
              <span className="tabular text-right text-ink">{coverage?.stats.tested ?? 0}</span>
              <span className="text-ink-muted">已排除</span>
              <span className="tabular text-right text-ink">
                {coverage?.stats.ruledOut ?? 0}
              </span>
            </div>
            <div className="mt-2 flex flex-wrap gap-1.5">
              {(coverage?.gaps ?? []).map((gap) => (
                <span
                  key={gap.key}
                  title={gap.recorded ? '有记录' : '没有记录（不等于没测过）'}
                  className={
                    gap.recorded
                      ? 'rounded-pill border border-line bg-surface px-2 py-0.5 text-2xs text-ink'
                      : 'rounded-pill border border-dashed border-line px-2 py-0.5 text-2xs text-ink-faint'
                  }
                >
                  {gap.recorded ? '✓ ' : '· '}
                  {gap.label}
                </span>
              ))}
            </div>
            <ItemList
              title="测过的面"
              items={coverage?.tested ?? []}
              empty="还没有测过的面"
            />
            <ItemList
              title="已排除"
              items={coverage?.ruledOut ?? []}
              empty="还没有排除记录"
            />
          </section>

          <section>
            <h3 className="text-sm font-medium text-ink">事实图</h3>
            <p className="mt-1 text-2xs text-ink-muted">
              节点是事实，线是事实之间的关系（攻击链 / 依赖 / 约束）。
            </p>
            {surface && surface.nodes.length > 0 ? (
              <>
                <ul className="mt-2 flex flex-col gap-1.5">
                  {surface.nodes.map((node) => (
                    <li key={node.factKey} className="rounded-sm border border-line px-2 py-1.5">
                      <div className="flex items-center justify-between gap-2">
                        <code className="truncate font-mono text-2xs text-ink-muted">
                          {node.factKey}
                        </code>
                        <span className="text-2xs text-ink-faint">{node.category}</span>
                      </div>
                      <p className="mt-0.5 text-xs text-ink">{node.summary}</p>
                    </li>
                  ))}
                </ul>
                {surface.edges.length > 0 && (
                  <ul className="mt-2 flex flex-col gap-1 text-2xs text-ink-muted">
                    {surface.edges.map((edge) => (
                      <li key={`${edge.from}-${edge.type}-${edge.to}`} className="truncate">
                        <code className="font-mono">{edge.from}</code> —{edge.type}→{' '}
                        <code className="font-mono">{edge.to}</code>
                      </li>
                    ))}
                  </ul>
                )}
              </>
            ) : (
              <p className="mt-2 text-sm text-ink-muted">还没有写成事实的认知</p>
            )}
          </section>
        </div>
      )}
    </Drawer>
  )
}

function ItemList({
  title,
  items,
  empty,
}: {
  title: string
  items: CoverageItem[]
  empty: string
}) {
  return (
    <div className="mt-3">
      <h4 className="text-2xs uppercase tracking-wide text-ink-faint">{title}</h4>
      {items.length === 0 ? (
        <p className="mt-1 text-xs text-ink-muted">{empty}</p>
      ) : (
        <ul className="mt-1 flex flex-col gap-1">
          {items.map((item) => (
            <li key={item.factKey} className="rounded-sm border border-line px-2 py-1.5">
              <div className="flex items-center justify-between gap-2">
                <code className="truncate font-mono text-2xs text-ink-muted">{item.factKey}</code>
                <span className="text-2xs text-ink-faint">{item.recordedBy}</span>
              </div>
              <p className="mt-0.5 text-xs text-ink">{item.summary}</p>
              {item.detail && <p className="mt-0.5 text-2xs text-ink-muted">{item.detail}</p>}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
