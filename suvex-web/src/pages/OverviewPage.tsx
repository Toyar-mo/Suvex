import { PageHeader } from '@/components/layout/AppShell'
import ActivityFeed from '@/components/overview/ActivityFeed'
import AttentionList from '@/components/overview/AttentionList'
import RiskScoreCard from '@/components/overview/RiskScoreCard'
import ResourceStrip from '@/components/overview/ResourceStrip'
import { OverviewTasks } from '@/components/overview/OverviewTasks'
import SeverityChart from '@/components/overview/SeverityChart'
import StatCard from '@/components/overview/StatCard'
import { RecentFindingsCard } from '@/components/overview/RecentFindingsCard'
import { TokenUsagePanel } from '@/components/overview/TokenUsagePanel'
import ErrorState from '@/components/ui/ErrorState'
import NoScansYet from '@/components/ui/NoScansYet'
import { Skeleton, SkeletonRows } from '@/components/ui/Skeleton'
import { useFleetStatus } from '@/data/fleet'
import { useScans } from '@/data/scans-store'
import type { ActivityEvent, OverviewData } from '@/data/overview'
import { loadOverview, useOverviewState } from '@/data/overview-store'
import { loadFindings } from '@/data/findings-store'
import { useFindings } from '@/data/findings-store'
import { loadQuality, type QualitySnapshot } from '@/data/quality'
import { useOverviewLiveActivity } from '@/hooks/useOverviewLiveActivity'
import { cn } from '@/lib/cn'
import { formatDuration } from '@/lib/datetime'
import { ScanSearch, ShieldAlert, FlaskConical, Timer, Clock, Boxes } from 'lucide-react'
import { useEffect, useState } from 'react'

