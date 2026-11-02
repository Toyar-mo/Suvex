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
  /** 当前会话 id：给正文里的 `/work/persist/...` 产物做下载链接。 */
  sessionId?: string
}) {
  const finalMessages = items.filter(
    (message) => message.role === 'assistant' && message.surface !== 'process',
  )
  const workItems = items.filter((message) => !finalMessages.includes(message))
  const tools = items.filter((message) => message.role === 'tool')
  const failed = tools.filter((message) => message.toolFailed === true).length
  const last = items.at(-1)

  return (
    <div className="flex flex-col gap-3" data-assistant-turn>
      {workItems.length > 0 && (
        <AssistantWork items={workItems} live={live} deciding={deciding} onDecide={onDecide} />
      )}

      {finalMessages.length > 0 && (
        /*
          ★ 过程 → 结论之间留 24px（12px 间距 + 12px 内边距）。
            两者贴在一起时，“这是结论”只能靠用户自己读出来。
            没有过程时不加内边距 —— 否则每个纯文本回合顶上都会多一截空白。
        */
        <div className={`flex flex-col gap-3${workItems.length > 0 ? ' pt-3' : ''}`}>
          {finalMessages.map((message) => (
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
      )}

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
          <span className="text-2xs text-ink-faint">{phaseText ?? '正在想…'}</span>
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

function formatClock(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}
