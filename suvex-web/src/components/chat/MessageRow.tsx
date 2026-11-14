import { memo, useState } from 'react'

import { useI18n } from '@/i18n'
import { copyText } from '@/lib/clipboard'
import { Check, ChevronDown, ChevronRight, Copy, GitBranch, Sparkles } from 'lucide-react'
import { ApprovalCard } from '@/components/chat/ChatControls'
import { ToolCard } from '@/components/chat/ToolCard'
import { WriteupMarkdown } from '@/components/writeup/WriteupMarkdown'
import type { ChatMessage } from '@/data/chat'

/**
 * 一条消息。三种角色三种形状。`onDecide` 只在该消息待审批时传。
 *
 * ## 与 v1.25 的差别
 *
 * 助手正文不再包一层灰底气泡。metis 的对话页里，助手最终答复是**无气泡的
 * 正文块**（只有用户消息有气泡），这样长回答不会变成一大块灰砖；分隔靠
 * 间距与回合页脚，而不是每个回合一个框。
 *
 * ## v1.75：助手正文改为 Markdown
 *
 * 成熟客户端（ChatGPT / Claude / LobeChat）都渲染 Markdown：加粗、列表、
 * 代码块、表格。之前是纯文本，模型写的 `**结论**` 会原样显示成星号。
 * 这里直接复用题解的渲染器（同一套 components / 代码高亮 / 列表缩进），
 * 避免两套样式漂移；`sessionId` 传下去，`/work/persist/...` 仍可下载。
 */
export const MessageRow = memo(
  MessageRowImpl,
  /*
    只比**真正影响输出**的东西。

    ★ 为什么必须 memo：流式回答每来一段增量，整个会话都会重渲染一次；
      `MessageRow` 里挂着 `WriteupMarkdown`，那是一次完整的 Markdown
      解析 + 语法高亮。历史消息的内容一个字都没变，却跟着重新解析 ——
      长会话（几十条回答）下这是最贵的一笔开销。

    ★ 为什么不比 `onDecide`：它是页面上每次渲染新建的箭头函数，
      引用永远不同，比了等于没 memo。而它只在**待审批**的那条消息上
      才被调用，那种行本来就该跟着 `deciding` 变。
  */
  (prev, next) =>
    prev.message === next.message &&
    prev.sessionId === next.sessionId &&
    prev.deciding === next.deciding &&
    prev.onFork === next.onFork &&
    // 待审批的那一行必须跟着回调用最新的（理由见 AssistantTurn 的比较函数）
    (prev.message.approvalStatus !== 'pending' || prev.onDecide === next.onDecide),
) as typeof MessageRowImpl

function MessageRowImpl({
  message,
  deciding,
  onDecide,
  sessionId,
  onFork,
}: {
  message: ChatMessage
  /** 有一条审批正在提交 —— 禁用按钮防重复点。 */
  deciding?: boolean
  onDecide?: (approve: boolean) => void
  /** 从这里分叉出一条新时间线。 */
  onFork?: (seq: number) => void
  /** 当前会话 id。给了才把正文里的 `/work/persist/...` 渲染成下载链接。 */
  sessionId?: string
}) {
  /*
    ★ 待审批的消息**优先**按审批卡渲染，不看 role。

      它 role 是 `tool`，但工具还没执行过。当普通工具卡画出来，
      用户在界面上看到的就是"它跑完了" —— 而实际上它在等一句话。
      这个分支必须排在 role 判断之前。
  */
  if (message.approvalStatus === 'pending' && onDecide) {
    return <ApprovalCard message={message} onDecide={onDecide} disabled={deciding} />
  }

  if (message.role === 'user') {
    return (
      <div
        className="flex justify-end"
        // ★ v0.100：稳定标记。端到端验证要靠它数「屏幕上有几条消息」。
        data-message-row="user"
        data-seq={message.seq}
      >
        <div className="flex max-w-[520px] flex-col items-end gap-1">
          <div className="rounded-lg rounded-br-xs border border-line bg-surface px-3.5 py-2">
            <p className="whitespace-pre-wrap break-words text-base leading-[1.5] text-ink">
              {message.content}
            </p>
          </div>
          <div className="flex items-center gap-2 pr-0.5 text-2xs text-ink-faint/80">
            <time className="tabular-nums" dateTime={message.createdAt}>
              {formatClock(message.createdAt)}
            </time>
            <CopyButton text={message.content} />
            {onFork && <ForkButton seq={message.seq} onFork={onFork} />}
          </div>
        </div>
      </div>
    )
  }

  if (message.role === 'tool') {
    return <ToolCard message={message} />
  }

  return (
    <div className="flex flex-col gap-1.5" data-message-row="assistant" data-seq={message.seq}>
      {message.reasoning && (
        <ReasoningBlock text={message.reasoning} durationMs={message.durationMs} />
      )}
      <div className="group/answer relative break-words text-base leading-7 text-ink" data-markdown-answer>
        <div className="absolute -top-1 right-0 flex items-center gap-1 opacity-0 transition-opacity duration-150 focus-within:opacity-100 group-hover/answer:opacity-100">
          <CopyButton text={message.content} />
          {onFork && <ForkButton seq={message.seq} onFork={onFork} />}
        </div>
        <WriteupMarkdown markdown={message.content} sessionId={sessionId} />
      </div>
    </div>
  )
}

