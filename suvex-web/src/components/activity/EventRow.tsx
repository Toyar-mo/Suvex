import { cn } from '@/lib/cn'
import {
  EVENT_TYPE_LABEL,
  EVENT_TYPE_TONE,
  type ActivityEvent,
  type EventDetail,
} from '@/data/activity'
import { SEVERITY_META } from '@/data/findings'
import { formatRelativeTime } from '@/lib/datetime'
import {
  AlertTriangle,
  CheckCircle2,
  ChevronRight,
  FlaskConical,
  Lightbulb,
  Sparkles,
  Terminal,
  XCircle,
  type LucideIcon,
} from 'lucide-react'
import { memo, useId, useState } from 'react'

/**
 * 一条事件。这是整页的核心单元，也是"结构化日志"与"原始日志"的分界线。
 *
 * 三层结构（自上而下）：
 *   1. **行头** —— 类型标签 + 一行摘要 + agent/时间。折叠态只显示这一层，
 *      扫视时用户读的就是它。
 *   2. **关键字段** —— 工具名、退出码、耗时、严重度这些**能一眼定性的**
 *      结构化字段，用等宽紧凑排在一行。不展开也能看到。
 *   3. **详情** —— 完整命令、原始输出、PoC 源码、证据。按需展开。
 *
 * 为什么这样分层，而不是把所有东西都平铺出来（那是原始日志的形态）：
 * 用户 90% 的时间在读 10% 的事件。把"判断哪条值得看"的信息（类型、摘要、
 * 退出码、严重度）全部放在折叠态，展开只用于"我要看细节"。
 * 反过来做的话，一屏只能放 3 条，扫视效率归零。
 *
 * 动效：**只有新进入的那条做一次轻微淡入上移**，且由父级通过 CSS 动画类控制。
 * 绝不给每条事件都挂复杂动画 —— 高频刷新下那是灾难。
 */

const TYPE_ICON: Record<ActivityEvent['type'], LucideIcon> = {
  tool_call: Terminal,
  reasoning: Sparkles,
  discovery: Lightbulb,
  poc_generated: FlaskConical,
  verified: CheckCircle2,
  blocked: AlertTriangle,
  failed: XCircle,
  completed: CheckCircle2,
}

/** 类型标签的配色。neutral 刻意用灰 —— 日常动作不该抢注意力 */
const TONE_CLASS = {
  neutral: 'text-ink-faint border-line',
  accent: 'text-accent-strong border-accent/40',
  ok: 'text-signal-ok border-signal-ok/40',
  risk: 'text-signal-risk border-signal-risk/40',
} as const

export interface EventRowProps {
  event: ActivityEvent
  /** 是否显示 agent 列。按 agent 过滤时这一列是冗余的，隐藏可省宽度 */
  showAgent: boolean
  className?: string
}

/**
 * ⚠️ 这个函数**不直接导出**，导出的是下面 `memo` 包过的那一个 ——
 *    原因见文件末尾那段说明（`useActivityStream` 的上限是 5000 条，
 *    每次 flush 都会重建整个 `events` 数组）。
 */
