import { cn } from '@/lib/cn'
import {
  loadKernelChallengeUsage,
  loadKernelUsage,
  type KernelUsageByChallenge,
  type KernelUsageByModel,
  type KernelUsageEntry,
} from '@/data/kernel'
import { formatCompact } from '@/lib/format'
import { formatRelativeTime } from '@/lib/datetime'
import { ChevronRight, Coins } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

/**
 * 总览页的 **LLM token 消耗**卡片（v0.37）。
 *
 * ## 为什么它挂在总览页，而不是只在 CTF2 页面
 *
 * token 是**成本**，而成本该与"扫了多少、发现了多少"同屏 ——
 * 与 `平均扫描耗时` 同一条理由：那张卡回答的是"这套东西烧掉了多少资源"。
 * 只在 CTF2 页面显示的话，用户得先想到去那一页才知道自己花了多少。
 *
 * ## 三个层次：总量 → 两个维度 → 逐轮（v0.37 补后两层）
 *
 * 用户的原话是「细分到每个**任务**还有总消耗」。v0.36 只做到第一层，
 * 所以看起来"没做"。现在三层齐了：
 *
 * | 层 | 数据 | 交互 |
 * | --- | --- | --- |
 * | 总消耗 | `/overview` 的四个字段 | — |
 * | 按模型 / 按题 | `GET /kernel/usage` 的 `byModel` / `byChallenge` | 并列两组条 |
 * | 逐轮明细 | `GET /kernel/challenges/{id}/usage` | 点某道题就地展开 |
 *
 * ★ 为什么"按模型"这一维值得单列：v0.35 的换模 fallback 会让**同一次
 *   解题用多个模型**，而"换模前后各花多少、值不值"是这张卡能给出的
 *   最有信息量的读数之一。后端一直在返回 `byModel`，v0.36 却没用它。
 *
 * ★ 为什么"逐轮"要能展开而不是只跳转：用户问的是"每个任务的消耗"，
 *   而一次 78 轮的解题里，**哪几轮最贵**才是他真正想看的。跳去 CTF2
 *   页面会丢掉"我正在看这一页"的上下文，所以在原地展开。
 *
 * ⚠️ 而"跳转"在这里**根本不可行**，不只是体验取舍：逐轮明细的 UI
 *   **只有本组件有**（CTF2 页面没有逐轮视图，全项目
 *   `loadKernelChallengeUsage` 只有这里在用）。所以任何"去 CTF2 看逐轮
 *   明细"的文案都是假承诺 —— 见下面那个链接上的说明。
 *
 * ## ★★ 三种"空"必须说成三句不同的话（这个组件最要紧的地方）
 *
 * | 数据状态 | 显示 | 为什么不能混 |
 * | --- | --- | --- |
 * | `since === null` | "还没有消耗记录" | 一条 usage 都没记过（还没解过题）|
 * | `recordedCalls === 0` 而 `calls > 0` | "接入点没有返回用量" | 跑了，但**接入点不给数字** —— 不是没跑 |
 * | `recordedCalls < calls` | 总量 + "其中 N 次未返回，是**下限**" | 有几次没算进去，不能说成完整值 |
 *
 * 混成一句"消耗 0"的后果很具体：用户会以为某项**没跑过**，或者以为
 * 那次特别省。这与 `hasKey`/`apiKey` 分两列、`candidateFlag`/`confirmedFlag`
 * 分两列是同一条纪律 —— **两种语义不能共用一个"空"**。
 *
 * 逐轮明细里同一条纪律继续成立：某轮的 `usageReported === false` 时
 * 显示「未记录」而**不是** `0`（见 `TurnRows`）。
 *
 * ## ⚠️ 不做 token → 金额的换算
 *
 * 单价随模型、供应商、时段变化，写死在代码里会误导（契约 §3.19.6 明写了
 * 这条取舍）。所以这里只给 token 数，换算由用户自己乘。
 *
 * ## 细分是**另一次请求**，失败不影响这张卡
 *
 * 总消耗来自 `/overview`（与其它指标同一个事务，口径一致）；细分来自
 * `GET /kernel/usage`，**单独发**。理由有两条：
 *
 *   1. `/overview` 是首屏端点，往里塞"前 50 道题的明细"会让它变重；
 *   2. 细分失败时用户仍然该看到总消耗 —— 而把两者绑在一个请求上，
 *      一次明细查询超时就会让整张卡消失。
 *
 * 所以它在 `calls > 0` 时才发（没有数据就不发这个请求），失败只降级成
 * 一句"细分暂时取不到"，**不**把卡片打成错误态。逐轮明细同理，而且
 * 它是**按需**发的（用户点了才发）。
 *
 * ## 设计统一（用户明确提的一条）
 *
 * 条形式照 `SeverityChart`：**按最大值归一**（按总和归一时最长那根也很短，
 * 整张图全是残条）、每条直接标数值、轨道用 `bg-line`（对 surface 有
 * 1.22:1，`surface-sunken` 只有 1.03:1 等于没有轨道）、`tabular` +
 * `data-numeric`。卡片壳沿用 `rounded-md border border-line bg-surface
 * p-panel`，字号层级 `text-sm` 标题 / `text-2xs` 注释。
 */
