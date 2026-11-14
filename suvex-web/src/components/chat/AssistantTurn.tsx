import { memo, useEffect, useRef, useState } from 'react'
import { Loader2 } from 'lucide-react'
import { MessageRow } from '@/components/chat/MessageRow'
import { digestToolArgs } from '@/components/chat/toolCardDigest'
import { InterjectionNote } from '@/components/chat/InterjectionNote'
import { ApprovalCard } from '@/components/chat/ChatControls'
import { ToolCard } from '@/components/chat/ToolCard'
import { WriteupMarkdown } from '@/components/writeup/WriteupMarkdown'
import { useI18n } from '@/i18n'
import type { ChatMessage } from '@/data/chat'

type Translate = (key: string, vars?: Record<string, string | number>, fallback?: string) => string

/**
 * 一个助手回合。
 *
 * ## 形态：对标 ChatGPT / Claude 的「结论是主角，过程是一条可展开的工作记录」
 *
 * ```
 * ┌ 工作过程 · 24 步 · 13 个工具                    ⌄   ← 默认收起，一个回合只有这一行
 * ├ 最终答复（纯文本，可有多段）
 * ├ 待审批卡（永远可见，绝不折进过程里）
 * ├ 插话（跑着的时候用户插的话）
 * └ 回合页脚：N 步 · M 个工具 · 时间
 * ```
 *
 * ## 为什么改回「一条总过程」而不是时间线逐条铺
 *
 * v1.56b 曾经把旁白/工具按发生顺序逐条铺开（对照 CyberStrikeAI 的事件流）。
 * 真实长任务下这会把一回合撑成几十行「过程详情 · 2 步」，结论被顶到几屏
 * 之外 —— 生产会话里 92% 的消息是过程/工具，用户的原话是"太难用了"。
 *
 * 成熟产品的做法相反：**过程默认是一条可展开的摘要，结论始终在视野里**。
 * 所以这里把一回合的全部过程（旁白 + 工具）收进一个 `TurnWork`；用户想看
 * 细节时点开，里面仍是按发生顺序排列的原始记录。
 *
 * 默认**始终收起**（不随 live 自动展开/收起）：跑着的时候展开、结束时收起
 * 会造成整段高度突变、滚动位置跳动，这正是用户抱怨的"跳一下"。
 */
export const AssistantTurn = memo(
  AssistantTurnImpl,
  (prev, next) =>
    sameRefList(prev.items, next.items) &&
    prev.live === next.live &&
    prev.streamingText === next.streamingText &&
    prev.phaseText === next.phaseText &&
    prev.liveTool === next.liveTool &&
    prev.liveToolOutput === next.liveToolOutput &&
    prev.liveToolArgs === next.liveToolArgs &&
    prev.liveToolProgress === next.liveToolProgress &&
    prev.onFork === next.onFork &&
    prev.deciding === next.deciding &&
    prev.meta === next.meta &&
    prev.sessionId === next.sessionId &&
    prev.hideWork === next.hideWork &&
    prev.startedAt === next.startedAt &&
    (prev.progress?.tools ?? null) === (next.progress?.tools ?? null) &&
    sameRefList(prev.interjections ?? [], next.interjections ?? []) &&
    /*
      ★ 有待审批的回合必须比 `onDecide`。
        否则会留着**第一次渲染**的那个闭包，而它捕获的是当时的
        `deciding` / 会话状态 —— 审批就会用一个过期的上下文去提交
        （表现为"点了一下没反应"或重复提交）。这种回合很少，
        每次重渲染的代价可以忽略。
    */
    (!hasPendingApproval(prev.items) || prev.onDecide === next.onDecide),
) as typeof AssistantTurnImpl

/**
 * 两个数组**逐项比引用**。
 *
 * ★ 数组本身每次渲染都是新的（`filter` 出来的），所以比数组引用等于没 memo。
 *   消息对象是 store 里的不可变值：除了正在流式产出的那一回合，
 *   历史回合里的元素引用一直不变 —— 这正是可以跳过它们重渲染的依据。
 */
function hasPendingApproval(items: readonly ChatMessage[]): boolean {
  return items.some((message) => message.approvalStatus === 'pending')
}

function sameRefList<T>(a: readonly T[], b: readonly T[]): boolean {
  if (a === b) return true
  if (a.length !== b.length) return false
  for (let i = 0; i < a.length; i += 1) {
    if (a[i] !== b[i]) return false
  }
  return true
}

