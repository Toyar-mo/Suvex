import { cn } from '@/lib/cn'
import { SEVERITY_META } from '@/data/findings'
import type { SeverityBreakdownEntry } from '@/data/overview'

/**
 * 漏洞严重级别分布 —— 水平条形图。
 *
 * 为什么是水平条而不是环形/饼图：
 *   1. 环形图只适合"占比"叙事，而这里要读的是**每一级各有多少条**。
 *      五个扇形里比较长度，人眼做不到；同一基线的条长可以直接比。
 *   2. 严重度是有序的（critical > high > …），水平条能自然按顺序从上到下排，
 *      环形图的顺序是任意的。
 *   3. 标签可以水平写，不用为了塞进扇形而旋转或引线。
 *
 * 为什么不用 SVG 库：这里只需要矩形，用 div + width% 就够 —— 引入图表库
 * 会增加 ~50KB 并且让主题化变复杂（要覆盖库的默认色）。
 *
 * 可读性细节：
 *   - 每条都**直接标数值**，不靠 tooltip。看板页不该要求用户悬停才得到数字。
 *   - 0 值保留一行（灰色空条 + "0"），不隐藏 —— 缺一行会被读成"漏了"。
 *   - 条宽按**最大值**归一，不按总和。按总和时最大的一级也只占 1/5 宽，
 *     整个图表会显得全是残条，读不出差异。
 *   - 总数写在标题右侧，它是"这堆条加起来是多少"的答案。
 */

export interface SeverityChartProps {
  data: SeverityBreakdownEntry[]
  /** 标题。默认给一个，但允许覆盖以便复用到别的页面 */
  title?: string
  className?: string
}

export function SeverityChart({ data, title = '漏洞严重级别分布', className }: SeverityChartProps) {
  const total = data.reduce((sum, entry) => sum + entry.count, 0)
  const max = Math.max(...data.map((entry) => entry.count), 0)

  return (
    <section
      aria-labelledby="severity-chart-title"
      className={cn('rounded-md border border-line bg-surface p-panel', className)}
    >
      <div className="flex items-baseline justify-between gap-tight">
        <h2 id="severity-chart-title" className="text-sm font-medium text-ink">
          {title}
        </h2>
        <span className="mono-ui tabular text-ink-faint" data-numeric>
          共 {total} 条
        </span>
      </div>

      {total === 0 ? (
        <p className="mt-panel text-xs text-ink-faint">
          暂无发现。扫描报告出第一条漏洞后，这里会显示分布。
        </p>
      ) : (
        <ul className="mt-panel flex flex-col gap-2.5">
          {data.map((entry) => {
            const meta = SEVERITY_META[entry.severity]
            const zero = entry.count === 0
            // 按最大值归一；非零值给一个最小宽度，避免 "1" 在 13 面前细成一根发丝
            const pct = max === 0 ? 0 : (entry.count / max) * 100
            const width = zero ? 0 : Math.max(pct, 4)

            return (
              <li key={entry.severity} className="flex items-center gap-tight">
                {/* 级别名。固定宽度让所有条从同一条基线开始 */}
                <span className="w-[4.5rem] shrink-0 text-xs text-ink-muted">{meta.label}</span>

                {/* 条。aria-hidden：数值已由右侧文本承载，条本身无额外语义 */}
                {/*
                  轨道用 line token 而不是 surface-sunken：
                  后者对 surface 只有 1.03:1，等于没有轨道，条看起来是悬空的。
                  line(#1F242D) 对 surface 是 1.22:1 —— 够当凹槽但不会抢戏。
                  每个填充对它都有 3.5:1 以上，条长边界清晰。
                */}
                <div
                  aria-hidden="true"
                  className="flex h-2 flex-1 overflow-hidden rounded-xs bg-line"
                >
                  {/*
                    ★ 任务 8 改：`duration-slow`(420ms) ease-enter → `duration-base`(200ms)
                    ease-state。

                    420ms 是为**背景层首帧淡入**定的档（§5.1 里它是全站唯一
                    允许用 slow 的地方）。一条数据条用它就太慢了：这一页的指标
                    来自轮询，条长变化时用户已经看到了新数字，条却还在慢慢爬 ——
                    读起来是"界面卡了"而不是"数据更新了"。
                    200ms 是"看得出来在变、又不用等它"的长度。

                    曲线也换成 ease-state：条长变化是**状态切换**（数据更新），
                    没有方向；ease-enter 那种强减速是给"从无到有的入场"用的。

                    transition-[width] 是全站仅有的两处布局动画之一（另一处是
                    侧栏折叠）。这里可以接受：它只在一行内改一个元素的宽度，
                    不触发兄弟元素的 reflow，而且 5 条同时动也就 5 个元素。
                  */}
                  <div
                    className={cn(
                      'h-full rounded-xs transition-[width] duration-base ease-state',
                      meta.bg,
                    )}
                    style={{ width: `${width}%` }}
                  />
                </div>

                {/*
                  0 值用 ink-faint（5.9:1），不用 ink-ghost。
                  ghost 在 design-system §2 里写明"禁止承载任何信息"，而这里的 0
                  是一个有意义的计数（"这一级确实没有"）。规则的边界要守住。
                */}
                <span
                  className={cn(
                    'tabular w-6 shrink-0 text-right text-xs',
                    zero ? 'text-ink-faint' : 'text-ink',
                  )}
                  data-numeric
                  title={meta.definition}
                >
                  {entry.count}
                </span>
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}

export default SeverityChart
