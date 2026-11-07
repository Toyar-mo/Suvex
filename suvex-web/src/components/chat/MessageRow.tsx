import { useState } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'
import { ApprovalCard } from '@/components/chat/ChatControls'
import { ToolCard } from '@/components/chat/ToolCard'
import { cn } from '@/lib/cn'
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
export function MessageRow({
  message,
  deciding,
  onDecide,
  sessionId,
}: {
  message: ChatMessage
  /** 有一条审批正在提交 —— 禁用按钮防重复点。 */
  deciding?: boolean
  onDecide?: (approve: boolean) => void
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
        <div className="max-w-[520px] rounded-[10px] rounded-br-xs border border-line bg-surface px-3.5 py-2">
          <p className="whitespace-pre-wrap break-words text-base leading-[1.5] text-ink">
            {message.content}
          </p>
        </div>
      </div>
    )
  }

  if (message.role === 'tool') {
    return <ToolCard message={message} />
  }

  return (
    <div className="flex flex-col gap-1.5" data-message-row="assistant" data-seq={message.seq}>
      {message.reasoning && <ReasoningBlock text={message.reasoning} />}
      <div className="break-words text-base leading-7 text-ink" data-markdown-answer>
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
export function ReasoningBlock({ text }: { text: string }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="rounded-md border border-line/70 bg-surface-sunken/40">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={cn(
          'flex w-full items-center gap-1 px-2 py-1 text-left text-2xs text-ink-faint',
          'transition-colors duration-150 hover:text-ink-muted',
          'focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
        )}
        aria-expanded={open}
      >
        {open ? (
          <ChevronDown size={11} strokeWidth={2} aria-hidden="true" />
        ) : (
          <ChevronRight size={11} strokeWidth={2} aria-hidden="true" />
        )}
        思考过程
      </button>
      {open && (
        <p className="whitespace-pre-wrap break-words border-t border-line/60 px-2 py-1.5 text-2xs leading-relaxed text-ink-muted">
          {text}
        </p>
      )}
    </div>
  )
}