function AssistantTurnImpl({
  items,
  live = false,
  streamingText = null,
  phaseText = null,
  liveTool = null,
  liveToolOutput = null,
  liveToolArgs = null,
  liveToolProgress = null,
  deciding,
  onDecide,
  onFork,
  interjections = [],
  meta,
  progress = null,
  sessionId,
  /** 只看回答：隐藏工作过程，只保留结论（空回合给一行说明）。 */
  hideWork = false,
  /** 回合开始时刻：live 时用来显示「已等待 N 秒」。 */
  startedAt = null,
}: {
  items: ChatMessage[]
  live?: boolean
  streamingText?: string | null
  phaseText?: string | null
  /** 正在执行的工具名（phase=tool）—— 画一张运行中的卡片。 */
  liveTool?: string | null
  /** 运行中工具已经吐出的输出（SSE 增量）。 */
  liveToolOutput?: string | null
  /** 运行中工具的浅层参数摘要（phase 帧带下来的）。 */
  liveToolArgs?: Record<string, string> | null
  /** 长工具心跳（秒）；没有可读输出时用它证明还活着。 */
  liveToolProgress?: {
    seconds: number
    message?: string
    progress?: number
    total?: number
  } | null
  deciding?: boolean
  onDecide?: (messageId: string, approve: boolean) => void
  /** 从某条消息分叉（GitBranch 按钮）。 */
  onFork?: (seq: number) => void
  /** 跑的过程中用户插的话（已落库、等下一轮被读到）。 */
  interjections?: ChatMessage[]
  /** 页脚右侧的附加信息（例如人格 / 接入点）。 */
  meta?: string | null
  /** 跑着的时候已经积累了多少（轮次外的计数，见 ChatPage）。 */
  progress?: { steps?: number; tools?: number } | null
  /** 当前会话 id：给正文里的 `/work/persist/...` 产物做下载链接。 */
  sessionId?: string
  hideWork?: boolean
  startedAt?: string | null
}) {
  const { t, locale } = useI18n()
  const answers = items.filter(isAnswer)
  const work = items.filter((message) => !isAnswer(message))
  const body = work.filter((message) => message.approvalStatus !== 'pending')
  const tools = items.filter((message) => message.role === 'tool')
  const failed = tools.filter((message) => message.toolFailed === true).length
  const hasAnswer = answers.length > 0
  const last = items.at(-1)
  const elapsed = useElapsed(live, startedAt ?? items[0]?.createdAt ?? null)

  return (
    <div className="flex flex-col gap-3" data-assistant-turn>
      <div
        className="flex min-w-0 flex-col gap-1.5"
        data-turn-work
        data-turn-work-steps={body.length}
        data-turn-work-tools={tools.length}
      >
        {items.map((message) => {
          if (message.approvalStatus === 'pending') {
            return (
              <ApprovalCard
                key={message.id}
                message={message}
                onDecide={(approve) => onDecide?.(message.id, approve)}
                disabled={deciding}
              />
            )
          }
          if (message.role === 'tool') {
            /* 工具卡是证据：默认铺开；「只看回答」时才整体隐藏。 */
            return hideWork ? null : <ToolCard key={message.id} message={message} />
          }
          if (message.role === 'assistant' && message.surface === 'process') {
            return hideWork ? null : (
              <p
                key={message.id}
                className="whitespace-pre-wrap break-words px-0.5 text-sm leading-6 text-ink-muted"
                data-narration-line
              >
                {message.content}
              </p>
            )
          }
          return (
            <MessageRow
              key={message.id}
              message={message}
              sessionId={sessionId}
              onFork={onFork}
            />
          )
        })}
      </div>

      {hideWork && items.length > 0 && !hasAnswer && !live && (
        <p className="text-2xs text-ink-faint" data-answers-only-placeholder>
          {t('chat.a.answersHiddenEmpty', undefined, '本轮没有给出回答（工具与过程已隐藏）。')}
        </p>
      )}

      {live &&
        interjections.map((message) => (
          <InterjectionNote key={message.id} message={message} />
        ))}

      {live && streamingText !== null && (
        <div
          className="w-full break-words text-base leading-7 text-ink"
          data-streaming-bubble="true"
          data-message-row="assistant"
        >
          {/* 流式正文也走 Markdown：否则落库那一刻会从「星号原文」跳成「加粗」。 */}
          <WriteupMarkdown markdown={streamingText} sessionId={sessionId} />
          <span
            className="ml-0.5 inline-block h-3.5 w-[0.125rem] animate-cursor-pulse bg-ink-muted align-text-bottom"
            aria-hidden="true"
          />
        </div>
      )}

      {live && liveTool && (
        <LiveToolCard
          name={liveTool}
          args={liveToolArgs}
          output={liveToolOutput}
          elapsed={elapsed}
          heartbeat={liveToolProgress}
        />
      )}

      {live && (
        <LiveStatus
          phaseText={phaseText}
          elapsed={elapsed}
          progress={progress}
          heartbeat={liveToolProgress}
          t={t}
        />
      )}

      {!live && items.length > 0 && (
        <div
          className="flex min-h-5 items-center gap-2 pl-1 text-2xs text-ink-faint"
          data-turn-footer
          data-turn-steps={items.length}
          data-turn-tools={tools.length}
          data-turn-failures={failed}
          data-turn-at={last?.createdAt ?? ''}
        >
          {failed > 0 && !hasAnswer && (
            <span className="text-signal-risk">
              {t('chat.a.toolsFailed', { n: failed }, `· ${failed} 个工具失败`)}
            </span>
          )}
          {last && <span aria-hidden="true">·</span>}
          {last && <time dateTime={last.createdAt}>{formatClock(last.createdAt, locale)}</time>}
          {meta && (
            <>
              <span aria-hidden="true">·</span>
              <span className="min-w-0 truncate">{meta}</span>
            </>
          )}
        </div>
      )}
    </div>
  )
}