export interface TokenUsageCardProps {
  /** LLM 调用总次数。**永远准确**（后端从行数数得出来） */
  calls: number
  /** 其中接入点真的返回了用量的次数 */
  recordedCalls: number
  /** 总 token（prompt + completion）。两者不等时它是**下限** */
  totalTokens: number
  /** 统计从哪一刻开始。**null = 一条记录都没有** */
  since: string | null
  className?: string
}

/**
 * 细分（`GET /kernel/usage`）的取数状态。
 *
 * ⚠️ 三个值而不是一个 `data | null`：要区分"还没取回来"与"取失败"，
 * 否则失败会显示成"没有数据"—— 那正是这个卡片要消灭的那类混淆。
 *
 * ★ `idle` **就是**初始的加载态，effect 里**不**同步 `setState`
 *   （eslint 的 `react-hooks/set-state-in-effect` 会拦，而那条规则是对的：
 *   在 effect 体里同步改状态会多渲染一轮）。所以渲染时 `idle` 与
 *   "正在加载"是同一件事。
 */
interface BreakdownState {
  status: 'idle' | 'ready' | 'error'
  byModel: KernelUsageByModel[]
  byChallenge: KernelUsageByChallenge[]
}

/** 逐轮明细的状态。`challengeId` 记录它是给哪道题取的 */
interface TurnState {
  challengeId: string | null
  status: 'idle' | 'loading' | 'ready' | 'error'
  rows: KernelUsageEntry[]
}

