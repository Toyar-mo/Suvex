import { MessageRow } from '@/components/chat/MessageRow'
import { AssistantWork } from '@/components/chat/AssistantWork'
import { InterjectionNote } from '@/components/chat/InterjectionNote'
import type { ChatMessage } from '@/data/chat'

/**
 * 一个助手回合。
 *
 * ## 结构（对照 metis 的 AssistantTurn / AssistantWork）
 *
 * ```
 * ┌ 工作过程（旁白折叠 + 工具分组）           ← 有过程才出现
 * ├ 最终答复（纯文本）                        ← surface !== 'process' 的助手消息
 * ├ 思考过程（可折叠，跟随那条消息）
 * ├ 插话（跑着的时候用户在回合中间说的话）
 * ├ 回合页脚：N 步 · M 个工具 · 时间           ← 非流式时出现
 * └ （流式中）正文光标 +「第 N 轮 · 在做什么」
 * ```
 *
 * ## 为什么插话画在回合里
 *
 * 插话确实属于**正在进行**的这一轮：模型下一轮就会读到它。画在回合
 * 外面（消息流末尾）会让「阶段状态行」跑到用户刚说的话上方，读起来像
 * 那句话发出去之后模型才开始想。
 *
 * ## 为什么"最终答复"要挑出来
 *
 * 过程旁白与工具结果都属于**证据**，默认收着；用户真正要看的是结论。
 * 后端已经用 `surface` 标好了：`process` 是过程，其余是给人看的正文。
 * 没有 surface 的历史消息按正文处理（不能因为字段缺失就把旧对话藏起来）。
 */
