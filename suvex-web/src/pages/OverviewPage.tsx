import { PageHeader } from '@/components/layout/AppShell'
import ActivityFeed from '@/components/overview/ActivityFeed'
import AttentionList from '@/components/overview/AttentionList'
import RiskScoreCard from '@/components/overview/RiskScoreCard'
import ResourceStrip from '@/components/overview/ResourceStrip'
import SeverityChart from '@/components/overview/SeverityChart'
import StatCard from '@/components/overview/StatCard'
import TokenUsageCard from '@/components/overview/TokenUsageCard'
import ErrorState from '@/components/ui/ErrorState'
import NoScansYet from '@/components/ui/NoScansYet'
import { Skeleton, SkeletonRows } from '@/components/ui/Skeleton'
import { useFleetStatus } from '@/data/fleet'
import { loadOverview, useOverviewState } from '@/data/overview-store'
import { cn } from '@/lib/cn'
import { formatDuration } from '@/lib/datetime'
import { ScanSearch, ShieldAlert, FlaskConical, Timer, Clock } from 'lucide-react'
import { useEffect, useState } from 'react'

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
/**
 * 总览页自动刷新周期（v0.95）。
 *
 * 60 秒抄自 CyberStrikeAI 的 `DASHBOARD_POLL_INTERVAL_MS`，理由见页面里
 * 那个 effect 的注释：这一页回答的是"积累了什么"，不是"此刻在做什么"。
 */
const OVERVIEW_POLL_INTERVAL_MS = 60 * 1000

/**
 * 超过这个年龄的数据要**显式标出来**（v0.95）。
 *
 * 5 分钟抄自 CyberStrikeAI 的 `DASHBOARD_STALE_THRESHOLD_MS`，
 * 但判据是我们自己算的：轮询是 60 秒，5 分钟意味着**至少连续失败 4 次**。
 * 偶发一次超时不该让用户怀疑数据；连续四次不再成功，就是真的拿不到新数据了。
 */
const OVERVIEW_STALE_THRESHOLD_MS = 5 * 60 * 1000

/**
 * 数据新鲜度提示条（v0.95）。
 *
 * ## 为什么这条必须存在
 *
 * 静默刷新失败时我们**保留旧数据**（`overview-store.ts` 里写明了理由：
 * 把已显示的看板换成错误页比"数字暂时没更新"糟得多）。这个取舍是对的，
 * 但它有一个必须补偿的副作用：**屏幕上可能显示着很久以前的数字，
 * 而用户毫不知情**。看板页上"看起来在实时更新、其实不是"比"显示不出来"
 * 危险得多 —— 用户会拿这个数字做决定。
 *
 * ## 为什么正常时不显示任何东西
 *
 * 正常状态下加一句"1 分钟前更新"是噪音：用户不需要在每个页面上
 * 确认系统在工作。只有**偏离预期**时才需要说话 ——
 * 这也让那句话说出口时真的被看见。
 */
function FreshnessNote({ lastLoadedAt }: { lastLoadedAt: number | null }) {
  // 用 state 触发重算：`Date.now()` 本身不会引起重渲染，
  // 没有这个 tick 的话提示条永远不会自己出现（要等下一次 60 秒轮询）。
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    // 30 秒重算一次就够 —— 阈值的量级是分钟，不需要每秒跳
    const timer = window.setInterval(() => setNow(Date.now()), 30 * 1000)
    return () => window.clearInterval(timer)
  }, [])

  if (lastLoadedAt === null) return null

  const age = now - lastLoadedAt

  // ★ 时钟回拨保护：`age < 0` 说明本机时间被往前调过（或服务端时间超前）。
  //   此时任何"多久之前"都是瞎猜，干脆什么都不说。
  //   不判这个的话会出现"数据来自 -3 分钟前"这种荒谬文案。
  if (age < 0) return null

  // 系统永远是"刚取到"的，不说话
  if (age < OVERVIEW_POLL_INTERVAL_MS * 2) return null

  const stale = age >= OVERVIEW_STALE_THRESHOLD_MS
  const minutes = Math.floor(age / 60000)

  return (
    <p
      role={stale ? 'status' : undefined}
      className={cn(
        'mb-bay flex items-center gap-1.5 rounded-sm border px-2.5 py-1.5 text-2xs',
        stale
          ? 'border-signal-warn/30 bg-signal-warn/5 text-signal-warn'
          : 'border-line text-ink-faint',
      )}
    >
      <Clock size={12} strokeWidth={1.75} aria-hidden="true" />
      {stale
        ? `这份数据是 ${minutes} 分钟前取的，之后一直没更新成功。数字可能已经过时。`
        : `数据来自 ${minutes} 分钟前，正在自动刷新。`}
    </p>
  )
}