export function TokenUsageCard({
  calls,
  recordedCalls,
  totalTokens,
  since,
  className,
}: TokenUsageCardProps) {
  const [detail, setDetail] = useState<BreakdownState>({
    status: 'idle',
    byModel: [],
    byChallenge: [],
  })
  /** 当前展开的那道题（null = 都收起）。同一时刻只展开一道 —— 见 `TurnRows` */
  const [openId, setOpenId] = useState<string | null>(null)
  const [turns, setTurns] = useState<TurnState>({
    challengeId: null,
    status: 'idle',
    rows: [],
  })

  const hasAny = calls > 0

  useEffect(() => {
    // 没有调用记录时**不发这个请求**：那是一次注定返回空数组的往返。
    if (!hasAny) return
    let alive = true
    void loadKernelUsage()
      .then((data) => {
        if (alive) {
          setDetail({ status: 'ready', byModel: data.byModel, byChallenge: data.byChallenge })
        }
      })
      .catch(() => {
        // ★ 不把整张卡打成错误态：总消耗已经拿到了，细分是附加信息。
        //   但也不静默 —— 用户看不到细分时要知道是"取不到"而不是"没有"。
        if (alive) setDetail({ status: 'error', byModel: [], byChallenge: [] })
      })
    return () => {
      alive = false
    }
    /*
      ⚠️ 依赖里带上三个数字，不只是 `hasAny`。

      总览数据会**静默刷新**（`loadOverview` 的 stale-while-revalidate，
      见 `overview-store`），那时这张卡会拿到新的 props 而不重新挂载。
      只依赖 `hasAny` 的话，细分列表会停在**上一次**的读数 —— 而它与
      上面那个总量同屏，两个数字对不上时用户没法判断哪个是对的
      （正是 `overview-store` 文件头在防的那类问题）。

      React 只在值真的变了（`Object.is`）才重跑，所以没有更新时不会白发请求。
    */
  }, [hasAny, calls, recordedCalls, totalTokens])

  /**
   * 展开/收起某道题的逐轮明细。
   *
   * ★ 逐轮明细**按需取**：78 轮一次请求不算大，但总览页不该在首屏替
   *   用户把每道题的明细都拉下来 —— 他只可能点开其中一道。
   *
   * ⚠️ 收起来时**不**清 `turns`：再点开同一道题会立刻显示上次的结果
   *   （`challengeId` 相同就不重取）。这不算缓存失效问题 —— 轮次明细
   *   是**追加**的，而这张卡的总量一变（上面的 effect）就会重取细分，
   *   那时这里也会跟着重新取。
   */
  const toggle = (challengeId: string) => {
    if (openId === challengeId) {
      setOpenId(null)
      return
    }
    setOpenId(challengeId)
    if (turns.challengeId === challengeId && turns.status === 'ready') return
    setTurns({ challengeId, status: 'loading', rows: [] })
    void loadKernelChallengeUsage(challengeId)
      .then((rows) => {
        // 用函数式更新里的 `prev.challengeId` 判"还是不是当前这道" ——
        // 用户可能在请求回来之前就点了别的题。
        setTurns((prev) => (prev.challengeId === challengeId ? { challengeId, status: 'ready', rows } : prev))
      })
      .catch(() => {
        setTurns((prev) => (prev.challengeId === challengeId ? { challengeId, status: 'error', rows: [] } : prev))
      })
  }

  return (
    <section
      aria-labelledby="token-usage-title"
      className={cn('rounded-md border border-line bg-surface p-panel', className)}
    >
      <div className="flex items-baseline justify-between gap-tight">
        <h2
          id="token-usage-title"
          className="flex items-center gap-1.5 text-sm font-medium text-ink"
        >
          <Coins size={14} strokeWidth={1.75} className="text-ink-ghost" aria-hidden="true" />
          LLM 消耗（CTF2 解题）
        </h2>
        {since && (
          /*
            ★ 统计起点必须说出来。v0.36 **之前**的解题没有 usage 记录 ——
              不写这一句，用户会把"从这时起的总量"读成"历史总量"。
              与契约里"宁可少说，不说错的"同一条（§3.18）。
          */
          <span className="text-2xs text-ink-faint">自 {formatRelativeTime(since)}起统计</span>
        )}
      </div>

      {!hasAny || !since ? (
        /*
          ── 状态一：一条记录都没有 ──

          ★ 这里**不显示"0 token"**。0 与"没记录"在界面上长得一样，
            而后者才是事实 —— 显示 0 会让用户以为解题没消耗。
        */
        <p className="mt-panel text-xs text-ink-faint">
          还没有消耗记录。CTF2 解题跑完一轮之后，这里会显示 token 用量。
        </p>
      ) : (
        <>
          <div className="mt-panel flex flex-wrap items-baseline gap-x-panel gap-y-2">
            <div className="flex items-baseline gap-1.5">
              <span className="tabular text-3xl font-medium text-ink" data-numeric>
                {formatCompact(totalTokens)}
              </span>
              <span className="text-sm text-ink-muted">token</span>
            </div>
            <div className="flex flex-col gap-0.5 text-2xs text-ink-faint">
              <span className="tabular" data-numeric>
                共 {calls} 次调用
                {recordedCalls < calls ? `，其中 ${calls - recordedCalls} 次未返回用量` : ''}
              </span>
              <span className="tabular" data-numeric>
                精确值 {totalTokens.toLocaleString('zh-CN')} token
              </span>
            </div>
          </div>

          {/*
            ── 状态二/三：接入点没给用量 ──

            ★ 这两种情况**必须出声**：
              · 一次都没给 → 上面的 0 是"没记录"，不是"没消耗"，所以
                干脆把 token 数说成不可统计，而不是摆一个 0；
              · 部分没给 → 总量是**下限**，措辞必须是"至少"。
          */}
          {recordedCalls === 0 ? (
            <p className="mt-tight flex items-start gap-1.5 text-2xs text-signal-warn">
              <span aria-hidden="true">·</span>
              <span>
                这几次调用
                <strong className="font-medium">接入点都没有返回 token 用量</strong>
                {' —— '}所以消耗无法统计。这不是「消耗为 0」，而是「没记录到」。
              </span>
            </p>
          ) : recordedCalls < calls ? (
            <p className="mt-tight text-2xs text-ink-faint">
              这是一个
              <strong className="font-medium text-ink-muted">下限</strong>
              ：有 {calls - recordedCalls} 次调用接入点没有返回用量，实际消耗只会更多。
            </p>
          ) : null}

          {/*
            ── 细分：按模型 / 按题（另一次请求，失败只降级） ──

            ★ 两个维度**并列**，不合成一组。它们回答的是两个不同问题：
              "钱花在哪个模型上"（换模值不值）与"哪道题最烧钱"。
              合成一组就得选一个分组键，另一个信息永久丢失。
          */}
          <div className="mt-panel border-t border-line pt-panel">
            {detail.status === 'idle' ? (
              <p className="text-2xs text-ink-faint">正在读取细分数据…</p>
            ) : detail.status === 'error' ? (
              <p className="text-2xs text-ink-faint">
                细分数据暂时取不到（总消耗不受影响）。稍后刷新页面可重试。
              </p>
            ) : (
              <div className="grid grid-cols-1 gap-panel lg:grid-cols-2">
                <UsageBreakdown
                  title="按模型"
                  hint="换模（fallback）之后同一次解题会用多个模型 —— 这里是换模前后各花了多少。"
                  rows={detail.byModel.map((row) => ({
                    key: `${row.model}·${row.providerLabel}`,
                    label: row.providerLabel ? `${row.model}（${row.providerLabel}）` : row.model || '未标注',
                    calls: row.calls,
                    totalTokens: row.totalTokens,
                  }))}
                  emptyText="还没有按模型的记录。"
                />

                <UsageBreakdown
                  title="按题目"
                  hint="点某道题可以就地展开它的逐轮消耗。"
                  rows={detail.byChallenge.map((row) => ({
                    key: row.challengeId,
                    label: row.title || '未命名题目',
                    calls: row.calls,
                    totalTokens: row.totalTokens,
                    challengeId: row.challengeId,
                  }))}
                  emptyText="还没有按题的记录。跑完一轮解题后这里会列出最烧钱的几道题。"
                  /* 只有按题那一组能展开 —— 逐轮明细是按 challengeId 取的 */
                  onSelect={toggle}
                  selectedKey={openId}
                />
              </div>
            )}
          </div>

          {/*
            逐轮明细就地展开在两组条**下面**（整卡宽度）。

            ★ 不塞进那一列的条里：逐轮是"某道题的细节"，宽度上需要
              更多空间（每行有轮次 / 模型 / 输入 / 输出 / 合计），
              挤在半栏里会把模型名压成省略号。
          */}
          {openId && <TurnRows challengeId={openId} state={turns} onClose={() => setOpenId(null)} />}
        </>
      )}
    </section>
  )
}