export function AssistantTurn({
  items,
  live = false,
  streamingText = null,
  phaseText = null,
  deciding,
  onDecide,
  interjections = [],
  meta,
  progress = null,
  sessionId,
}: {
  items: ChatMessage[]
  live?: boolean
  streamingText?: string | null
  phaseText?: string | null
  deciding?: boolean
  onDecide?: (messageId: string, approve: boolean) => void
  /** 跑的过程中用户插的话（已落库、等下一轮被读到）。 */
  interjections?: ChatMessage[]
  /** 页脚右侧的附加信息（例如人格 / 接入点）。 */
  meta?: string | null
  /**
   * 跑着的时候，已经积累了多少（v1.30）。
   *
   * 长任务（贴一段 ffuf/curl，模型能连着跑十几二十次工具）最难受的地方
   * 是：那一行「第 4 轮 · 正在执行 run_shell…」从第 1 次工具到第 18 次
   * 长得一模一样。用户无法区分「在推进」和「卡在这个工具上」。
   *
   * 所以不传 `phaseText` 也行 —— 只要给 `progress`，这一行就把计数带上：
   * 「第 4 轮 · 正在执行 run_shell…（已 18 次工具）」。
   */
  progress?: { steps?: number; tools?: number } | null
  /** 当前会话 id：给正文里的 `/work/persist/...` 产物做下载链接。 */
  sessionId?: string
}) {
  const tools = items.filter((message) => message.role === 'tool')
  const failed = tools.filter((message) => message.toolFailed === true).length
  const last = items.at(-1)
  // ★ v1.56b：单一时间线（对照 CyberStrikeAI 的事件流）。按到达顺序分段，
  //   推理 / 工具 / 结果 / 答复各在**它发生的那个位置**逐条铺下来，不再把
  //   全部过程收进顶上「过程详情」折叠块、结论另起一块贴在最后。
  const segments = buildTimelineSegments(items)

  return (
    <div className="flex flex-col gap-3" data-assistant-turn data-timeline>
      {segments.map((segment) => {
        if (segment.kind === 'answer') {
          return (
            <MessageRow
              key={`a-${segment.message.id}`}
              message={segment.message}
              sessionId={sessionId}
              deciding={deciding}
              onDecide={
                segment.message.approvalStatus === 'pending'
                  ? (approve) => onDecide?.(segment.message.id, approve)
                  : undefined
              }
            />
          )
        }
        if (segment.kind === 'tools') {
          return (
            <div key={`t-${segment.items[0].id}`} className="flex flex-col gap-1">
              {segment.items.map((message) => (
                <MessageRow
                  key={message.id}
                  message={message}
                  sessionId={sessionId}
                  deciding={deciding}
                  onDecide={
                    message.approvalStatus === 'pending'
                      ? (approve) => onDecide?.(message.id, approve)
                      : undefined
                  }
                />
              ))}
            </div>
          )
        }
        // 连续旁白收成一条可折叠的「过程」条，**留在它在时间线里的位置**。
        return (
          <AssistantWork
            key={`n-${segment.items[0].id}`}
            items={segment.items}
            live={live}
            deciding={deciding}
            onDecide={onDecide}
          />
        )
      })}

      {live &&
        interjections.map((message) => (
          <InterjectionNote key={message.id} message={message} />
        ))}

      {live && streamingText !== null && (
        <div
          className="w-full whitespace-pre-wrap break-words text-sm leading-7 text-ink"
          data-streaming-bubble="true"
          data-message-row="assistant"
        >
          {streamingText}
          <span
            className="ml-0.5 inline-block h-3.5 w-[0.125rem] animate-cursor-pulse bg-ink-muted align-text-bottom"
            aria-hidden="true"
          />
        </div>
      )}

      {live && (
        <div className="flex items-center gap-2" role="status" aria-live="polite">
          <span className="relative flex h-1.5 w-1.5" aria-hidden="true">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-pill bg-accent opacity-60" />
            <span className="relative inline-flex h-1.5 w-1.5 rounded-pill bg-accent" />
          </span>
          <span className="text-2xs text-ink-faint">
            {phaseText ?? '正在想…'}
            {progressLabel(progress)}
          </span>
        </div>
      )}

      {!live && items.length > 0 && (
        <div
          className="flex min-h-5 items-center gap-2 pl-1 text-2xs text-ink-faint/80"
          data-turn-footer
          data-turn-steps={items.length}
          data-turn-tools={tools.length}
          data-turn-failures={failed}
          data-turn-at={last?.createdAt ?? ''}
        >
          <span>
            {items.length} 步
            {tools.length > 0 && ` · ${tools.length} 个工具`}
            {failed > 0 && <span className="text-signal-risk"> · {failed} 个失败</span>}
          </span>
          {last && <span aria-hidden="true">·</span>}
          {last && <time dateTime={last.createdAt}>{formatClock(last.createdAt)}</time>}
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

/*
  ★ v1.56b：把一回合的消息切成**按发生顺序**的时间线段。

  CyberStrikeAI 的事件流是 progress / iteration / thinking / tool_call /
  tool_result / 最终答复 逐条推、前端按到达顺序逐条追加。落到这里就是：
  「旁白 → 工具 → 旁白 → 工具 → 答复」原样铺下来，不再「过程全收进顶上
  一个折叠块、结论另起一块贴最后」。

  切段规则（只把**连续的同类**合在一起，绝不跨类型重排）：
  - 连续工具消息 → 一个 tools 段（每张 ToolCard 仍各占一行）；
  - 连续旁白（assistant 且 surface==='process'）→ 一个 narration 段
    （沿用它原来的「过程详情」折叠条，只是留在时间线原位）；
  - 最终答复（assistant 且 surface!=='process'）→ 单条 narration 段，
    由 MessageRow 画成纯文本正文 —— 它就出现在它**实际产生的位置**。
  - 待审批消息单独成段（它 role 可能是 tool，但还没执行），
    MessageRow 会优先画审批卡。

  为什么最终答复也走 narration 段：AssistantWork 对「只有一条、且不是
  process」的项不会把它当旁白折起来 —— 见下面 timeline 渲染里对单条
  answer 的处理。这样答复正文位置正确、形态也仍是纯文本。
 */
type TimelineSegment =
  | { kind: 'tools'; items: ChatMessage[] }
  | { kind: 'narration'; items: ChatMessage[] }
  | { kind: 'answer'; message: ChatMessage }

/** 这条消息是「最终答复」（要露正文、纯文本）还是「过程旁白」（可折）。 */
function isAnswer(message: ChatMessage): boolean {
  return message.role === 'assistant' && message.surface !== 'process'
}

function buildTimelineSegments(items: ChatMessage[]): TimelineSegment[] {
  const segments: TimelineSegment[] = []
  let tools: ChatMessage[] = []
  let narration: ChatMessage[] = []

  const flushTools = () => {
    if (tools.length > 0) {
      segments.push({ kind: 'tools', items: tools })
      tools = []
    }
  }
  const flushNarration = () => {
    if (narration.length > 0) {
      segments.push({ kind: 'narration', items: narration })
      narration = []
    }
  }

  for (const message of items) {
    // 最终答复自成一段，永远画正文（不折叠、也不跟旁白混）。
    if (isAnswer(message)) {
      flushTools()
      flushNarration()
      segments.push({ kind: 'answer', message })
      continue
    }
    // 待审批消息：role 可能还是 tool，但还没执行，单独成段走 MessageRow 审批卡。
    if (message.approvalStatus === 'pending') {
      flushTools()
      flushNarration()
      segments.push({ kind: 'tools', items: [message] })
      continue
    }
    if (message.role === 'tool') {
      flushNarration()
      tools.push(message)
      continue
    }
    flushTools()
    narration.push(message)
  }
  flushTools()
  flushNarration()
  return segments
}

function formatClock(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}

/**
 * 阶段行末尾的计数后缀（v1.30）。
 *
 * 只在「真的积累了一点东西」时才出现：0 次工具的时候写「（已 0 次工具）」
 * 是噪音，第一轮开始前那几秒本来也没有进度可言。
 *
 * 为什么只报工具次数、不报轮次：轮次已经在 `phaseLabel` 的前缀里
 * （「第 4 轮 · …」），再写一遍就成了「第 4 轮 · …（第 4 轮）」。工具次数
 * 是真正随时间长的那一维，也是用户判断"它还在动"的依据。
 */
function progressLabel(progress: { steps?: number; tools?: number } | null): string {
  if (!progress) return ''
  const tools = progress.tools ?? 0
  if (tools <= 0) return ''
  return `（已 ${tools} 次工具）`
}