/** 这条消息是「最终答复」（要露正文、纯文本）还是「过程旁白」（可折）。 */
function isAnswer(message: ChatMessage): boolean {
  return message.role === 'assistant' && message.surface !== 'process'
}

/**
 * live 时的已等待秒数。
 *
 * 长任务里最难受的是"看不出它还在不在动"：阶段文案可能几十秒不变，
 * 一个持续增长的秒数是最便宜、也最诚实的活着的证据。
 */
/**
 * 运行中的工具卡（v1.92）。
 *
 * omp 的工具卡在执行期间就能看到输出在涨；这里用 SSE 的 `tool_output`
 * 增量画出同一件事。落库后由真正的 `ToolCard` 接管 —— 所以这张卡只需要
 * "活着"的证据：工具名、秒数、最后几屏输出。
 */
function LiveToolCard({
  name,
  args,
  output,
  elapsed,
  heartbeat,
}: {
  name: string
  args: Record<string, string> | null
  output: string | null
  elapsed: number
  heartbeat: { seconds: number; message?: string; progress?: number; total?: number } | null
}) {
  const { t } = useI18n()
  const ref = useRef<HTMLPreElement>(null)
  const digest = digestToolArgs(args)
  useEffect(() => {
    const el = ref.current
    if (el) el.scrollTop = el.scrollHeight
  }, [output])

  return (
    <div
      className="overflow-hidden rounded-md border border-line/70 bg-surface-raised/20"
      data-live-tool
    >
      <div className="flex items-center gap-2 px-2.5 py-1.5">
        <Loader2
          size={12}
          strokeWidth={2}
          className="animate-spin text-ink-faint"
          aria-hidden="true"
        />
        <span className="shrink-0 font-mono text-[12px] text-ink-muted">{name}</span>
        {digest && (
          <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-ink-faint">
            {digest}
          </span>
        )}
        <span className="shrink-0 text-2xs text-ink-faint">
          {t('chat.a.liveToolRunning', undefined, '执行中…')}
          {heartbeat ? progressLabelOf(heartbeat, t) : elapsed > 0 ? ` · ${formatElapsed(elapsed, t)}` : ''}
        </span>
      </div>
      {output ? (
        <pre
          ref={ref}
          className="max-h-[240px] overflow-auto border-t border-line/60 bg-surface-sunken/40 px-3 py-2 font-mono text-[11.5px] leading-[1.6] whitespace-pre-wrap break-words text-ink-muted"
        >
          {output}
          <span className="ml-0.5 inline-block h-3 w-[0.125rem] animate-cursor-pulse bg-ink-muted align-text-bottom" />
        </pre>
      ) : null}
    </div>
  )
}

