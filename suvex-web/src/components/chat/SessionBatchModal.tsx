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
  /**
   * 批量移动到一个**新**分组（v1.20）。
   *
   * 与 `onMoveTo` 分开而不是传一个"可能不存在"的名字：新建要拦重复、
   * 要回填错误，语义比"移到一个名字"多一层。这里只负责把名字交上去，
   * 校验在页面侧的 `GroupNameDialog`/行内校验里做。
   */
  onCreateGroup: (ids: string[], name: string) => Promise<void>
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
  onCreateGroup,
  onArchive,
}: SessionBatchModalProps) {
  const [query, setQuery] = useState('')
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [moveTo, setMoveTo] = useState<string>('')
  /** 按分组筛选（对齐 CyberStrikeAI 的 `batch-project-filter`）：条目一多，靠标题搜不够。 */
  const [projectFilter, setProjectFilter] = useState<string>('')
  /** 选中「＋ 新建分组…」时手输的名字。 */
  const [newGroup, setNewGroup] = useState('')
  /** 行内校验的错误文案（只在用户点过"应用"之后才显示）。 */
  const [newGroupError, setNewGroupError] = useState('')

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    return sessions.filter((s) => {
      if (projectFilter && (s.project?.trim() || '') !== projectFilter) return false
      if (!q) return true
      return (s.title || '').toLowerCase().includes(q)
    })
  }, [sessions, query, projectFilter])

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
              onChange={(event) => {
                setMoveTo(event.target.value)
                setNewGroupError('')
              }}
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
              {/*
                ★ v1.20：与右键菜单里的「新建分组…」是同一个能力。
                  放在这里是为了"批量"这个场景 —— 一次勾 8 条再一条条
                  去右键菜单新建分组，是把同一件事做 8 遍。
              */}
              <option value="__new__">＋ 新建分组…</option>
            </select>
            {moveTo === '__new__' && (
              <input
                value={newGroup}
                onChange={(event) => {
                  setNewGroup(event.target.value)
                  setNewGroupError('')
                }}
                placeholder="新分组名字"
                aria-label="新分组名字"
                aria-invalid={newGroupError !== ''}
                className={cn(
                  'h-7 w-32 rounded-sm border bg-surface px-2 text-xs text-ink',
                  newGroupError ? 'border-signal-risk' : 'border-line',
                )}
              />
            )}
            {/*
              ★ 错误必须**画出来**。原来只 setNewGroupError 不渲染，
                等于"校验了但不告诉用户为什么点不动"——所以这条测试
                第一轮是红的（它抓的就是这个真缺口）。
            */}
            {moveTo === '__new__' && newGroupError && (
              <span role="alert" className="text-2xs text-signal-risk">
                {newGroupError}
              </span>
            )}
            <Button
              variant="secondary"
              size="sm"
              disabled={busy || pickedIds.length === 0 || !moveTo}
              onClick={() => {
                void (async () => {
                  if (moveTo === '__new__') {
                    const name = newGroup.trim()
                    if (!name) {
                      setNewGroupError('名字不能为空')
                      return
                    }
                    if (name.length > 24) {
                      setNewGroupError('最多 24 个字符')
                      return
                    }
                    if (projects.includes(name)) {
                      setNewGroupError(`「${name}」已存在，直接选它即可`)
                      return
                    }
                    await onCreateGroup(pickedIds, name)
                    setNewGroup('')
                  } else {
                    const project = moveTo === '__none__' ? null : moveTo
                    await onMoveTo(pickedIds, project)
                  }
                  /*
                    应用成功后清空勾选与选择：留着勾选会让用户以为"还没生效"，
                    再点一次就变成对同一批的重复移动（幂等但白跑一遍网络）。
                  */
                  setPicked(new Set())
                  setMoveTo('')
                  setNewGroupError('')
                })()
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
        <div className="flex flex-wrap items-center gap-2">
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="按标题搜索"
            aria-label="按标题搜索对话"
            className="h-8 min-w-0 flex-1 rounded-sm border border-line bg-surface px-2 text-xs text-ink"
          />
          <select
            value={projectFilter}
            onChange={(event) => setProjectFilter(event.target.value)}
            aria-label="按分组筛选"
            className="h-8 rounded-sm border border-line bg-surface px-2 text-xs text-ink"
          >
            <option value="">全部分组</option>
            {projects.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
            <option value="__none__">无分组</option>
          </select>
        </div>
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