/**
 * 一组条形细分（按模型 / 按题共用）。
 *
 * ## ★ 为什么抽成一个组件而不是写两遍
 *
 * 用户明确提了"页面设计统一"。两组条如果各写一份，迟早漂移 ——
 * 症状是"同一个页面里两组条形图看着不是一套东西"（间距、归一方式、
 * 数值对齐各差一点）。这跟 `WriteupDialogShell` 被抽出来的理由一样。
 *
 * ## 条形式照 `SeverityChart`（同一页的参照物）
 *
 *   · 条长按**最大值**归一 —— 按总和归一时最长那根也很短，整组会
 *     看起来全是残条，读不出差异；
 *   · 非零值给最小宽度 2%，避免 "1" 在 "13" 面前细成一根发丝；
 *   · 轨道用 `bg-line`（对 surface 1.22:1），不用 `surface-sunken`（1.03:1
 *     等于没有轨道）；
 *   · 每条**直接标数值**，不靠 tooltip —— 看板页不该要求悬停才得到数字。
 *
 * ⚠️ 只显示前 5 条。后端 `byChallenge` 最多给 50 条，而总览页要的是
 *    "最烧钱的几道"，不是完整清单。
 */
interface BreakdownRow {
  key: string
  label: string
  calls: number
  totalTokens: number
  /** 有它才可展开（逐轮明细按 challengeId 取） */
  challengeId?: string
}

