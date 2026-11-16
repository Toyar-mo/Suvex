import { ChevronDown, ChevronRight } from 'lucide-react'
import { useEffect, useState } from 'react'

import { listTodos, type ChatTodoItem } from '@/data/chat'
import { cn } from '@/lib/cn'

/**
 * 对话页的计划条（v1.89）。
 *
 * ## 它是只读的
 *
 * 计划由模型在对话里用 `create_todo` / `update_todo` 维护 —— 界面上能改的话，
 * 就要回答"人和模型同时改谁赢"，而这不是这一版要解决的问题。这里只负责
 * 让用户看见"它打算做几步、走到哪了"。
 *
 * ## 什么时候刷新
 *
 * `refreshKey` 由调用方传（现在用消息条数）：一轮结束要么多一条消息、
 * 要么多一条工具结果，两种情况都会让它变。这样不需要为计划单独开 SSE。
 */
export function ChatPlanStrip({
  sessionId,
  refreshKey,
}: {
  sessionId: string
  refreshKey: number
}) {
  const [todos, setTodos] = useState<ChatTodoItem[]>([])
  const [expanded, setExpanded] = useState(false)

  useEffect(() => {
    let cancelled = false
    void listTodos(sessionId).then((rows) => {
      if (!cancelled) setTodos(rows)
    })
    return () => {
      cancelled = true
    }
  }, [sessionId, refreshKey])

  if (todos.length === 0) return null

  const done = todos.filter((todo) => todo.status === 'done').length
  const allDone = done === todos.length
  const hasDone = done > 0
  const showList = !allDone || expanded
  // 未展开时只显示还没做完的步骤；完成项默认收起来，计划才不会越堆越高。
  const visible = expanded ? todos : todos.filter((todo) => todo.status !== 'done')

  return (
    <div className="shrink-0 px-3 pt-2" data-testid="chat-plan">
      <div className="mx-auto w-full max-w-[880px] rounded-md border border-line bg-surface-sunken px-3 py-2">
        <button
          type="button"
          onClick={() => setExpanded((value) => !value)}
          aria-expanded={expanded}
          className="flex w-full items-center gap-1 text-left text-2xs text-ink-faint transition-ui duration-fast hover:text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
        >
          {hasDone &&
            (expanded ? (
              <ChevronDown size={10} strokeWidth={2} aria-hidden="true" />
            ) : (
              <ChevronRight size={10} strokeWidth={2} aria-hidden="true" />
            ))}
          计划 · {done}/{todos.length} 完成
          {hasDone && !expanded && <span className="ml-1">· 点击展开已完成</span>}
        </button>
        {showList && (
        <ol className="mt-1 flex max-h-40 flex-col gap-0.5 overflow-y-auto pr-1">
          {visible.map((todo) => (
            <li key={todo.seq} className="flex gap-1.5 text-xs">
              <span className="tabular-nums text-ink-faint">#{todo.seq}</span>
              <span
                className={cn(
                  todo.status === 'done' && 'text-ink-faint line-through',
                  todo.status === 'in_progress' && 'text-ink',
                  todo.status === 'pending' && 'text-ink-muted',
                )}
              >
                {todo.text}
              </span>
            </li>
          ))}
        </ol>
        )}
      </div>
    </div>
  )
}
