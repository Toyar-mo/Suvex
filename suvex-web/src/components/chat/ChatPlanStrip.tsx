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

  return (
    <div className="shrink-0 px-3 pt-2" data-testid="chat-plan">
      <div className="mx-auto w-full max-w-[880px] rounded-md border border-line bg-surface-sunken px-3 py-2">
        <p className="text-2xs text-ink-faint">
          计划 · {done}/{todos.length} 完成
        </p>
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
      </div>
    </div>
  )
}