function UsageBreakdown({
  title,
  hint,
  rows,
  emptyText,
  onSelect,
  selectedKey,
}: {
  title: string
  hint: string
  rows: BreakdownRow[]
  emptyText: string
  onSelect?: (challengeId: string) => void
  selectedKey?: string | null
}) {
  const top = rows.slice(0, 5)
  // 按最大值归一 —— 与 SeverityChart 同一条（按总和归一时最长的一根也很短）
  const max = Math.max(...rows.map((row) => row.totalTokens), 0)
  const sum = rows.reduce((acc, row) => acc + row.totalTokens, 0)

  return (
    <div className="flex min-w-0 flex-col">
      <div className="flex items-baseline justify-between gap-tight">
        <h3 className="text-2xs text-ink-muted">{title}</h3>
        <span className="tabular text-2xs text-ink-faint" data-numeric>
          {rows.length} 项
        </span>
      </div>

      {rows.length === 0 ? (
        <p className="mt-1.5 text-2xs text-ink-faint">{emptyText}</p>
      ) : (
        <>
          <ul className="mt-2 flex flex-col gap-1.5">
            {top.map((row) => {
              const pct = max === 0 ? 0 : (row.totalTokens / max) * 100
              const open = selectedKey === row.key && row.challengeId !== undefined
              const label = (
                <span className="min-w-0 flex-1 truncate text-2xs text-ink-muted" title={row.label}>
                  {row.label}
                </span>
              )
              const bar = (
                <span className="h-1.5 min-w-0 flex-1 overflow-hidden rounded-pill bg-line">
                  <span
                    className="block h-full rounded-pill bg-accent/60"
                    style={{ width: `${Math.max(pct, 2)}%` }}
                    aria-hidden="true"
                  />
                </span>
              )
              const value = (
                <span
                  className="tabular w-14 shrink-0 text-right text-2xs text-ink-muted"
                  data-numeric
                  title={`${row.totalTokens.toLocaleString('zh-CN')} token · ${row.calls} 次调用`}
                >
                  {formatCompact(row.totalTokens)}
                </span>
              )

              /*
                ★ 可展开的行整行是一个按钮（不是"标签旁边一个小箭头"）：
                  点击目标越大越不容易点空，而且 `aria-expanded` 让读屏器
                  能说出"这一项是展开状态"。与 `FindingsGroups` 同一手法。
              */
              return row.challengeId && onSelect ? (
                <li key={row.key}>
                  <button
                    type="button"
                    onClick={() => onSelect(row.challengeId!)}
                    aria-expanded={open}
                    title={`${row.label} —— 点开看逐轮消耗`}
                    className={cn(
                      'flex w-full items-center gap-tight rounded-xs text-left',
                      'transition-ui duration-fast hover:bg-surface-raised',
                    )}
                  >
                    <ChevronRight
                      size={11}
                      strokeWidth={2}
                      aria-hidden="true"
                      className={cn(
                        'shrink-0 text-ink-ghost transition-transform duration-fast',
                        open && 'rotate-90',
                      )}
                    />
                    {label}
                    {bar}
                    {value}
                  </button>
                </li>
              ) : (
                <li key={row.key} className="flex items-center gap-tight">
                  {/* 不可展开的行补一个等宽占位，让两组条的基线对齐 */}
                  <span className="w-[11px] shrink-0" aria-hidden="true" />
                  {label}
                  {bar}
                  {value}
                </li>
              )
            })}
          </ul>

          {/*
            ★ 条形的分母是"这一组里最大的那项"，不是总量。不说明的话，
              用户会把最长的那根读成"占了全部" —— 而它只是相对最大。
            下面这句同时给出合计，让"细分加起来是多少"有答案。
          */}
          <p className="mt-2 flex flex-wrap items-center gap-x-tight text-2xs text-ink-faint">
            <span>{hint}</span>
            <span className="tabular" data-numeric>
              条长按本组最高项归一，不代表占总量比例。
            </span>
            <span className="tabular" data-numeric>
              这 {rows.length} 项合计 {formatCompact(sum)} token
            </span>
          </p>
        </>
      )}
    </div>
  )
}

