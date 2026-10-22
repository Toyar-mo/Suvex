import EventStream, { StreamToolbar } from '@/components/activity/EventStream'
import AgentRoster from '@/components/activity/AgentRoster'
import { HintComposer } from '@/components/activity/HintComposer'
import NoScansYet from '@/components/ui/NoScansYet'
import { FlagChip } from '@/components/ui/FlagChip'
import type { Agent } from '@/data/activity'
import { createSseEventSource } from '@/data/activity-stream'
import { loadScanHints, type ScanHint } from '@/data/scan-hints'
import { stallInfo, type Scan } from '@/data/scan'
import { loadScans, useScansState } from '@/data/scans-store'
import { useActivityStream } from '@/hooks/useActivityStream'
import { api } from '@/lib/api'
import { cn } from '@/lib/cn'
import { useEffect, useMemo, useState } from 'react'
import { AlertTriangle, Radio } from 'lucide-react'
import { Link, useSearchParams } from 'react-router-dom'

/**
 * Agent Activity —— 平台的视觉核心。
 *
 * 版面：左侧名册 + 右侧事件流，两栏各自独立滚动。
 *
 * 为什么这一页不跟其他页面共用「页面头 + 内容」的常规布局：
 * 它是一块**常驻的工作台**，不是一篇文档。页面头（大标题 + 说明）会占掉
 * 80px 且在滚动时完全无用；这里改成一行紧凑的工具条，
 * 把这 80px 还给事件流 —— 多出两行可见事件。
 *
 * 高度：`h-[calc(100dvh-...)]` 锁死视口高度，内部两栏各自滚动。
 * 这是少数**必须**用内部滚动容器的场景（其他页面都让 document 滚动）：
 * 事件流要的是一个自包含的滚动区，滚到顶/底不该带动整页。
 * 用 dvh 而不是 vh —— 移动端地址栏收起时 vh 会算错。
 *
 * 信息密度控制（这是本页最容易失控的地方）：
 *   - 一屏的事件条数由「折叠态高度」决定，约 5–6 条。这不是偶然：
 *     每条折叠态 3 行（摘要 + 事实行 + 展开按钮），行高按中文调过。
 *   - **默认全部折叠**。详情是"我要看"才展开的，不是默认铺开的。
 *   - 类型只用三种颜色（见 activity.ts 的 EVENT_TYPE_TONE 注释）。
 */

/**
 * 稳定的空名册引用。
 *
 * ⚠️ 必须定义在**模块级**，而不是每次渲染写一个字面量 `[]`：
 *    `useActivityStream` 里 `agents` 是 `useMemo(..., [events, roster])`，
 *    传入新数组会让它每次渲染都重算，重算结果又被当成新 props ——
 *    名册组件跟着每帧重渲染。
 */
const EMPTY_ROSTER: Agent[] = []

