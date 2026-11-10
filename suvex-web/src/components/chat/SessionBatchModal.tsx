import { Button } from '@/components/ui/Button'
import { Checkbox } from '@/components/ui/Checkbox'
import { Modal } from '@/components/ui/Modal'
import type { ChatSession } from '@/data/chat'
import { cn } from '@/lib/cn'
import { useMemo, useState } from 'react'
import { Select } from '@/components/ui/Select'

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
  /**
   * 批量删除（= 归档）。返回**失败的那些 id**。
   *
   * ★ v1.79：从"失败条数"改成"失败 id 列表"。
   *   只知道有几条失败时，界面只能笼统说"3 条失败"，而用户下一步
   *   要重试 —— 他不知道该重新勾哪几条，只好把全部再删一遍。
   */
  onArchive: (ids: string[]) => Promise<string[]>
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
  /** 上一次批量归档的结果 —— 成功几条、失败几条留在选中里 */
  const [archiveResult, setArchiveResult] = useState<string | null>(null)

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    return sessions.filter((s) => {
      /*
        ★ v1.79：「无分组」是一个**哨兵值**，不是分组名。

        下拉里 `value="__none__"` 表示"项目为空"。原来直接拿它去比
        `s.project` —— 没有任何会话的项目叫 `__none__`，所以选「无分组」
        永远得到 0 条。用户会以为"我根本没有未分组的对话"。
      */
      const project = s.project?.trim() || ''
      if (projectFilter === '__none__') {
        if (project !== '') return false
      } else if (projectFilter && project !== projectFilter) {
        return false
      }
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
            <Select
              id="batch-move-select"
              size="sm"
              value={moveTo}
              options={[
                { value: '', label: '选择分组…' },
                ...projects.map((name) => ({ value: name, label: name })),
                { value: '__none__', label: '无分组' },
                { value: '__new__', label: '＋ 新建分组…' },
              ]}
              onChange={(value) => {
                setMoveTo(value)
                setNewGroupError('')
              }}
              disabled={busy || pickedIds.length === 0}
              className="h-7 w-40"
            />
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
            onClick={() => {
              void (async () => {
                setArchiveResult(null)
                const failedIds = await onArchive(pickedIds)
                const okCount = pickedIds.length - failedIds.length
                /*
                  ★ 失败的那些**留在选中里**。用户下一步就是重试，
                    而让他在一长串里重新找出哪几条挂了，等于把
                    "逐条记下失败"这件事白做了。
                */
                setPicked(new Set(failedIds))
                setArchiveResult(
                  failedIds.length === 0
                    ? `已删除 ${okCount} 条对话。`
                    : `已删除 ${okCount} 条；${failedIds.length} 条失败，失败的仍在选中里，可以直接重试。`,
                )
              })()
            }}
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
          <Select
            size="sm"
            value={projectFilter}
            options={[
              { value: '', label: '全部分组' },
              ...projects.map((name) => ({ value: name, label: name })),
              { value: '__none__', label: '无分组' },
            ]}
            onChange={setProjectFilter}
            ariaLabel="按分组筛选"
            className="w-36"
          />
        </div>
        {archiveResult && (
          <p
            role="status"
            className="rounded-sm border border-line bg-surface-sunken px-2 py-1.5 text-2xs text-ink-muted"
          >
            {archiveResult}
          </p>
        )}
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
                <span className="min-w-0 flex-1 truncate text-xs text-ink">
                  {session.title || '未命名对话'}
                </span>
                <span className="shrink-0 text-2xs text-ink-faint">
                  {/* 「条」单独出现时不知道数的是什么 —— 这里是消息条数（含工具调用记录） */}
                  {session.project?.trim() || '无分组'} · {session.messageCount} 条消息
                </span>
              </label>
            </li>
          ))}
        </ul>
      </div>
    </Modal>
  )
}
