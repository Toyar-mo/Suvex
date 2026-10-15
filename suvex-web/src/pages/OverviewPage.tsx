import { PageHeader } from '@/components/layout/AppShell'
import ActivityFeed from '@/components/overview/ActivityFeed'
import SeverityChart from '@/components/overview/SeverityChart'
import StatCard from '@/components/overview/StatCard'
import TokenUsageCard from '@/components/overview/TokenUsageCard'
import ErrorState from '@/components/ui/ErrorState'
import NoScansYet from '@/components/ui/NoScansYet'
import { Skeleton, SkeletonRows } from '@/components/ui/Skeleton'
import { useFleetStatus } from '@/data/fleet'
import { loadOverview, useOverviewState } from '@/data/overview-store'
import { formatDuration } from '@/lib/datetime'
import { ScanSearch, ShieldAlert, FlaskConical, Timer } from 'lucide-react'
import { useEffect } from 'react'

/**
 * Overview —— 用户看到的第一屏。
 *
 * 目标：**5 秒内明白系统现在的状态。** 这个约束决定了整个版面：
 *
 *   1. 四个指标卡在最上，高度统一，一行扫过。（"现在有多少、发现了多少"）
 *   2. 左栏是实时活动流（"正在发生什么"）—— 它是这一页的核心叙事，
 *      占主要宽度，因为它是唯一会动、也是唯一需要读的东西。
 *   3. 右栏是严重度分布（"留下了什么"）—— 静态、结构化，窄栏足够。
 *
 * 版面顺序对应阅读顺序：数字 → 动作 → 结果。
 *
 * 宽屏两栏，<lg 单栏堆叠（活动流在前，分布在后 —— 顺序不变，
 * 因为"正在发生什么"比"历史分布"更该先看到）。
 *
 * ## 任务 7：整页读一个端点
 *
 * 原来是前端从 findings-store + scans-store 现场算的（`buildOverviewData`，
 * 用的是两份 mock）。现在读 `GET /overview`：后端在同一个事务里算出
 * 全部指标、活动流与分布。
 *
 * ⚠️ 那条 `StateToggle`（"示例数据 / 空状态"切换按钮）连同 mock 一起删了。
 *   它存在的唯一理由是"空状态必须能被验收，而数据是假的、清不空"。
 *   现在空状态是**真的**（新装的系统一条扫描都没有），
 *   `hasAnyScan` 由后端如实给出 —— 不需要一个只能骗自己的开关。
 */
function OverviewPage() {
  const { data, status, error } = useOverviewState()

  useEffect(() => {
    void loadOverview()
  }, [])

  const { agents } = useFleetStatus()

  return (
    <>
      <PageHeader navId="overview" />

      <div className="px-gutter py-bay">
        {/*
          三态（任务 7 要求）。顺序很重要：loading 先于空状态 ——
          反过来的话，新用户会先看到「还没有跑过扫描」，
          而真相是数据还在路上。首屏闪一下假空状态是最刺眼的。
        */}
        {status === 'loading' ? (
          <OverviewSkeleton />
        ) : status === 'error' || !data ? (
          <ErrorState
            title="总览数据加载失败"
            error={error}
            onRetry={() => void loadOverview({ silent: false })}
          />
        ) : !data.hasAnyScan ? (
          /*
            ★ 「一次都没跑过」时整块换掉，**不是**渲染四个 0 值卡片。
              `hasAnyScan` 由后端给出（它知道得比前端的任何推导都准）——
              前端不再需要那个"示例数据 / 空状态"的开发开关来假装空态。
          */
          <NoScansYet description="给它一个目标，它会先摸清攻击面、寻找薄弱点，再逐一尝试用可复现的利用方式证明。整个过程会实时出现在这里。" />
        ) : (
          <OverviewContent data={data} agents={agents} />
        )}
      </div>
    </>
  )
}

/**
 * 有数据时的正文。
 *
 * 单独拆出来是因为它需要 `data` 已收窄为非空 —— 在主组件里那需要
 * 一层 `data!` 或者把整块 JSX 塞进三元里，两者都会让"三态"
 * 这条主线淹没在缩进里。
 */