function EventRowImpl({ event, showAgent, className }: EventRowProps) {
  const [expanded, setExpanded] = useState(false)
  const detailId = useId()

  const Icon = TYPE_ICON[event.type]
  const tone = EVENT_TYPE_TONE[event.type]
  const hasDetail = event.detail !== null

  return (
    <li
      className={cn(
        'group border-b border-line/60 last:border-b-0',
        // 一眼能看出"出事了"的行给一点极淡的底色，不是整条染色
        tone === 'risk' && 'bg-signal-risk/[0.04]',
        className,
      )}
    >
      <div className="flex gap-tight px-panel py-2">
        {/* 类型图标。mt-0.5 让它与首行文字的视觉中线对齐 */}
        <Icon
          size={14}
          strokeWidth={1.75}
          className={cn(
            'mt-0.5 shrink-0',
            tone === 'neutral' && 'text-ink-faint',
            tone === 'accent' && 'text-accent-strong',
            tone === 'ok' && 'text-signal-ok',
            tone === 'risk' && 'text-signal-risk',
          )}
          aria-hidden="true"
        />

        <div className="min-w-0 flex-1">
          {/* ── 第 1 层：行头 ── */}
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
            <span
              className={cn(
                'shrink-0 rounded-xs border px-1.5 text-2xs leading-[1.35]',
                TONE_CLASS[tone],
              )}
            >
              {EVENT_TYPE_LABEL[event.type]}
            </span>

            <p className="min-w-0 flex-1 text-sm leading-snug text-ink">{event.summary}</p>
          </div>

          {/* ── 第 2 层：关键字段（折叠态也可见）── */}
          <div className="mono-ui mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-ink-faint">
            {showAgent && (
              <>
                <span className="text-ink-muted">{event.agentId}</span>
                <Sep />
              </>
            )}
            <time dateTime={event.at} className="tabular">
              {formatRelativeTime(event.at)}
            </time>

            <InlineFacts detail={event.detail} />
          </div>

          {/* ── 第 3 层：详情 ── */}
          {hasDetail && (
            <>
              {/*
                触摸目标尺寸：实测宽 61px × 高 18px，**远低于 44px 的最小建议**
                （WCAG 2.5.8 / Apple HIG）。而这是本页最高频的交互 —— 每条事件
                都有一个，在手机上极容易误触。

                修法不是把字放大（那会破坏信息密度），而是用等距内边距把
                可点区域撑到 44px 高，同时用负外边距抵消它对行高的影响，
                视觉位置几乎不变。
              */}
              <button
                type="button"
                onClick={() => setExpanded((v) => !v)}
                aria-expanded={expanded}
                aria-controls={detailId}
                className={cn(
                  'inline-flex items-center gap-1 text-2xs text-ink-faint',
                  'transition-ui duration-fast hover:text-ink-muted',
                  // 撑到 44px 触摸高度；-my-2.5 把它在视觉上拉回原位
                  '-my-2.5 min-h-11 py-2.5',
                  // 横向也给足够宽度，避免只有文字那么窄
                  '-ml-1 min-w-11 px-1',
                )}
              >
                <ChevronRight
                  size={12}
                  strokeWidth={2}
                  className={cn('transition-transform duration-fast', expanded && 'rotate-90')}
                  aria-hidden="true"
                />
                {expanded ? '收起详情' : '展开详情'}
              </button>

              {expanded && (
                <div id={detailId} className="mt-2">
                  <EventDetailView detail={event.detail!} />
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </li>
  )
}

function Sep() {
  return (
    <span aria-hidden="true" className="text-ink-ghost">
      ·
    </span>
  )
}

/**
 * 折叠态就能看到的关键字段。
 *
 * 这些字段的共同点：**它们能让人快速决定"要不要展开"**。
 * 退出码非 0 说明工具失败了；耗时长说明这次调用重；
 * 严重度决定这条发现要不要立刻处理。
 * 而那些"只有展开了才有用"的内容（完整输出、源码）不进这里。
 */
function InlineFacts({ detail }: { detail: EventDetail | null }) {
  if (!detail) return null

  switch (detail.kind) {
    case 'tool_call':
      return (
        <>
          <Sep />
          <span className="text-ink-muted">{detail.tool}</span>
          <Sep />
          <span
            className={cn(
              'tabular',
              /*
                非 0 退出码是"这次调用有问题"的信号，值得给颜色。

                ⚠️ `null`（未知）**必须中性**。契约 §4.2 写明这个字段对
                   `tool_call` 恒为 `null`（外部工具不给单次调用的退出码），
                   而按"非 0 即红"写会把**每一条**工具调用都标成红色错误 ——
                   它们大多数其实是成功的。那比不显示颜色更糟。
              */
              detail.exitCode !== null && detail.exitCode !== 0
                ? 'text-signal-risk'
                : 'text-ink-faint',
            )}
            data-numeric
          >
            exit {detail.exitCode ?? '—'}
          </span>
          <Sep />
          <span className="tabular" data-numeric>
            {formatMs(detail.durationMs)}
          </span>
        </>
      )

    case 'reasoning':
      return (
        <>
          <Sep />
          <span className="truncate text-ink-faint">{detail.intent}</span>
        </>
      )

    case 'discovery': {
      const meta = SEVERITY_META[detail.severity ?? 'info']
      return (
        <>
          {detail.subject && (
            <>
              <Sep />
              <span className="truncate" title={detail.subject}>
                {detail.subject}
              </span>
            </>
          )}
          <Sep />
          <span className={meta.text}>{meta.label}</span>
          <Sep />
          <span>置信度 {CONFIDENCE_LABEL[detail.confidence]}</span>
        </>
      )
    }

    case 'poc_generated':
      return (
        <>
          <Sep />
          <span className="text-ink-muted">{detail.language}</span>
          <Sep />
          <span className="truncate" title={detail.target}>
            {detail.target}
          </span>
        </>
      )

    case 'verified':
      return (
        <>
          <Sep />
          <span className="text-signal-ok">已复现</span>
        </>
      )

    case 'blocked':
      return (
        <>
          <Sep />
          <span className="text-signal-risk">{detail.policy}</span>
        </>
      )

    case 'failed':
      return null

    case 'completed':
      return null

    default:
      return null
  }
}

const CONFIDENCE_LABEL = { low: '低', medium: '中', high: '高' } as const

/**
 * 展开后的详情。
 *
 * 排版规则：
 *   - 命令 / 输出 / 代码一律 `pre` + 等宽 + 不换行折行（横向可滚），
 *     因为日志的换行位置本身携带信息，软换行会把它毁掉；
 *   - 每类事件用**语义化的字段名**而不是把 JSON 倒出来 ——
 *     "退出码 0" 比 `"exitCode": 0` 好读得多；
 *   - 说明性文字（blocked 的 reason、failed 的 error）用普通字体，
 *     它们是给人读的句子，不是机器输出。
 */
function EventDetailView({ detail }: { detail: EventDetail }) {
  switch (detail.kind) {
    case 'tool_call':
      return (
        <div className="flex flex-col gap-2">
          <Field label="命令">
            <CodeBlock>{detail.command}</CodeBlock>
          </Field>
          {detail.output && (
            <Field label="输出">
              <CodeBlock muted>{detail.output}</CodeBlock>
            </Field>
          )}
        </div>
      )

    case 'reasoning':
      return (
        <Field label="推理">
          {/* 推理是给人读的散文，用普通字体与正常行高，不用等宽 */}
          <p className="max-w-[72ch] whitespace-pre-wrap text-sm leading-relaxed text-ink-muted">
            {detail.text}
          </p>
        </Field>
      )

    case 'discovery':
      return (
        <div className="flex flex-col gap-2">
          <Field label="证据">
            <CodeBlock>{detail.evidence}</CodeBlock>
          </Field>
          <p className="text-xs text-ink-faint">
            {SEVERITY_META[detail.severity ?? 'info'].definition}
          </p>
        </div>
      )

    case 'poc_generated':
      return (
        <Field label={`验证脚本 · ${detail.language}`}>
          <CodeBlock>{detail.code}</CodeBlock>
        </Field>
      )

    case 'verified':
      return (
        <div className="flex flex-col gap-2">
          <Field label="复现方式">
            <p className="text-sm text-ink-muted">{detail.method}</p>
          </Field>
          <Field label="复现证据">
            <CodeBlock>{detail.evidence}</CodeBlock>
          </Field>
        </div>
      )

    case 'blocked':
      return (
        <div className="flex flex-col gap-2">
          <Field label="被拦下的动作">
            <CodeBlock>{detail.attempted}</CodeBlock>
          </Field>
          <Field label="触发策略">
            <span className="mono-ui text-ink-muted">{detail.policy}</span>
          </Field>
          <p className="max-w-[72ch] text-sm leading-relaxed text-ink-muted">{detail.reason}</p>
        </div>
      )

    case 'failed':
      return (
        <div className="flex flex-col gap-2">
          <Field label="尝试的动作">
            <p className="text-sm text-ink-muted">{detail.attempted}</p>
          </Field>
          <Field label="错误">
            <CodeBlock>{detail.error}</CodeBlock>
          </Field>
        </div>
      )

    case 'completed':
      return (
        <Field label="结果">
          <p className="max-w-[72ch] whitespace-pre-wrap text-sm leading-relaxed text-ink-muted">
            {detail.result}
          </p>
        </Field>
      )

    default:
      return null
  }
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-2xs uppercase tracking-wide text-ink-faint">{label}</span>
      {children}
    </div>
  )
}

/**
 * 代码/日志块。
 *
 * `overflow-x-auto` + `whitespace-pre`：**不软换行**。
 * 日志的换行位置本身携带信息（一条记录一行），软换行会让长行折成多行，
 * 看起来像多条记录。宁可横向滚动。
 */
function CodeBlock({ children, muted = false }: { children: React.ReactNode; muted?: boolean }) {
  return (
    <pre
      className={cn(
        'overflow-x-auto rounded-sm border border-line bg-surface-sunken px-3 py-2',
        'text-xs leading-relaxed',
        muted ? 'text-ink-muted' : 'text-ink',
      )}
    >
      <code className="whitespace-pre font-mono">{children}</code>
    </pre>
  )
}

/**
 * 毫秒 → "1.2s" / "840ms"。超过 1 分钟给 "1m 20s"。
 *
 * ⚠️ `null` → `'—'`。契约 §4.2 写明 `tool_call` 的 `durationMs` **恒为 null**，
 *    而 `null < 1000` 在 JS 里是 `true` —— 原来的实现会渲染出 **"nullms"**。
 *    一个根本没测到的数字被显示成耗时，比空着更糟：
 *    读者会拿它去判断"这一步是不是慢得异常"。
 */
function formatMs(ms: number | null): string {
  if (ms === null) return '—'
  if (ms < 1000) return `${ms}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`
  const totalSec = Math.round(ms / 1000)
  return `${Math.floor(totalSec / 60)}m ${totalSec % 60}s`
}

/**
 * ★ 用 `memo` 包一层（2026-10 修）。
 *
 * ## 为什么这里值得 memo，而别处不必
 *
 * `useActivityStream` 的 `MAX_EVENTS` 是 **5000**，而它每次 flush
 * （`FLUSH_INTERVAL_MS = 120ms`）都 `setEvents` 出一个**新数组**
 * （`[...prev, ...fresh]`）。`EventStream` 于是重新 `map` 一遍全部 5000 条，
 * 而在此之前每一行的渲染体都会重跑一次 —— 里面有 `useId()`、
 * `formatRelativeTime()`（`Date` 解析 + 相对时间换算）、
 * 以及 `TYPE_ICON` / `EVENT_TYPE_TONE` / `TONE_CLASS` 三次查表。
 * 换句话说：**新增 1 条事件 = 5000 行的这些开销**，而流是持续在推的。
 *
 * `memo` 之后只有"内容真的变了"的那些行会重渲染：新到达的那几条，
 * 以及入场动画翻转时 `className` 变了的行。其余几千行的 props 与上一次
 * 逐值相等，直接跳过 —— 而 `key` 是 `event.id`，所以数组里的位置移动
 * 不会让 React 把它们当成新组件。
 *
 * ## 为什么 memo 在这里**真的会生效**（这一步不能省）
 *
 * `memo` 的默认浅比较只要有一个 prop 引用变了就失效，所以先确认了
 * `EventStream` 传下来的三个 prop 都是原始值：
 *
 * | prop | 传的是什么 | 会变吗 |
 * | --- | --- | --- |
 * | `event` | 数组元素本身，flush 只 append 新对象 | 只在事件真的变了时变 |
 * | `showAgent` | `!filtered` —— 布尔表达式 | 只随"是否按 agent 筛选"变 |
 * | `className` | `isNew ? 'animate-stream-in' : undefined` | 只在新旧状态翻转时变 |
 *
 * ⚠️ 这三个都是**字符串/布尔**，所以浅比较比的是值，不是引用。
 *    这正是它成立的原因 —— 若哪天有人给这里加一个内联箭头函数或
 *    内联对象 prop（比如 `style={{...}}`），`memo` 会**静默失效**，
 *    表现为"加了 memo 但一帧都没省"，很难发现。改这个组件时请留意。
 *
 * ## 为什么不用虚拟化
 *
 * 那是另一个决策（会牵动 `useAutoScroll` 的滚动锚定与"回到最新"的定位），
 * 不在这次改动范围内 —— 这里只解决"已经渲染的行被白重渲染"。
 */
const EventRow = memo(EventRowImpl)

export default EventRow