function OverviewPage() {
  const { data, status, error, lastLoadedAt } = useOverviewState()

  useEffect(() => {
    void loadOverview()
  }, [])

  /*
   * ── 自动刷新（v0.95，学 CyberStrikeAI 的 60 秒轮询）──
   *
   * ★ 为什么这里是 60 秒轮询而不是 SSE：
   *   总览回答的是"积累了什么"，不是"此刻正在做什么"。一个漏洞被发现
   *   一分钟后再出现在总览上，对决策没有任何影响；而为了这一分钟的
   *   新鲜度给首屏挂一条长连接，代价是每次切页都要重连、断线要重试、
   *   还要为"连接活着但没数据"写超时判据 —— 那是活动流（Agent Activity）
   *   该承担的成本，不是这一页的。
   *
   * ★ 用 `setInterval` + 静默刷新（`silent: true`），且**只在页面可见时**：
   *   切到后台标签页还每 60 秒打一次接口，是纯浪费 —— 而 `silent: true`
   *   会让失败保持旧数据（不闪错误页），所以"上次更新时间"就是用户
   *   判断数据是否可信的唯一依据，那个徽章不能省。
   *
   * ⚠️ 依赖数组刻意是 `[]`：`loadOverview` 是模块级函数，引用恒定；
   *    把 `status` 之类放进来会让每次状态变化都重建定时器，
   *    实际刷新间隔变成"随状态变化"而不是 60 秒。
   */
  useEffect(() => {
    const timer = window.setInterval(() => {
      // 后台标签页不刷新：可见性变化时会立刻补一次，不会漏
      if (document.visibilityState !== 'visible') return
      void loadOverview({ silent: true })
    }, OVERVIEW_POLL_INTERVAL_MS)

    // 从后台切回来时立刻刷一次 —— 否则最长要等满一个周期
    const onVisible = () => {
      if (document.visibilityState === 'visible') void loadOverview({ silent: true })
    }
    document.addEventListener('visibilitychange', onVisible)

    return () => {
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisible)
    }
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
          <>
            <FreshnessNote lastLoadedAt={lastLoadedAt} />
            <OverviewContent data={data} agents={agents} />
          </>
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
  const { metrics, activity, severityBreakdown, attention, resources } = data

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

      {/*
        ── 2 · 该做什么（v0.95）────────────────────────

        ★ 位置在指标卡**之后、活动流之前**，这是刻意的：
          用户打开总览页的第一件事不是"看看发生了什么"，而是
          "有没有我该处理的"。把这块放在活动流下面，就等于让
          实时日志的滚动画面的喧闹盖住"你有 3 个严重漏洞"。

        ★ 它在没有待办时**仍然渲染**（显示那段"检查了四项"的说明），
          不隐藏：一块会消失的区块比一块说"我查过了，没事"的区块
          更让人怀疑 —— 消失时无法区分"没事"与"这块坏了"。
      */}
      <AttentionList items={attention} />

      {/*
        ── 2.5 · 可用能力（v0.96）──────────────────────

        ★ 位置在「该做什么」**之后、活动流之前**，两个理由：

          1. 「该做什么」是有时限的（有严重漏洞就现在去修），
             「可用能力」是常驻家底（多少工具、多少技能）。
             急的排前面，这是同一页里唯一合理的顺序。

          2. 它在**没有待办时承担了"这系统能干什么"的回答**。
             新装部署的「该做什么」是一段"没有需要你处理的事"，
             紧接着就是一片空的活动流 —— 中间没有任何东西告诉用户
             这平台配了 20 个工具和 31 篇技能。这一格把那句话补上。

        ★ 不放进上面那个 4 列指标卡网格：那四张卡的共同语义是
          "本次统计窗口内的产出"，三格家底是"部署时的配置"。
          混进一排会让人以为"技能 31 篇"和"本周新增 18"是同一种数。
      */}
      <ResourceStrip resources={resources} />

      {/* ── 3 + 4 · 活动流 与 严重度分布 ─────────────── */}
      <div className="grid grid-cols-1 gap-bay lg:grid-cols-[minmax(0,1fr)_20rem]">
        <ActivityFeed events={activity} isLive={metrics.activeScans > 0} limit={6} />

        {/*
          右栏放两张卡：风险分在上（它是结论），严重度分布在下（它是明细）。
          ★ `self-start` 只给最外层容器，两张卡各自按内容高度排列 ——
            之前 `SeverityChart` 直接 `self-start`，加一张卡之后
            需要把它们包一层，否则第二张会被 grid 拉伸到与左栏等高。
        */}
        <div className="flex flex-col gap-bay self-start">
          <RiskScoreCard
            score={metrics.riskScore}
            level={metrics.riskLevel}
            total={metrics.unresolvedTotal}
            breakdown={metrics.unresolvedBreakdown}
          />

          <SeverityChart data={severityBreakdown} />
        </div>
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