export function ActivityPage() {
  /**
   * 从 URL 读要看的扫描（Scans 页「查看活动」的落点是 `/activity?scan=<id>`）。
   *
   * ★ 它必须在创建事件源**之前**读到 —— 源是按 `scanId` 订阅的。
   *   不带参数时订阅**全局流**（`/api/v1/events`），用于同时看多个扫描。
   */
  const [searchParams] = useSearchParams()
  const scanFilter = searchParams.get('scan')

  /**
   * 事件源 —— 真实 SSE（契约 §4）。
   *
   * **按 scanFilter 重建**：切换扫描就是换一条流，复用旧连接会把两个
   * 扫描的事件混在一起。
   *
   * 依赖是 `[scanFilter]` 而不是空数组：源持有 `EventSource`，
   * 每次渲染重建会泄漏连接（旧的不 close 就一直挂着）。
   * 关闭由 `useActivityStream` 的清理函数负责。
   */
  const source = useMemo(() => createSseEventSource({ scanId: scanFilter }), [scanFilter])

  /**
   * 扫描集合 —— 只用来判断**"从来没跑过"**这一种空态。
   *
   * ⚠️ 空事件流有三种含义完全不同的成因，第三种（一次扫描都没有）需要
   *    "去发起第一次扫描"这个下一步，而前两种需要的是一句解释。
   *    见 `EmptyStream`。
   */
  const { scans, status: scansStatus } = useScansState()

  /**
   * 当前正在看的这次扫描若被判定为疑似卡死，给出提示（任务 11）。
   *
   * 只在**指定了某次扫描**时判定 —— 全局流没有"这次扫描"的概念，
   * 拿别的扫描的卡死状态来提示会让人以为是当前这条流卡了。
   *
   * 用 `scans` 里的那条而不是另发请求：扫描列表本来就在轮询
   * （running 时 2.5 秒一次），卡死标记会在下一轮自动出现、恢复时自动消失。
   */
  const stalledScan = useMemo(() => {
    if (!scanFilter) return null
    const scan = scans.find((s) => s.id === scanFilter)
    return scan ? stallInfo(scan) : null
  }, [scanFilter, scans])

  /**
   * 当前这次扫描的 flag（任务 18）。
   *
   * ★ 它放在这一页的工具栏里，是因为**用户就是这么被卡住的**：
   *   任务跑完、agent 在结论里说"flag 已确认"，而那一行摘要是被截断的
   *   JSON（flag 正好落在截断点之后），于是他说"没看到 flag"。
   *   把结果放在他正盯着的这一页顶部，比藏在事件详情里有用得多。
   *
   * 与 `stalledScan` 一样取自已经在轮询的 `scans`，不另发请求。
   */
  const filteredScan = useMemo(
    () => (scanFilter ? (scans.find((s) => s.id === scanFilter) ?? null) : null),
    [scanFilter, scans],
  )

  useEffect(() => {
    void loadScans()
  }, [])

  /**
   * 人给智能体补的线索（P2 人机协同，v0.57）。
   *
   * ## 为什么要在这里也存一份
   *
   * 线索不是事件流的一部分（它不进 `activity_events`），所以不能从 SSE
   * 派生。这里单独维护一个列表，用途有二：
   *   1. 让用户在事件流里看到「我给过哪些线索」，而不是输入完就消失；
   *   2. 显示每条线索是**待送达**还是**已送达**（`consumed`）——
   *      只有能区分这两者，用户才不会因为"没看到它在动"而反复输入。
   *
   * ## 只在指定扫描时加载 / 显示
   *
   * 全局流（没有 `?scan=`）下没有"这一次"的概念，补线索也无从谈起。
   *
   * 轮询而不是只拉一次：线索被引擎取走后 `consumed` 会从 false 变 true，
   * 只拉一次就永远停在"待送达"，反而是误导。
   */
  const [hints, setHints] = useState<ScanHint[]>([])
  useEffect(() => {
    if (!scanFilter) {
      setHints([])
      return
    }
    let cancelled = false
    const refresh = () => {
      loadScanHints(scanFilter)
        .then((list) => {
          if (!cancelled) setHints(list)
        })
        .catch(() => {
          /* 线索回显失败不影响事件流 —— 它是次要部分，不弹错页 */
        })
    }
    refresh()
    const timer = window.setInterval(refresh, 5000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [scanFilter])

  const isRunning = filteredScan?.status === 'running'

  /**
   * agent 名册 —— `GET /scans/{id}/agents`。
   *
   * ⚠️ 它只按扫描取。全局流（没有 `scanId`）时**留空**，不伪造一份名册：
   *    名册回答的是"这一次扫描派了哪些 agent"，跨扫描混在一起就没有意义。
   *    那种情况下 agent 统计仍能从事件流派生（见 `useActivityStream`）。
   */
  const [rosterState, setRosterState] = useState<{ scanId: string | null; agents: Agent[] }>({
    scanId: null,
    agents: EMPTY_ROSTER,
  })

  useEffect(() => {
    if (!scanFilter) return
    let cancelled = false
    api
      .get<Agent[]>(`/scans/${scanFilter}/agents`)
      .then((list) => {
        if (!cancelled) setRosterState({ scanId: scanFilter, agents: list })
      })
      .catch(() => {
        /*
          名册拉不到**不影响事件流**：右边的事件照常在滚。
          弹一个错误会把"次要部分失败"表现成整页故障，
          而用户真正来看的是那个流。
        */
        if (!cancelled) setRosterState({ scanId: scanFilter, agents: EMPTY_ROSTER })
      })
    return () => {
      cancelled = true
    }
  }, [scanFilter])

  /*
    ★ 用**派生**代替"在 effect 里清空名册"。

    原来写的是 `if (!scanFilter) setRoster([])`，有两个问题：
      1. 同步 setState 写在 effect body 里会触发 react-hooks 的
         `set-state-in-effect`（同步 setState → 级联渲染）；
      2. 更实际的一个：切换扫描时，旧名册会**先显示一帧**再被清掉 ——
         用户看到上一个扫描的 agent 列表闪一下。

    把 scanId 和 agents 存在一起、只在匹配时采用，两个问题一起解决：
    不匹配就等同于空，不需要任何"清空"动作。
  */
  const roster = rosterState.scanId === scanFilter ? rosterState.agents : EMPTY_ROSTER

  const { events, agents, connection, isPaused, setPaused, newWhilePaused, latestBatchIds } =
    useActivityStream(source, roster)

  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null)

  const scanEvents = useMemo(
    () => (scanFilter === null ? events : events.filter((e) => e.scanId === scanFilter)),
    [events, scanFilter],
  )

  const visibleEvents = useMemo(
    () =>
      selectedAgentId === null
        ? scanEvents
        : scanEvents.filter((e) => e.agentId === selectedAgentId),
    [scanEvents, selectedAgentId],
  )

  /** 暂停期间各 agent 的未读条数 —— 让用户知道是谁在他没看时又动了 */
  const unreadByAgent = useMemo(() => {
    if (!isPaused || newWhilePaused === 0) return {}
    const recent = events.slice(-newWhilePaused)
    const result: Record<string, number> = {}
    for (const e of recent) result[e.agentId] = (result[e.agentId] ?? 0) + 1
    return result
  }, [events, isPaused, newWhilePaused])

  const selectedAgent = selectedAgentId
    ? (agents.find((a) => a.id === selectedAgentId) ?? null)
    : null

  return (
    <div className="flex min-h-0 flex-1 flex-col md:flex-row">
      {/*
        ── 左/上：智能体名册 ──

        桌面：左侧竖直栏，独立滚动
        窄屏：顶部横向一行，**不占纵向空间**（高度由内容决定，约 58px）

        这里刻意不在外层包 flex 容器：AgentRoster 自己管两种形态，
        外层只负责边框与背景。之前用 `display:contents` 把内层拉平到
        flex-row 外层里，结果名册项按 58px 竖直堆叠，在 320px 屏上
        把事件流挤到只剩 38px。
      */}
      <aside
        className={cn(
          'shrink-0 border-line bg-surface/40',
          // 窄屏：顶部条，横向滚动，不参与纵向分配
          'border-b',
          // 桌面：左侧栏，撑满高度并独立滚动
          'md:w-60 md:overflow-y-auto md:border-b-0 md:border-r',
        )}
      >
        <AgentRoster
          agents={agents}
          selectedAgentId={selectedAgentId}
          onSelect={setSelectedAgentId}
          unreadByAgent={unreadByAgent}
          /*
            ⚠️ 用 `scanEvents` 而**不是** `events`。

            两者现在恰好相等 —— 带 `?scan=` 进来时，SSE 订阅的就是那一条
            扫描的流（`/scans/{id}/events`），所以 `events` 里本来就只有它。

            但那是一个**隐含前提**：这个数字的正确性依赖"订阅源已经过滤过"，
            而它在代码里看不出来。一旦哪天改成"订阅全局流 + 前端按 scanId 过滤"
            （这是很自然的一步演进，`scanEvents` 那个 useMemo 已经在了），
            这里就会显示**全站**事件总数，而用户以为是这次扫描的 ——
            数字偏大且看起来完全合理，属于最难发现的一类错。

            改成 `scanEvents` 之后，过滤这件事在**使用点**上显式成立，
            不再依赖数据源的巧合。`visibleEvents` 同样基于它。
          */
          totalEvents={scanEvents.length}
          className="md:min-h-full"
        />
      </aside>

      {/* ── 右：事件流 ── */}
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <StreamToolbar
          isPaused={isPaused}
          onPauseChange={setPaused}
          eventCount={visibleEvents.length}
          connection={connection}
        >
          {/*
            扫描上下文。从 Scans 页点「详情」进来时这里显示是哪个扫描 ——
            没有它，用户无法确认自己看的是不是那一次扫描。
            做成链接，点击回到扫描列表。
          */}
          {scanFilter && (
            <Link
              to="/scans"
              className="mono-ui inline-flex items-center gap-1 rounded-xs border border-line px-1.5 py-0.5 text-ink-muted transition-ui duration-fast hover:border-line-strong hover:text-ink"
              title="返回扫描列表"
            >
              {scanFilter}
            </Link>
          )}
          {selectedAgent && (
            <span className="mono-ui rounded-xs border border-line px-1.5 py-0.5 text-ink-muted">
              {selectedAgent.id}
            </span>
          )}
          {/*
            ★ flag（任务 18）。放在工具栏里、紧跟扫描上下文 ——
            它和那个 id 是同一个性质的东西："我现在看的这一次，结论是什么"。

            用 `full` 变体：这里是"结论"的展示位，不该被省略号截断 ——
            用户要判断的就是那串字符对不对。复制靠点它。
          */}
          {filteredScan?.flag && <FlagChip flag={filteredScan.flag} variant="full" />}
          {/*
            ★「疑似卡死」提示（任务 11）。

            为什么放在**这一页**：32 分钟零产出那次，用户盯着的就是这里 ——
            一个空转的事件流。提示放在扫描列表里没用，因为他不在那一页。

            措辞与列表一致（都取自 `stallInfo`），不在这里另写一套。
          */}
          {stalledScan && (
            <span
              className="inline-flex items-center gap-1 rounded-xs border border-signal-warn/40 px-1.5 py-0.5 text-signal-warn"
              title={stalledScan.hint}
            >
              <AlertTriangle size={11} strokeWidth={2.25} aria-hidden="true" />
              疑似卡死 · {stalledScan.silentLabel}
            </span>
          )}
        </StreamToolbar>

        {/*
          ★ 运行中补线索（P2 人机协同）。只在「这次扫描是 running」时出现 ——
          排队中该写任务说明、已结束没有作用点（与后端 add_hint 的 409 判断一致）。
        */}
        {scanFilter && isRunning && (
          <HintComposer
            scanId={scanFilter}
            onAdded={(hint) => setHints((prev) => [...prev, hint])}
          />
        )}

        {/*
          已补的线索回显。贴在事件流上方，让用户看见「我给的线索还在不在、
          送没送到模型」。没有它，输入完就消失的框会让人怀疑是否真的生效。
          `consumed` 区分「排队中」与「已送达当前轮」。
        */}
        {scanFilter && hints.length > 0 && (
          <div className="flex flex-wrap items-center gap-1 border-b border-line px-panel py-1">
            <span className="text-2xs text-ink-faint">已补线索 {hints.length} 条：</span>
            {hints.slice(-4).map((h) => (
              <span
                key={h.id}
                className={cn(
                  'inline-flex max-w-[28ch] items-center gap-1 truncate rounded-xs border px-1.5 py-0.5 text-2xs',
                  h.consumed
                    ? 'border-line text-ink-muted'
                    : 'border-signal-warn/40 text-signal-warn',
                )}
                title={
                  h.consumed
                    ? `已送达模型（第 ${h.consumedTurn ?? '?'} 轮）`
                    : '还在排队，等智能体下一轮开始时会看到'
                }
              >
                {h.consumed ? '已送达' : '排队中'} · {h.text}
              </span>
            ))}
          </div>
        )}

        <EventStream
          events={visibleEvents}
          latestBatchIds={latestBatchIds}
          filtered={selectedAgentId !== null}
          isPaused={isPaused}
          newWhilePaused={newWhilePaused}
          onPauseChange={setPaused}
          emptyState={
            <EmptyStream
              selectedAgentId={selectedAgentId}
              scanFilter={scanFilter}
              /* 空事件流的第三种成因：一次扫描都没跑过 */
              noScansYet={scansStatus === 'ready' && scans.length === 0}
              /* 最近一次扫描 —— 全局流没有 replay，用它给一条"看得到事件"的路 */
              latestScanId={scans[0]?.id ?? null}
              /*
                当前这次扫描的状态（任务 12）。用来区分「正在启动」与
                「已经结束但没事件」—— 后者要说的是另一句话。
              */
              currentScan={scanFilter ? (scans.find((s) => s.id === scanFilter) ?? null) : null}
            />
          }
        />
      </div>
    </div>
  )
}

/**
 * 空状态。要区分**三种**情况，因为它们的含义完全不同：
 *   1. 指定了某个扫描，但它还没有事件（如刚创建、还在排队）
 *   2. 选中了某个 agent，它还没产出
 *   3. 整体还没有任何事件
 *
 * 第一种最容易做错：只有一次扫描在产生事件时，从 Scans 页点进
 * 别的扫描就是空的。这里**不伪造事件去填满它** —— 那会让人以为那个扫描
 * 真的在跑。如实说明"还没开始产生事件"才是诚实的呈现。
 *
 * ## 第三种还要再分两层（任务 7 补）
 *
 * 「整体还没有事件」有两种成因，而它们需要的下一步完全不同：
 *
 *   · **一次扫描都没跑过** —— 用户还没开始。要给按钮，否则他就停在这一页。
 *   · **跑过了，但事件已经过去** —— 这一条是接真实 SSE 之后才出现的：
 *     全局流按契约 §4.1 **从订阅那一刻起推送**，没有 replay。
 *     所以刚跑完一次扫描的人切到这一页，看到的是"还没有事件"，
 *     而系统里明明躺着 5 条记录。那时需要的是**一条能真的看到它们的路**
 *     （切到那一次扫描），以及一句解释 —— 否则用户会以为事件丢了。
 *
 * 混成一句"还没有事件"的话，前者是死胡同，后者是**假故障**。
 *
 * ## ★ 第四种：指定了扫描、它还在**启动**（任务 12 实测补上）
 *
 * 起因是一次真实的困惑：用户创建扫描后 31 秒点进这一页，看到的是
 * 「这次扫描还没有产生事件。扫描排队结束、智能体开始工作后，事件会
 * 实时出现在这里。」—— 于是他来问"活动这里怎么是空的"。
 *
 * 而那条扫描**完全正常**：实测第一条事件在**启动后 1 分 35 秒**才出现
 * （strix 要起容器、加载工具、跑第一轮 LLM）。所以那句话本身没错，
 * 错在**它没给时间预期** —— 用户无法区分「正在启动」与「已经挂了」，
 * 而这两者需要的动作完全相反（继续等 vs 去查问题）。
 *
 * 这与任务 11 修的"32 分钟卡死"是同一类病：**缺状态可见性**。
 * 所以启动期要给出「已等待多久」与「通常要多久」，见 `StartupNotice`。
 */
/**
 * 「正在启动」提示（任务 12）。
 *
 * 它要有**会走的秒数**，否则用户盯着一个静止的页面仍然分不清
 * "在跑"与"卡住了" —— 而一个自己会变的数字本身就是"它还活着"的证据。
 *
 * ## 为什么每秒一跳
 *
 * 这个组件只在启动窗口存活（通常 1–2 分钟，事件一来就被替换掉），
 * 所以每秒一次重渲染的代价可以忽略；而秒数跳动带来的"确实在动"
 * 比 30 秒一跳强得多。刻意**不复用** `formatDuration`：
 * 那个函数 60 秒以上会转成"分钟"，而这里最需要看清的恰恰是前 60 秒。
 */
function StartupNotice({ scanId, startedAt }: { scanId: string; startedAt: string | null }) {
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [])

  /*
    ⚠️ 夹到 0：`startedAt` 是**服务端**时间，而浏览器时钟可能慢几秒 ——
    不夹的话会显示"已等待 -3 秒"。相对时长不敏感钟差，但负数是荒谬的。
    （与 `stallInfo` 夹负数是同一条理由。）
  */
  const started = startedAt ? new Date(startedAt).getTime() : Number.NaN
  const elapsedSec = Number.isFinite(started)
    ? Math.max(0, Math.round((now - started) / 1000))
    : null
  const overdue = elapsedSec !== null && elapsedSec > 180

  return (
    <div className="flex max-w-[56ch] flex-col items-center gap-tight text-center">
      {/*
        用呼吸的点而不是静态图标：它是"正在进行"的语义，
        与 StatusDot 的 pulse 规则一致（design-system §2）。
      */}
      <span className="relative flex h-2.5 w-2.5" aria-hidden="true">
        <span className="absolute inline-flex h-full w-full animate-ping rounded-pill bg-accent opacity-60" />
        <span className="relative inline-flex h-2.5 w-2.5 rounded-pill bg-accent" />
      </span>

      <p className="text-sm text-ink-muted">
        正在启动
        {elapsedSec !== null && (
          <>
            <span aria-hidden="true"> · </span>
            <span className="tabular" data-numeric>
              已等待 {elapsedSec} 秒
            </span>
          </>
        )}
      </p>

      <p className="text-xs leading-relaxed text-ink-faint">
        扫描进程已就绪，正在准备沙箱与第一轮分析。通常需要
        <strong className="font-normal text-ink-muted"> 1–2 分钟 </strong>
        才会出现第一条事件，这段时间里没有输出是正常的。
      </p>

      {/*
        ★ 超过 3 分钟**换一句话**，而不是继续安慰。

        那时还写"请耐心等待"就是在替系统掩盖问题。改成指路。
        这与任务 11 的"疑似卡死"是同一立场的两级：那边是**判定并标记**，
        这里是**早期提示并指路**。
      */}
      {overdue && (
        <p className="mt-tight max-w-[50ch] text-2xs leading-relaxed text-signal-warn">
          启动超过 3 分钟仍无输出，可能不正常。可在「扫描任务」页查看这次扫描的详情与失败原因。
        </p>
      )}

      <Link
        to={`/scans?created=${encodeURIComponent(scanId)}`}
        className="mt-tight text-xs text-accent-strong underline hover:text-accent"
      >
        查看这次扫描的状态
      </Link>
    </div>
  )
}

