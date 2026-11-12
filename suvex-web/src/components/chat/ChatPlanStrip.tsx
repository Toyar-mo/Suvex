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
  const showList = !allDone || expanded

  return (
    <div className="shrink-0 px-3 pt-2" data-testid="chat-plan">
      <div className="mx-auto w-full max-w-[880px] rounded-md border border-line bg-surface-sunken px-3 py-2">
        <button
          type="button"
          onClick={() => setExpanded((value) => !value)}
          aria-expanded={showList}
          className="flex w-full items-center gap-1 text-left text-2xs text-ink-faint transition-ui duration-fast hover:text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
        >
          {allDone &&
            (expanded ? (
              <ChevronDown size={10} strokeWidth={2} aria-hidden="true" />
            ) : (
              <ChevronRight size={10} strokeWidth={2} aria-hidden="true" />
            ))}
          计划 · {done}/{todos.length} 完成
          {allDone && !expanded && <span className="ml-1">· 点击展开</span>}
        </button>
        {showList && (
        <ol className="mt-1 flex flex-col gap-0.5">
          {todos.map((todo) => (
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
