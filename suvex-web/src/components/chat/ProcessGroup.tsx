import { useState } from 'react'
import { ChevronRight, Wrench } from 'lucide-react'
import { cn } from '@/lib/cn'
import type { ChatMessage } from '@/data/chat'

/**
 * 「过程详情」折叠块（v1.25）。
 *
 * ## 它解决的问题
 *
 * 用户原话：「又中断了，就不能一次性把 flag 给我解出来吗」，并贴出截图：
 * 一串小气泡夹在工具行之间（「我先看看附件里是什么。」「解出来的文件叫 pwn…」
 * 「这是 CTF 的 pwn 二进制，我拆开看逻辑。」）。那是**工作过程**，不是回答。
 *
 * 它把真正的结论挤出视野，而且任何一句被截断都长得像"它停在半路"。
 * 所以过程证据默认收成**一行**：`过程详情 · N 步（M 个工具）`，点开才看。
 *
 * ## 为什么默认收起
 *
 * 与工具卡同一个理由（见 `ToolCard` 的注释）：对话页的主线是
 * **模型最后说了什么**，过程是证据。默认值代表谁的意见 —— 这里选"别挡着看结论"。
 *
 * ## 为什么失败要冒到折叠标题上
 *
 * 折叠态如果不体现失败，用户收着就永远看不见"某个工具偷偷挂了"。
 * 这和 `ToolCard` 在折叠行上保留失败红点是同一条纪律。
 */
export function ProcessGroup({ items }: { items: ChatMessage[] }) {
  const [open, setOpen] = useState(false)
  const tools = items.filter((m) => m.role === 'tool')
  const failed = tools.filter((m) => m.toolFailed === true).length
  const steps = items.length

  return (
    <div className="flex flex-col gap-1" data-process-group data-process-steps={steps}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className={cn(
          'flex w-full items-center gap-1.5 rounded-sm px-1 py-0.5 text-left',
          'text-2xs text-ink-faint transition-ui duration-fast',
          'hover:text-ink-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
        )}
      >
        <ChevronRight
          size={11}
          strokeWidth={2}
          aria-hidden="true"
          className={cn('transition-transform duration-fast', open && 'rotate-90')}
        />
        <Wrench size={11} strokeWidth={2} aria-hidden="true" />
        <span>
          过程详情 · {steps} 步
          {tools.length > 0 && `（${tools.length} 个工具）`}
        </span>
        {failed > 0 && (
          <span className="text-signal-risk" data-process-failed>
            · {failed} 个失败
          </span>
        )}
      </button>

      {open && (
        <div className="ml-1 flex flex-col gap-1 border-l border-line pl-2">
          {items.map((m) => (
            <ProcessItem key={m.id} message={m} />
          ))}
        </div>
      )}
    </div>
  )
}

/** 过程里的一条：工具结果或模型旁白。 */
function ProcessItem({ message }: { message: ChatMessage }) {
  if (message.role === 'tool') {
    const failed = message.toolFailed === true
    return (
      <div
        className="flex flex-col gap-0.5"
        data-process-item="tool"
        data-tool-failed={failed ? 'true' : 'false'}
      >
        <p className={cn('text-2xs', failed ? 'text-signal-risk' : 'text-ink-faint')}>
          <span className="font-mono">{message.toolName ?? 'tool'}</span>
          {failed && ' · 失败'}
        </p>
        <pre className="max-h-60 overflow-auto whitespace-pre-wrap break-words rounded-xs bg-surface-sunken px-2 py-1 text-2xs text-ink-muted">
          {message.content || '（无输出）'}
        </pre>
      </div>
    )
  }
  return (
    <p
      className="whitespace-pre-wrap break-words text-2xs text-ink-faint"
      data-process-item="narration"
    >
      {message.content}
    </p>
  )
}
