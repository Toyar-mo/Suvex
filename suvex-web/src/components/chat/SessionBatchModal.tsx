import { Button } from '@/components/ui/Button'
import { Checkbox } from '@/components/ui/Checkbox'
import { Modal } from '@/components/ui/Modal'
import type { ChatSession } from '@/data/chat'
import { cn } from '@/lib/cn'
import { useMemo, useState } from 'react'

/**
 * 批量管理会话。
 *
 * ## 为什么是"逐个请求"而不是一个 batch 端点
 *
 * 参照 CyberStrikeAI（`chat.js:11121` 的 `for (const id of ids) await
 * deleteConversation(id, true)`）：对话的批量操作天然是**低并发、低频**
 * （一个人一次勾十几条，一天点不了几次），一个 batch 端点换来的是
 * 「部分成功怎么回、幂等怎么做、权限按条还是按批」三个新问题。
 * 而逐个循环唯一的缺点是慢几百毫秒 —— 在这个动作的使用频率下不值一提。
 *
 * ## 为什么失败要**逐条记下来**
 *
 * 循环里第 7 条失败时，前 6 条已经删了。只报一句"删除失败"会让用户
 * 完全不知道到底删掉了几条 —— 而下一步他要不要重试取决于这个数字。
 */

export interface SessionBatchModalProps {
  open: boolean
  sessions: ChatSession[]
  projects: string[]
  busy: boolean
  onClose: () => void
  /** 批量移动到分组。`null` = 无分组。 */
  onMoveTo: (ids: string[], project: string | null) => Promise<void>
  /** 批量删除（= 归档）。返回失败条数。 */
  onArchive: (ids: string[]) => Promise<number>
}

export function SessionBatchModal({
  open,
  sessions,
  projects,
  busy,
  onClose,
  onMoveTo,
  onArchive,
}: SessionBatchModalProps) {
  const [query, setQuery] = useState('')
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [moveTo, setMoveTo] = useState<string>('')

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return sessions
    return sessions.filter((s) => (s.title || '').toLowerCase().includes(q))
  }, [sessions, query])

  const allPicked = filtered.length > 0 && filtered.every((s) => picked.has(s.id))

  function toggleAll() {
    setPicked((current) => {
      const next = new Set(current)
      if (allPicked) filtered.forEach((s) => next.delete(s.id))
      else filtered.forEach((s) => next.add(s.id))
      return next
    })
  }

  function toggleOne(id: string) {
    setPicked((current) => {
      const next = new Set(current)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const pickedIds = [...picked]

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={`批量管理对话${pickedIds.length > 0 ? `（已选 ${pickedIds.length} 条）` : ''}`}
      widthClass="max-w-2xl"
      footer={
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <label htmlFor="batch-move-select" className="text-xs text-ink-muted">
              移动到
            </label>
            <select
              id="batch-move-select"
              value={moveTo}
              onChange={(event) => setMoveTo(event.target.value)}
              disabled={busy || pickedIds.length === 0}
              className="h-7 rounded-sm border border-line bg-surface px-2 text-xs text-ink disabled:opacity-50"
            >
              <option value="">选择分组…</option>
              {projects.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
              <option value="__none__">无分组</option>
            </select>
            <Button
              variant="secondary"
              size="sm"
              disabled={busy || pickedIds.length === 0 || !moveTo}
              onClick={() => {
                const project = moveTo === '__none__' ? null : moveTo
                void onMoveTo(pickedIds, project)
              }}
            >
              应用
            </Button>
          </div>
          <Button
            variant="danger"
            size="sm"
            disabled={busy || pickedIds.length === 0}
            onClick={() => void onArchive(pickedIds)}
          >
            删除选中的 {pickedIds.length} 条
          </Button>
        </div>
      }
    >
      <div className="flex flex-col gap-2">
        <input
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="按标题搜索"
          aria-label="按标题搜索对话"
          className="h-8 rounded-sm border border-line bg-surface px-2 text-xs text-ink"
        />
        <div className="flex items-center gap-2 border-b border-line pb-1">
          <Checkbox
            id="batch-select-all"
            checked={allPicked}
            indeterminate={pickedIds.length > 0 && !allPicked}
            onChange={() => toggleAll()}
            label="全选"
          />
          <span className="text-xs text-ink-muted">
            共 {filtered.length} 条{query.trim() ? '（已过滤）' : ''}
          </span>
        </div>
        <ul className="flex max-h-[46vh] flex-col gap-0.5 overflow-y-auto">
          {filtered.length === 0 && (
            <li className="px-2 py-6 text-center text-xs text-ink-faint">没有匹配的对话。</li>
          )}
          {filtered.map((session) => (
            <li key={session.id}>
              <label
                className={cn(
                  'flex cursor-pointer items-center gap-2 rounded-sm px-2 py-1.5',
                  'transition-ui duration-fast hover:bg-surface-sunken',
                )}
              >
                <Checkbox
                  id={`batch-pick-${session.id}`}
                  checked={picked.has(session.id)}
                  onChange={() => toggleOne(session.id)}
                  label={`选择「${session.title || '未命名对话'}」`}
                />
                <span className="min-w-0 flex-1 truncate text-xs text-ink">{session.title || '未命名对话'}</span>
                <span className="shrink-0 text-2xs text-ink-faint">
                  {session.project?.trim() || '无分组'} · {session.messageCount} 条
                </span>
              </label>
            </li>
          ))}
        </ul>
      </div>
    </Modal>
  )
}
