import { cn } from '@/lib/cn'
import { compareScans, type ScanComparison } from '@/data/scans-store'
import { errorMessage } from '@/lib/api'
import { AlertTriangle, Loader2, Minus, Plus, X } from 'lucide-react'
import { useEffect, useState } from 'react'

/**
 * 两条扫描的发现对比（任务 9）。
 *
 * ## 为什么按**标题**比而不是按漏洞 id
 *
 * `source_ref` 是外部工具在**单次运行内**的编号（`vuln-0001`），
 * 换个目标、换次运行都会重来一遍。拿它比，两次同一目标的扫描会得出
 * "全都不一样"——一个毫无意义但看起来很确定的结论。
 * 人比对两次扫描时问的是"同一个问题这次还在不在"，那个"问题"就是它的标题。
 *
 * 后端已经按标题比好了（`service.compare_scans`），这里只负责展示。
 *
 * ## 为什么"消失"用中性色、"新增"用危险色
 *
 * 直觉上"新增"是好消息（发现了新问题？）—— 恰恰相反：
 * 对比两次扫描时，**新增意味着问题变多了**，而"消失"意味着修好了。
 * 所以新增用 risk 色、消失用 ok 色。这与"漏洞数变多不是好事"一致。
 */

export function CompareDialog({
  aId,
  bId,
  onClose,
}: {
  aId: string
  bId: string
  onClose: () => void
}) {
  const [data, setData] = useState<ScanComparison | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    void compareScans(aId, bId)
      .then((result) => {
        if (alive) setData(result)
      })
      .catch((err) => {
        if (alive) setError(errorMessage(err))
      })
    return () => {
      alive = false
    }
  }, [aId, bId])

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [onClose])

  return (
    <div
      onClick={onClose}
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-canvas/70 p-gutter backdrop-blur-sm"
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="compare-title"
        onClick={(e) => e.stopPropagation()}
        className="mt-bay w-full max-w-3xl animate-rise-in rounded-md border border-line bg-surface-overlay p-panel shadow-overlay"
      >
        <div className="flex items-start justify-between gap-2">
          <div>
            <h2 id="compare-title" className="text-sm font-medium text-ink">
              扫描对比
            </h2>
            {data && (
              <p className="mt-1 text-xs text-ink-muted">
                <span className="mono-ui">{data.a.startedAt.slice(0, 16).replace('T', ' ')}</span>
                {' → '}
                <span className="mono-ui">{data.b.startedAt.slice(0, 16).replace('T', ' ')}</span>
                <span className="ml-2 text-ink-faint">目标 {data.b.target}</span>
              </p>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭"
            className="rounded-sm p-1 text-ink-faint transition-ui duration-fast hover:text-ink"
          >
            <X size={14} strokeWidth={2} aria-hidden="true" />
          </button>
        </div>

        {error ? (
          <p role="alert" className="mt-panel text-xs text-signal-risk">
            对比失败：{error}
          </p>
        ) : !data ? (
          <p className="mt-panel flex items-center gap-2 text-xs text-ink-faint">
            <Loader2 size={13} className="animate-spin" aria-hidden="true" />
            正在对比…
          </p>
        ) : (
          <>
            {/* 一句话结论 —— 多数人只看这一行 */}
            <p className="mt-panel rounded-sm border border-line bg-surface-sunken px-3 py-2 text-xs text-ink">
              {data.verdict}
            </p>

            {/*
              ★ 目标不同的警告。

              页面不禁止选两条不同目标的扫描（"比较两台主机的同类问题"是个真实
              需求），但**必须说出来**：按标题比对出来的"新增/消失"在目标不同时
              基本没有意义 —— 两台机器上出现同一个标题的漏洞，不代表它"还在"。
              不说的话，用户会拿到一份格式完整、结论笃定、但推导前提不成立的报告。
            */}
            {data.a.target !== data.b.target && (
              <div className="mt-2 flex items-start gap-1.5 rounded-sm border border-signal-warn/40 bg-signal-warn/[0.06] p-2">
                <AlertTriangle
                  size={12}
                  strokeWidth={2}
                  className="mt-0.5 shrink-0 text-signal-warn"
                  aria-hidden="true"
                />
                <p className="text-2xs text-signal-warn">
                  这两次扫描的目标不同（
                  <span className="mono-ui">{data.a.target}</span> 与{' '}
                  <span className="mono-ui">{data.b.target}</span>）—— 下面的「新增 / 消失」
                  是按漏洞标题比出来的，跨目标时通常不能当作"问题变多 / 已修好"的证据。
                </p>
              </div>
            )}

            <div className="mt-panel grid gap-panel sm:grid-cols-2">
              <Side
                title="新增"
                hint="这次才出现 —— 可能是新引入的问题"
                items={data.onlyInB}
                tone="risk"
              />
              <Side
                title="消失"
                hint="上次有、这次没有 —— 可能是已经修好了"
                items={data.onlyInA}
                tone="ok"
              />
            </div>

            {data.common.length > 0 && (
              <div className="mt-panel">
                <h3 className="text-xs font-medium text-ink-muted">
                  两次都有（{data.common.length}）
                </h3>
                <ul className="mt-1 flex flex-col gap-0.5">
                  {data.common.map((item) => (
                    <li key={item} className="flex items-start gap-1.5 text-2xs text-ink-faint">
                      <Minus size={11} strokeWidth={2} className="mt-0.5 shrink-0" aria-hidden="true" />
                      <span className="min-w-0">{item}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}

function Side({
  title,
  hint,
  items,
  tone,
}: {
  title: string
  hint: string
  items: string[]
  tone: 'risk' | 'ok'
}) {
  return (
    <section className="rounded-sm border border-line bg-surface p-panel">
      <div className="flex items-baseline gap-2">
        <h3 className={cn('text-xs font-medium', tone === 'risk' ? 'text-signal-risk' : 'text-signal-ok')}>
          {title}
        </h3>
        <span className="tabular text-2xs text-ink-faint" data-numeric>
          {items.length}
        </span>
      </div>
      <p className="mt-0.5 text-2xs text-ink-faint">{hint}</p>

      {items.length === 0 ? (
        <p className="mt-2 text-2xs text-ink-ghost">没有</p>
      ) : (
        <ul className="mt-2 flex flex-col gap-1">
          {items.map((item) => (
            <li key={item} className="flex items-start gap-1.5 text-2xs text-ink-muted">
              <Plus
                size={11}
                strokeWidth={2}
                className={cn('mt-0.5 shrink-0', tone === 'risk' ? 'text-signal-risk' : 'text-signal-ok')}
                aria-hidden="true"
              />
              <span className="min-w-0">{item}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

export default CompareDialog