/**
 * Overview —— 用户看到的第一屏。
 *
 * 目标：**5 秒内明白系统现在的状态。** 这个约束决定了整个版面：
 *
 *   1. 第一屏是活动流（"正在发生什么"）—— 它是这一页的核心叙事，
 *      也是产品承诺本身。右栏并排放风险分与严重度分布（结论 + 明细）。
 *   2. 第二块是「该做什么」—— 有严重发现时它是有时限的。
 *   3. 四个指标卡退到第三块：它们是**积累量**，不该占掉第一屏。
 *
 * 版面顺序对应阅读顺序：过程 → 动作 → 数字。
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
  const [quality, setQuality] = useState<QualitySnapshot | null>(null)

  useEffect(() => {
    void loadOverview()
  }, [])

  useEffect(() => {
    // 质量是增值信息：失败静默（保持 null），不拖累总览首屏。
    void loadQuality()
      .then(setQuality)
      .catch(() => setQuality(null))
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

            ★ v1.79：空态也保留「可用能力」三格。

              它回答的是"这套部署能干什么"（工具 / 技能 / 模型），
              与"跑过没有"无关 —— 后端在 `hasAnyScan=False` 时同样返回
              `resources`，就是为了让**新装的系统**也能看到自己的家底。
              原来这里整块换成了 `NoScansYet`，那三格被一起藏掉了。
          */
          <div className="flex flex-col gap-bay">
            <NoScansYet description="给它一个目标，它会先摸清攻击面、寻找薄弱点，再逐一尝试用可复现的利用方式证明。整个过程会实时出现在这里。" />
            <ResourceStrip resources={data.resources} />
          </div>
        ) : (
          <>
            <FreshnessNote lastLoadedAt={lastLoadedAt} />
            <OverviewContent data={data} agents={agents} quality={quality} />
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
function QualityPanel({ quality }: { quality: QualitySnapshot | null }) {
  if (!quality) return null
  const percent = (value: number | null) =>
    value === null ? '—' : `${Math.round(value * 100)}%`
  const verifiedShare =
    quality.findings.total === 0 ? null : quality.findings.verified / quality.findings.total
  const tokens = quality.cost?.totalTokens ?? 0
  const tokensText =
    tokens >= 1_000_000
      ? `${(tokens / 1_000_000).toFixed(1)}M`
      : tokens >= 1_000
        ? `${(tokens / 1_000).toFixed(1)}k`
        : String(tokens)
  const usd = quality.cost?.usd ?? 0
  const rows = [
    { label: 'CTF 解题率', value: percent(quality.challenges.solveRate) },
    { label: '发现误报率', value: percent(quality.findings.falsePositiveRate) },
    { label: '已验证占比', value: percent(verifiedShare) },
    { label: '覆盖的面', value: String(quality.coverage.surfaces) },
    { label: '解题 token', value: tokensText },
    { label: '估算成本', value: `$${usd < 0.01 ? usd.toFixed(4) : usd.toFixed(2)}` },
  ]

  return (
    <section
      aria-label="质量指标"
      className="rounded-lg border border-line bg-surface p-panel"
    >
      <h3 className="text-sm font-medium text-ink">质量</h3>
      <p className="mt-1 text-2xs text-ink-muted">
        成功率 / 误报 / 覆盖度 —— 用来看“变好还是变差”，不只看数量。
      </p>
      <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2">
        {rows.map((row) => (
          <div key={row.label} className="flex flex-col">
            <dt className="text-2xs text-ink-muted">{row.label}</dt>
            <dd className="tabular text-sm text-ink">{row.value}</dd>
          </div>
        ))}
      </dl>
    </section>
  )
}


function OverviewContent({
  data,
  agents,
  quality,
}: {
  data: OverviewData
  agents: { running: number; queued: number }
  quality: QualitySnapshot | null
}) {
  const { metrics, activity, severityBreakdown, attention, resources } = data

  /**
   * 「现在有没有事在跑」—— 以**扫描列表**为准，而不是 `metrics.activeScans`。
   *
   * `metrics` 来自 60 秒轮询的 `/overview`，而扫描列表有活跃任务时每 2.5 秒
   * 刷新一次。刚创建的扫描要等最多一分钟才在 `activeScans` 里出现 ——
   * 那段时间里界面会说「最近活动」、不连实时流，而其实任务已经在跑了。
   * 两个来源取并集，只多不少；扫描结束后轮询也会把 `activeScans` 归零。
   */
  const isLive = metrics.activeScans > 0 || agents.running > 0 || agents.queued > 0

  const scans = useScans()
  const findings = useFindings()
  useEffect(() => {
    void loadFindings({ silent: true })
  }, [])
  const findingTotal = severityBreakdown.reduce((sum, item) => sum + item.count, 0)
  return (
    <div className="flex flex-col gap-bay">
      {/*
        ── 0 · 顶部 KPI（学 ARTEX 总览的信息架构）────

        五张卡回答「现在有什么」：任务、发现、资产、流量、token。
        流量卡如实显示「未接入」——Suvex 没有流量捕获，不能编一个 0。
      */}
      <section aria-label="关键指标" className="grid grid-cols-2 gap-panel lg:grid-cols-4">
        <StatCard
          label="活跃任务"
          value={metrics.activeScans}
          icon={ScanSearch}
          footnote={
            metrics.activeScans > 0
              ? [agents.running > 0 ? `${agents.running} 执行` : null, agents.queued > 0 ? `${agents.queued} 排队` : null]
                  .filter(Boolean)
                  .join(' · ')
              : '暂无任务'
          }
        />
        <StatCard
          label="确认发现"
          value={findingTotal}
          icon={ShieldAlert}
          footnote={severityBreakdown
            .filter((item) => item.count > 0)
            .map((item) => `${item.severity} ${item.count}`)
            .join(' · ') || '暂无发现'}
        />
        <StatCard
          label="资产节点"
          value={quality ? quality.coverage.targets : '—'}
          icon={Boxes}
          footnote={quality ? `覆盖记录 ${quality.coverage.surfaces} 条` : '质量数据未加载'}
        />
        <StatCard
          label="流量交互"
          value="—"
          icon={Clock}
          footnote="流量捕获未接入"
        />
      </section>

      <TokenUsagePanel />

      {/*
        ── 1 · 此刻在发生什么 + 留下了什么 ─────────────

        ★ 活动流从第三屏提到了第一屏（2026-11 设计审查）。

          改之前是「4 张指标卡 → 该做什么 → 可用能力 → 活动流」，
          实测 1440×900 下活动流标题落在 y=859、手机落在 1570 ——
          产品的承诺是「看它每一步在想什么」，而第一屏是一张静态报表。
          现在第一屏就是它，风险分与严重度分布并排放在右栏（结论 + 明细）。

        ★ 标题随状态切换「实时 / 最近」：没有任务在跑时，流里是历史事件，
          再叫「实时」就是在骗人。
      */}
      <div className="grid grid-cols-1 items-start gap-bay lg:grid-cols-[minmax(0,1fr)_20rem]">
        <div className="flex min-w-0 flex-col gap-bay">
          <OverviewActivityFeed seed={activity} isLive={isLive} />

      {/* ── 2 · 该做什么 ───────────────────────────── */}
      <AttentionList items={attention} />

      {/* ── 3 · 本周产出（与顶部 KPI 不重复）────────── */}
      <section aria-label="本周产出">
        <h2 className="mb-2 text-sm font-medium text-ink">本周产出</h2>
        <div className="grid grid-cols-1 gap-panel sm:grid-cols-3">
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
              metrics.avgScanDurationSec === null ? '—' : formatDuration(metrics.avgScanDurationSec)
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
        ── 4 · 可用能力（v0.96）──────────────────────

        ★ 放在指标卡之后：四张卡的共同语义是"本次统计窗口内的产出"，
          三格家底是"部署时的配置"。混进一排会让人以为"技能 31 篇"
          和"本周新增 18"是同一种数。

        ★ 它承担了「这系统能干什么」的回答：新装部署的活动流是空的，
          这一格告诉用户平台配了多少工具、多少技能。
      */}
      <OverviewTasks scans={scans} />

      <ResourceStrip resources={resources} />
        </div>

        <div className="flex flex-col gap-bay self-start">
          <RiskScoreCard
            score={metrics.riskScore}
            level={metrics.riskLevel}
            total={metrics.unresolvedTotal}
            breakdown={metrics.unresolvedBreakdown}
          />

          <SeverityChart data={severityBreakdown} />

          <RecentFindingsCard findings={findings} />
          <QualityPanel quality={quality} />
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
    </div>
  )
}