/**
 * 可折叠的「思考过程」。
 *
 * ★ 默认折叠。它是"我要看才展开"的东西，不是正文；默认铺开会让每条回复
 *   都变成两屏，而用户大多数时候只关心结论。
 */
export function ReasoningBlock({
  text,
  durationMs,
  live = false,
  defaultOpen = false,
}: {
  text: string
  durationMs?: number | null
  live?: boolean
  defaultOpen?: boolean
}) {
  const { t } = useI18n()
  const [open, setOpen] = useState(defaultOpen)
  const seconds =
    typeof durationMs === 'number' && durationMs > 0
      ? Math.max(1, Math.round(durationMs / 1000))
      : null
  return (
    <div className="flex animate-stream-in flex-col" data-reasoning>
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="group flex w-max items-center gap-1.5 rounded-sm py-0.5 text-left text-2xs italic text-ink-faint transition-colors duration-150 hover:text-ink-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
        aria-expanded={open}
      >
        {open ? (
          <ChevronDown size={11} strokeWidth={2} aria-hidden="true" />
        ) : (
          <ChevronRight size={11} strokeWidth={2} aria-hidden="true" />
        )}
        <Sparkles size={11} strokeWidth={2} aria-hidden="true" className="opacity-70" />
        <span>
          {live
            ? t('chat.a.thinking', undefined, '正在想…')
            : seconds
              ? t('chat.thoughtFor', { seconds }, `Thought for ${seconds}s`)
              : t('chat.thinking', undefined, '思考过程')}
        </span>
      </button>
      {open && (
        /* omp 的形态：折叠时只是一行 "Thought for 1s ›"，展开才读全文。
           不套卡片边框 —— 思考不是证据卡片，它是正文的附注。 */
        <p className="mt-1 animate-stream-in whitespace-pre-wrap break-words border-l border-line/80 pl-2.5 text-xs leading-relaxed text-ink-muted">
          {text}
        </p>
      )}
    </div>
  )
}

/** 与 AssistantTurn 的时钟格式一致（HH:MM），用户消息下方用。 */
function formatClock(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}

/** 复制一条消息的原文（markdown 源文，不是渲染后的纯文本）。 */
function CopyButton({ text }: { text: string }) {
  const { t } = useI18n()
  const [copied, setCopied] = useState(false)
  return (
    <button
      type="button"
      onClick={() => {
        void copyText(text).then((ok) => {
          if (!ok) return
          setCopied(true)
          window.setTimeout(() => setCopied(false), 1500)
        })
      }}
      aria-label={t('chat.copyMessage', undefined, '复制消息')}
      title={t('chat.copyMessage', undefined, '复制消息')}
      className="rounded-sm p-0.5 text-ink-faint transition-ui duration-fast hover:text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
    >
      {copied ? (
        <Check size={11} strokeWidth={2.25} aria-hidden="true" />
      ) : (
        <Copy size={11} strokeWidth={2} aria-hidden="true" />
      )}
    </button>
  )
}

/** 从这条消息分叉：复制截至它的历史，另开一条时间线。 */
function ForkButton({ seq, onFork }: { seq: number; onFork: (seq: number) => void }) {
  const { t } = useI18n()
  return (
    <button
      type="button"
      onClick={() => onFork(seq)}
      aria-label={t('chat.fork', undefined, '从这里分叉')}
      title={t('chat.fork', undefined, '从这里分叉')}
      className="rounded-sm p-0.5 text-ink-faint transition-ui duration-fast hover:text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
    >
      <GitBranch size={11} strokeWidth={2} aria-hidden="true" />
    </button>
  )
}