function OverviewContent({
  data,
  agents,
}: {
  data: import('@/data/overview').OverviewData
  agents: { running: number; queued: number }
}) {
  const { metrics, activity, severityBreakdown } = data

  return (
    <div className="flex flex-col gap-bay">
      {/* ── 1 · 关键指标 ───────────────────────────── */}
      <section aria-label="关键指标">
        <div className="grid grid-cols-1 gap-panel sm:grid-cols-2 xl:grid-cols-4">
          <StatCard
            label="进行中的扫描"
            value={metrics.activeScans}
            icon={ScanSearch}
            footnote={
              metrics.activeScans > 0
                ? `${agents.queued > 0 ? `${agents.queued} 个排队 · ` : ''}${agents.running} 个智能体在跑`
                : '当前没有任务在跑'
            }
          />

          <StatCard
            label="本周新增高危及以上"
            value={metrics.newHighSeverityThisWeek}
            icon={ShieldAlert}
            delta={metrics.newHighSeverityDelta}
            deltaIntent="negative-when-up"
            deltaLabel="较上周"
          />

          <StatCard
            label="已验证的 PoC"
            value={metrics.verifiedPocCount}
            icon={FlaskConical}
            footnote="由智能体实际复现，而非仅报告"
          />

          <StatCard
            label="平均扫描耗时"
            value={
              metrics.avgScanDurationSec === null
                ? '—'
                : formatDuration(metrics.avgScanDurationSec)
            }
            icon={Timer}
            footnote="统计已完成的扫描"
          />
        </div>

        {/*
          如实说明有 N 条被判为误报、**未计入上面两张指标卡**。
          不加这句的话，用户会以为"本周新增"漏统计了。
          注意这句话挂在指标卡下方而不是图表下方 ——
          图表的口径包含全部发现（与 Findings 页逐项相等），
          只有"本周新增高危"和"已验证 PoC"这两张卡排除了误报。
          把说明放错位置会让人以为图表也排除了。
        */}
        {metrics.falsePositiveCount > 0 && (
          <p className="mt-tight text-2xs text-ink-faint">
            上面两张指标卡已排除 {metrics.falsePositiveCount} 条被判为误报的条目；
            下方分布图包含全部发现。
          </p>
        )}
      </section>

      {/* ── 2 + 3 · 活动流 与 严重度分布 ─────────────── */}
      <div className="grid grid-cols-1 gap-bay lg:grid-cols-[minmax(0,1fr)_20rem]">
        <ActivityFeed events={activity} isLive={metrics.activeScans > 0} limit={6} />

        <SeverityChart data={severityBreakdown} className="self-start" />
      </div>

      {/*
        ── 4 · LLM 消耗（CTF2 解题，v0.36）─────────────

        ★ 为什么单独一整块、而不是第五张指标卡：`StatCard` 的契约是
          "一个标签 + 一个数值"，而 token 这张要说**三种不同的空**
          （没有记录 / 接入点没给 / 只是下限），还要带按题细分。
          塞进 `StatCard` 会让那个可复用件的接口长出一堆 token 专属字段。

        ★ 位置在活动流之后：它是**成本回顾**，不是"现在在发生什么"。
          首屏最上面那四张卡回答的是"现在怎么样"，这一块回答
          "我们为此付出了多少"。
      */}
      <TokenUsageCard
        calls={metrics.ctf2LlmCalls}
        recordedCalls={metrics.ctf2RecordedCalls}
        totalTokens={metrics.ctf2TotalTokens}
        since={metrics.ctf2UsageSince}
      />
    </div>
  )
}

/**
 * 首屏加载骨架。
 *
 * ★ 形状照着真实版面来：**四个等宽卡片一行 + 下方两栏**。
 *   骨架的高度节奏必须贴近真实内容，否则数据到达的瞬间整页会往上跳一次 ——
 *   那一下比多等 200ms 更让人不适（这条规矩在 `Skeleton` 的文件头写过，
 *   这里是它的第一个真正的用武之地：Overview 是唯一"整页都是数字"的页面）。
 */
function OverviewSkeleton() {
  return (
    <div className="flex flex-col gap-bay" role="status" aria-live="polite" aria-busy="true">
      <span className="sr-only">正在加载总览数据</span>

      <div className="grid grid-cols-1 gap-panel sm:grid-cols-2 xl:grid-cols-4">
        {Array.from({ length: 4 }, (_, index) => (
          <Skeleton key={index} className="h-[5.5rem] rounded-md bg-surface" />
        ))}
      </div>

      <div className="grid grid-cols-1 gap-bay lg:grid-cols-[minmax(0,1fr)_20rem]">
        <SkeletonRows rows={6} rowClassName="h-11" />
        <Skeleton className="h-64 rounded-md bg-surface" />
      </div>
    </div>
  )
}

export default OverviewPage