/**
 * 某道题的**逐轮**消耗（`GET /kernel/challenges/{id}/usage`）。
 *
 * ## ★★ 逐轮的"未记录"必须显示成「未记录」，不是 `0`
 *
 * `usageReported === false` 的轮次，后端给的 token 是 0 —— 而那个 0
 * **不代表这一轮没消耗**，只代表接入点没给数字。这与整卡的三种"空"
 * 是同一条纪律，只是粒度落到每一轮：把"没记录"渲染成 `0` 会让用户
 * 以为那一轮特别省，或者以为它没跑。
 *
 * ## 为什么最多列出前 20 轮（按消耗降序之外还留了原序）
 *
 * 一次解题常见 20–80 轮（真实样本 78 轮）。全部铺开会把卡片撑得很长，
 * 而这里要回答的是"哪几轮最贵"。所以：
 *   · 先给**最贵的 5 轮**（这是"在哪儿烧的钱"的直接答案）；
 *   · 再按**轮次顺序**列出全部（最多 20 条，超出只给计数）。
 * 两段都标着各自的排序口径，避免用户把两段的顺序搞混。
 */
function TurnRows({
  challengeId,
  state,
  onClose,
}: {
  challengeId: string
  state: TurnState
  onClose: () => void
}) {
  const rows = state.challengeId === challengeId ? state.rows : []
  const status = state.challengeId === challengeId ? state.status : 'loading'

  const recorded = rows.filter((row) => row.usageReported)
  const unrecorded = rows.length - recorded.length
  const topByCost = [...recorded].sort((a, b) => b.totalTokens - a.totalTokens).slice(0, 5)

  return (
    <div className="mt-panel rounded-sm border border-line bg-surface-sunken p-panel">
      <div className="flex items-baseline justify-between gap-tight">
        <h3 className="text-2xs text-ink-muted">逐轮消耗</h3>
        <button
          type="button"
          onClick={onClose}
          className="text-2xs text-ink-faint transition-ui duration-fast hover:text-ink"
        >
          收起
        </button>
      </div>

      {status === 'loading' ? (
        <p className="mt-1.5 text-2xs text-ink-faint">正在读取逐轮明细…</p>
      ) : status === 'error' ? (
        <p className="mt-1.5 text-2xs text-ink-faint">
          逐轮明细暂时取不到。上面按题的合计不受影响。
        </p>
      ) : rows.length === 0 ? (
        <p className="mt-1.5 text-2xs text-ink-faint">这道题没有逐轮记录。</p>
      ) : (
        <>
          {/*
            ★ 有轮次没返回用量时必须出声 —— 否则下面那些 0 会被读成
              "这几轮没花钱"。与整卡"下限"那句同一条纪律。
          */}
          {unrecorded > 0 && (
            <p className="mt-1.5 text-2xs text-signal-warn">
              共 {rows.length} 轮，其中{' '}
              <strong className="font-medium">{unrecorded} 轮接入点未返回用量</strong>
              （显示为「未记录」而不是 0）。
            </p>
          )}

          <h4 className="mt-2 text-2xs text-ink-faint">最贵的 5 轮</h4>
          <ul className="mt-1 flex flex-col gap-1">
            {topByCost.map((row) => (
              <li key={`top-${row.turn}`} className="flex items-center gap-tight text-2xs">
                <span className="tabular w-12 shrink-0 text-ink-faint" data-numeric>
                  第 {row.turn} 轮
                </span>
                <span className="min-w-0 flex-1 truncate text-ink-muted" title={row.model}>
                  {row.model}
                </span>
                <span className="tabular shrink-0 text-ink-faint" data-numeric>
                  {formatCompact(row.promptTokens)} 入 / {formatCompact(row.completionTokens)} 出
                </span>
                <span className="tabular w-14 shrink-0 text-right text-ink" data-numeric>
                  {formatCompact(row.totalTokens)}
                </span>
              </li>
            ))}
          </ul>

          {/*
            完整列表放在可滚区域里（`max-h-64`）：78 轮全铺开会把这张卡
            拉得比整页还长，而页面滚动条被吃掉之后用户会以为自己滚不动了。
            内层滚动在这里可接受 —— 它是一块**明确的明细面板**，不是主内容。
          */}
          <h4 className="mt-2 text-2xs text-ink-faint">全部轮次（按顺序）</h4>
          <ul className="mt-1 flex max-h-64 flex-col gap-1 overflow-y-auto pr-1">
            {rows.slice(0, 100).map((row) => (
              <li key={row.turn} className="flex items-center gap-tight text-2xs">
                <span className="tabular w-12 shrink-0 text-ink-faint" data-numeric>
                  第 {row.turn} 轮
                </span>
                <span className="min-w-0 flex-1 truncate text-ink-muted" title={row.model}>
                  {row.model}
                </span>
                {/*
                  ★★ 未记录的轮次，**三个数字全部**说成「未记录」。

                  `usage_reported === false` 时后端把 prompt/completion/total
                  一律存成 0（见 `service.record_solve_usages` 传的
                  `usage.prompt_tokens`，而那是 `TokenUsage()` 的默认值）。
                  所以只把合计那一列写成「未记录」、入/出两列照旧渲染
                  `0 入 / 0 出` 是**自相矛盾**的：同一行既说没记录、又给出
                  两个具体读数，用户只会信那两个数字（"这轮没花钱"）。

                  这是整卡"两种语义不能共用一个空"那条纪律落到每一轮上
                  的完整形态 —— 只改一列等于没改。
                */}
                <span className="tabular shrink-0 text-ink-faint" data-numeric>
                  {row.usageReported
                    ? `${formatCompact(row.promptTokens)} 入 / ${formatCompact(row.completionTokens)} 出`
                    : '未记录'}
                </span>
                <span
                  className="tabular w-14 shrink-0 text-right text-ink-muted"
                  data-numeric
                  title={row.usageReported ? undefined : '这一轮接入点没有返回用量'}
                >
                  {row.usageReported ? formatCompact(row.totalTokens) : '—'}
                </span>
              </li>
            ))}
          </ul>

          {rows.length > 100 && (
            <p className="mt-1.5 text-2xs text-ink-faint">
              共 {rows.length} 轮，这里只列前 100 轮。
            </p>
          )}

          <p className="mt-2 flex flex-wrap items-center gap-x-tight text-2xs text-ink-faint">
            <span>
              合计{' '}
              <span className="tabular" data-numeric>
                {formatCompact(recorded.reduce((acc, row) => acc + row.totalTokens, 0))} token
              </span>
              （{recorded.length} 轮有记录）
            </span>
            {/*
              ★★ 这个链接指向**题解**，不指向逐轮明细（2026-10-15 改文案）。

              原来写的是「去 CTF2 看逐轮明细」—— 那是**承诺了做不到的事**：
              逐轮明细的 UI **只存在于本组件的就地展开里**
              （全项目 `loadKernelChallengeUsage` 只有这里在用，CTF2 页面
              根本没有逐轮视图）。点过去看不到任何轮次明细，
              与契约 §3.18「宁可少说，不说错的」直接冲突。

              为什么留这个链接、且指向题解是**准确**的：
                · 逐轮已经在上面就地展开，用户不需要为它跳页；
                · 而**题解**（关键步骤 / 过程纪要 / 结论）确实只在那张卡片上
                  —— 扫描页那条路对 CTF2 的壳会回 409 并指回这里
                  （契约 §3.19.5）。所以这是从总览页到题解的唯一通路。
                · 那张卡片上**一定有**「题解」入口：面板里每道题都来自
                  `byChallenge`（有 usage 行 ⇒ 跑过一轮 ⇒ 受理时建过壳 scan），
                  而前端 `canHaveWriteup` 的判据正是 `scanId` 非空。
                  实测核对过这条不变量：有 usage 但 `scanId` 为空的题 = **0 道**。

              ⚠️ 措辞刻意说「看这道题的题解」而不是「看这道题的题解**入口**」：
                入口是必然存在的，而题解本身在极端情况下可能没生成
                （`save_writeup` 吞异常）。那种情况点开是一句诚实的
                「这次解题没有生成题解，重新解一次会生成」—— 不是死路。
            */}
            <Link to="/ctf2" className="text-accent-strong underline hover:text-accent">
              去 CTF2 看这道题的题解
            </Link>
          </p>
        </>
      )}
    </div>
  )
}

export default TokenUsageCard