/**
 * 实时活动卡片的**数据壳**（v1.81）。
 *
 * ★ 为什么把 SSE 状态放在这里，而不是 `OverviewContent` 里：
 *
 *   `useOverviewLiveActivity` 每收到一批事件就会 setState。若那段状态
 *   长在页面正文里，每来一条事件都会把风险分、严重度图、指标卡、
 *   token 卡全部重渲染一遍 —— 而它们和这条事件毫无关系。状态下沉到
 *   这张卡片之后，变化只重渲染卡片自己的子树。
 *
 * 这也让「实时」的边界很明确：只有这一块会随事件流动，
 * 其余数字仍然按 60 秒轮询更新（口径见页面顶部那段注释）。
 */
function OverviewActivityFeed({ seed, isLive }: { seed: ActivityEvent[]; isLive: boolean }) {
  const { events, latestBatchIds, connection } = useOverviewLiveActivity(seed, isLive)

  return (
    <ActivityFeed
      events={events}
      isLive={isLive}
      limit={6}
      title={isLive ? '实时活动' : '最近活动'}
      latestBatchIds={latestBatchIds}
      connection={connection}
    />
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

      {/* 骨架顺序跟着真实版面走：先是活动流 + 右栏，再是该做什么，最后才是指标卡。 */}
      <div className="grid grid-cols-1 items-start gap-bay lg:grid-cols-[minmax(0,1fr)_20rem]">
        <SkeletonRows rows={6} rowClassName="h-11" />
        <Skeleton className="h-64 rounded-md bg-surface" />
      </div>

      <Skeleton className="h-16 rounded-md bg-surface" />

      <div className="grid grid-cols-1 gap-panel sm:grid-cols-2 xl:grid-cols-4">
        {Array.from({ length: 4 }, (_, index) => (
          <Skeleton key={index} className="h-[5.5rem] rounded-md bg-surface" />
        ))}
      </div>
    </div>
  )
}

export default OverviewPage
