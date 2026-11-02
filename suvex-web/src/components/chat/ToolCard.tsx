import { useState } from 'react'
import { ChevronDown, ChevronRight, Terminal, Wrench } from 'lucide-react'
import { useRevealOnExpand } from '@/hooks/useRevealOnExpand'
import { cn } from '@/lib/cn'
import type { ChatMessage } from '@/data/chat'

/**
 * 工具卡：它到底做了什么。
 *
 * ★ 失败必须**看得出来**。一次静默失败（工具返回了错误但界面显示成正常）
 *   会让用户以为"扫描已建"然后去扫描页找一个不存在的任务 —— 这比报错更糟。
 *
 * ## v0.103：默认收成**一行**
 *
 * 第一版做的是"长输出才折叠、折叠时露前两行"。对照 Claude Code / Codex
 * 的形态：**每条工具调用默认就是一行灰色文字**，想看细节才点开。
 *
 * 对话页的主线是**模型说了什么**，工具调用是过程证据。过程证据默认占一行，
 * 是这两种价值的正确配比。所以现在**无条件折叠**。
 *
 * ## 为什么不复用 ReasoningBlock
 *
 * 形态接近（都是一行可展开），但语义不同：思考过程是纯文本，工具卡要表达
 * **成功/失败**、要显示工具名和参数摘要。失败状态必须留在**折叠态**那一行上。
 */
export function ToolCard({ message, compact = false }: { message: ChatMessage; compact?: boolean }) {
  const failed = message.toolFailed === true
  const content = message.content ?? ''
  const lineCount = content.split('\n').length
  const [open, setOpen] = useState(false)
  const bodyRef = useRevealOnExpand<HTMLDivElement>(open)

  /*
    摘要行写什么 —— 这是折叠态**唯一**的信息，所以它必须回答
    "这次调用干了什么"，而不是"这是第几条消息"。

    ★ 纯字符串扫描，不走 `useMemo`：一次最多看 7 个键、截 80 字，
      记忆化的开销比它省下的还多；而且 React Compiler 遇到手写 memo
      会整块跳过优化（`react-hooks/preserve-manual-memoization`）。
  */
  const argDigest = digestToolArgs(message.toolArgs)

  return (
    <div
      data-message-row="tool"
      data-seq={message.seq}
      data-tool-name={message.toolName ?? ''}
      data-tool-collapsed={open ? 'false' : 'true'}
      className={cn(compact && 'py-0')}
    >
      <button
        type="button"
        data-tool-toggle="true"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className={cn(
          'flex w-full items-center gap-1.5 rounded-sm px-1 text-left',
          compact ? 'py-0.5' : 'py-1',
          'transition-colors duration-150',
          'focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
          failed ? 'text-signal-risk hover:bg-signal-risk/5' : 'text-ink-faint hover:text-ink-muted',
        )}
      >
        {open ? (
          <ChevronDown size={11} strokeWidth={2} className="shrink-0" aria-hidden="true" />
        ) : (
          <ChevronRight size={11} strokeWidth={2} className="shrink-0" aria-hidden="true" />
        )}
        {failed ? (
          <Wrench size={11} strokeWidth={2} className="shrink-0" aria-hidden="true" />
        ) : (
          <Terminal size={11} strokeWidth={2} className="shrink-0" aria-hidden="true" />
        )}
        <span className="shrink-0 font-mono text-2xs">{message.toolName ?? 'tool'}</span>
        {argDigest && (
          <span className="min-w-0 truncate text-2xs text-ink-faint/80">{argDigest}</span>
        )}
        {failed && (
          <span className="shrink-0 rounded-pill bg-signal-risk/15 px-1.5 text-2xs text-signal-risk">
            失败
          </span>
        )}
        <span className="ml-auto shrink-0 text-2xs text-ink-faint/60">
          {open ? '收起' : `${lineCount} 行`}
        </span>
      </button>

      {open && (
        <div
          ref={bodyRef}
          className={cn(
            'mt-1 rounded-sm border px-2.5 py-1.5',
            failed ? 'border-signal-risk/40 bg-signal-risk/5' : 'border-line bg-surface-sunken',
          )}
        >
          <p className="whitespace-pre-wrap break-words text-xs text-ink-muted">{content}</p>
        </div>
      )}
    </div>
  )
}

/** 折叠态那行参数摘要：只在白名单键里找第一个非空字符串。 */
function digestToolArgs(args: ChatMessage['toolArgs']): string {
  if (!args) return ''
  for (const key of ['command', 'cmd', 'path', 'name', 'query', 'url', 'target']) {
    const value = args[key]
    if (typeof value === 'string' && value.trim()) {
      return value.trim().split('\n')[0].slice(0, 80)
    }
  }
  return ''
}