/** 运行卡状态行：服务端给了 progress/total 就显示真实百分比，否则退成秒数。 */
function LiveStatus({
  phaseText,
  elapsed,
  progress,
  heartbeat,
  t,
}: {
  phaseText: string | null
  elapsed: number
  progress: { steps?: number; tools?: number } | null
  heartbeat: { seconds: number; message?: string; progress?: number; total?: number } | null
  t: Translate
}) {
  const total = heartbeat?.total ?? 0
  const pct =
    heartbeat?.progress !== undefined && total > 0
      ? Math.max(0, Math.min(100, Math.round((heartbeat.progress / total) * 100)))
      : null
  return (
    <div className="flex flex-col gap-1" data-live-status>
      <div className="flex min-w-0 items-center gap-2" role="status" aria-live="polite">
        <span className="flex h-3 shrink-0 items-end gap-[2px]" aria-hidden="true">
          <i className="live-bar" />
          <i className="live-bar" />
          <i className="live-bar" />
        </span>
        <span className="shrink-0 tabular-nums text-2xs text-ink-muted">
          {formatElapsedPrecise(elapsed)}
        </span>
        <span className="shrink-0 text-2xs text-ink-ghost" aria-hidden="true">
          |
        </span>
        <span className="min-w-0 truncate text-2xs text-ink-faint">
          {phaseText ?? t('chat.a.thinking', undefined, '正在想…')}
          {progressLabel(progress, t)}
          {heartbeat?.message && pct === null ? ` · ${heartbeat.message}` : ''}
        </span>
      </div>
      <div className="h-0.5 w-full overflow-hidden rounded-pill bg-line/60" aria-hidden="true">
        <div
          className={pct === null ? 'live-progress h-full rounded-pill bg-accent/70' : 'h-full rounded-pill bg-accent/70 transition-[width] duration-300'}
          style={pct === null ? undefined : { width: `${pct}%` }}
        />
      </div>
    </div>
  )
}

function progressLabelOf(
  p: {
    seconds: number
    message?: string
    progress?: number
    total?: number
  },
  t: Translate,
): string {
  if (p.progress !== undefined && p.total && p.total > 0) {
    const pct = Math.max(0, Math.min(100, Math.round((p.progress / p.total) * 100)))
    return ` · ${pct}%${p.message ? ` · ${p.message}` : ''}`
  }
  const base = p.seconds > 0 ? t('chat.a.waitedSuffix', { seconds: p.seconds }, ` · 已等待 ${p.seconds} 秒`) : ''
  return p.message ? `${base} · ${p.message}` : base
}

function useElapsed(live: boolean, since: string | null): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!live) return
    const timer = window.setInterval(() => setNow(Date.now()), 100)
    return () => window.clearInterval(timer)
  }, [live])

  if (!live || !since) return 0
  const start = new Date(since).getTime()
  if (Number.isNaN(start)) return 0
  return Math.max(0, (now - start) / 1000)
}

function formatElapsedPrecise(seconds: number): string {
  if (seconds < 60) return `${seconds.toFixed(1)}s`
  const minutes = Math.floor(seconds / 60)
  const rest = Math.floor(seconds % 60)
  return `${minutes}m ${String(rest).padStart(2, '0')}s`
}

function formatElapsed(seconds: number, t: Translate): string {
  if (seconds < 60) return t('chat.a.elapsedSeconds', { seconds }, `${seconds} 秒`)
  const minutes = Math.floor(seconds / 60)
  const rest = seconds % 60
  return rest > 0
    ? t('chat.a.elapsedMinutesSeconds', { minutes, rest }, `${minutes} 分 ${rest} 秒`)
    : t('chat.a.elapsedMinutes', { minutes }, `${minutes} 分`)
}

function formatClock(value: string, locale: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit' })
}

/**
 * 阶段行末尾的计数后缀（v1.30）。
 *
 * 只在「真的积累了一点东西」时才出现：0 次工具的时候写「（已 0 次工具）」
 * 是噪音，第一轮开始前那几秒本来也没有进度可言。
 */
function progressLabel(progress: { steps?: number; tools?: number } | null, t: Translate): string {
  if (!progress) return ''
  const tools = progress.tools ?? 0
  if (tools <= 0) return ''
  return t('chat.a.toolCountSuffix', { n: tools }, `（已 ${tools} 次工具）`)
}