function EmptyStream({
  selectedAgentId,
  scanFilter,
  noScansYet,
  latestScanId,
  currentScan,
}: {
  selectedAgentId: string | null
  scanFilter: string | null
  noScansYet: boolean
  latestScanId: string | null
  /** 指定了扫描时，那一条的最新状态（用来区分「启动中」与「已结束」） */
  currentScan: Scan | null
}) {
  /*
    ★ 启动中：`running` 但一条事件都还没有。

    ⚠️ 只在 `running` 时进这个分支。`queued`（还在排队）与终态
    （completed/failed/aborted）各有各的说法，混进来会说错话 ——
    比如对一条已完成的扫描说"正在启动"，那就是在骗人。
  */
  const startingUp = scanFilter && currentScan?.status === 'running'

  // 没有任何筛选、而且一次扫描都没跑过 —— 这是"还没开始"，不是"空"
  if (!selectedAgentId && !scanFilter && noScansYet) {
    return (
      <NoScansYet
        bordered={false}
        description="扫描一开始，智能体的每一次工具调用、推理和发现都会实时出现在这里。"
      />
    )
  }

  if (startingUp) {
    return (
      <StartupNotice scanId={scanFilter} startedAt={currentScan?.startedAt ?? null} />
    )
  }

  const headline = selectedAgentId
    ? `${selectedAgentId} 还没有产生事件。`
    : scanFilter
      ? '这次扫描还没有产生事件。'
      : '还没有事件。'

  return (
    <div className="flex max-w-[52ch] flex-col items-center gap-tight text-center">
      <Radio size={20} strokeWidth={1.5} className="text-ink-ghost" aria-hidden="true" />
      <p className="text-sm text-ink-muted">{headline}</p>
      <p className="text-xs text-ink-faint">
        {scanFilter
          ? '扫描排队结束、智能体开始工作后，事件会实时出现在这里。'
          : '扫描一开始，智能体的每一次工具调用、推理和发现都会实时出现在这里。'}
      </p>
      {scanFilter && (
        <Link
          to="/activity"
          className="mt-tight text-xs text-accent-strong underline hover:text-accent"
        >
          查看全部扫描的活动
        </Link>
      )}

      {/*
        ★ 全局流 + 有历史扫描 = "事件已经过去了"，不是"没有事件"。
          契约 §4.1：不传游标时从那**一刻**开始推，没有 replay。
          这里如实说明，并给一条真能看到的路径 —— 否则用户会以为
          刚刚那次扫描的事件丢了（而它们都好端端躺在库里）。
      */}
      {!scanFilter && !selectedAgentId && latestScanId && (
        <>
          {/*
            ⚠️ 这句话用 `text-ink-faint` 而不是 `ink-ghost` ——
              design-system §2 写明 ghost 是 2.17:1、**禁止承载任何信息**。
              这里说的是"为什么这一栏是空的"，那是用户此刻唯一需要知道的事。
          */}
          <p className="mt-tight max-w-[46ch] text-2xs text-ink-faint">
            这一栏只推送此刻起发生的事件，不重放历史。
          </p>
          <Link
            to={`/activity?scan=${encodeURIComponent(latestScanId)}`}
            className="text-xs text-accent-strong underline hover:text-accent"
          >
            查看最近一次扫描的活动记录
          </Link>
        </>
      )}
    </div>
  )
}

export default ActivityPage
