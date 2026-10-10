import { cn } from '@/lib/cn'
import { activeMembers, type Member } from '@/data/members-store'
import { useEffect, useMemo, useRef, useState } from 'react'
import { UserCheck } from 'lucide-react'

/**
 * 「指派处理人」弹窗（任务 10 P2）。
 *
 * ## 为什么是弹窗而不是下拉框
 *
 * 批量指派需要**先知道要指派给谁**，而下拉框在浮动操作栏里展开时会被
 * 视口底部裁掉（操作栏本身是 sticky 贴底的）。更实际的原因是：
 * 它是一个"带确认的动作"——选错了人等于把 50 条漏洞交给错的人。
 *
 * ## 为什么候选里**没有**已停用的人
 *
 * 停用意味着"不再接新活"。但**历史指派仍然显示他的名字**（那是真实记录），
 * 所以列表渲染读全量名单，而下拉只给 active 的。两者读同一份数据、
 * 用不同的过滤 —— `activeMembers()` 就是为此存在的。
 *
 * ## 焦点给搜索框
 *
 * 名单一长（几十人）就要打字筛。焦点已经在输入上，用户点开就能打。
 * 名字少时直接回车选第一个 —— 这条路径要顺，因为它是最常见的用法。
 */

export interface AssignDialogProps {
  open: boolean
  /** 会应用到的条数 */
  count: number
  members: Member[]
  /** 当前选中的这批里，已有负责人的那个名字（用于提示"将覆盖"） */
  currentNames: string[]
  busy?: boolean
  onConfirm: (assigneeId: string | null) => void
  onCancel: () => void
}

export function AssignDialog(props: AssignDialogProps) {
  // 与 `ReasonDialog` 同一个做法：打开时才渲染内层，
  // state 自然拿到初始值 —— 不需要在 effect 里"清空"
  if (!props.open) return null
  return <AssignDialogInner {...props} />
}

function AssignDialogInner({
  count,
  members,
  currentNames,
  busy = false,
  onConfirm,
  onCancel,
}: AssignDialogProps) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [query, setQuery] = useState('')
  const [picked, setPicked] = useState<string | null>(null)

  useEffect(() => {
    inputRef.current?.focus()
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onCancel()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [onCancel])

  const candidates = useMemo(() => {
    const all = activeMembers(members)
    const q = query.trim()
    if (!q) return all
    return all.filter((m) => m.name.includes(q) || (m.email ?? '').includes(q))
  }, [members, query])

  const hasAny = useMemo(() => activeMembers(members).length > 0, [members])

  const submit = (assigneeId: string | null) => {
    if (busy) return
    onConfirm(assigneeId)
  }

  return (
    <div
      onClick={onCancel}
      className="fixed inset-0 z-50 flex items-center justify-center bg-canvas/70 p-gutter backdrop-blur-sm"
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="assign-title"
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-sm animate-rise-in rounded-md border border-line bg-surface-overlay p-panel shadow-overlay"
      >
        <h2 id="assign-title" className="text-sm font-medium text-ink">
          指派处理人
        </h2>
        <p className="mt-1.5 text-xs text-ink-muted">
          {count > 1 ? `${count} 条漏洞会一起改负责人。` : '这条漏洞的负责人会变。'}
          {currentNames.length > 0 && (
            // 覆盖是常见操作（换人接手），但必须让用户知道自己在覆盖什么
            <span className="ml-1 text-ink-faint">
              当前的负责人是 {currentNames.slice(0, 3).join('、')}
              {currentNames.length > 3 ? ` 等 ${currentNames.length} 人` : ''}。
            </span>
          )}
        </p>

        {hasAny ? (
          <>
            <input
              ref={inputRef}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                /*
                  回车 = 选**第一个候选**（或已选中的那个）。
                  名单短时这是最常见路径：点开 → 回车 → 完成。
                  没有候选时不动作 —— 空提交没有意义。
                */
                if (e.key !== 'Enter') return
                e.preventDefault()
                e.stopPropagation()
                if (picked) submit(picked)
                else if (candidates.length > 0) submit(candidates[0].id)
              }}
              placeholder="搜索名字或邮箱"
              className={cn(
                'mt-panel h-8 w-full rounded-sm border border-line bg-surface px-2.5 text-xs text-ink',
                'placeholder:text-ink-ghost focus:outline-none focus:ring-2 focus:ring-accent/40',
              )}
            />

            <ul
              role="listbox"
              aria-label="处理人候选"
              className="mt-1.5 max-h-56 overflow-y-auto rounded-sm border border-line"
            >
              {candidates.length === 0 ? (
                <li className="px-2.5 py-3 text-2xs text-ink-faint">
                  没有匹配的处理人。去「设置 → 处理人」里加一个。
                </li>
              ) : (
                candidates.map((member) => {
                  const isPicked = picked === member.id
                  return (
                    <li key={member.id}>
                      <button
                        type="button"
                        role="option"
                        aria-selected={isPicked}
                        onClick={() => setPicked(member.id)}
                        onDoubleClick={() => submit(member.id)}
                        className={cn(
                          'flex w-full items-center gap-1.5 px-2.5 py-1.5 text-left text-xs',
                          'transition-ui duration-fast',
                          isPicked
                            ? 'bg-accent-wash text-ink'
                            : 'text-ink-muted hover:bg-surface-raised hover:text-ink',
                        )}
                      >
                        <span className="min-w-0 flex-1 truncate">{member.name}</span>
                        {member.email && (
                          <span className="shrink-0 truncate text-2xs text-ink-ghost">
                            {member.email}
                          </span>
                        )}
                        {isPicked && (
                          <UserCheck size={12} strokeWidth={2} className="shrink-0 text-accent-strong" />
                        )}
                      </button>
                    </li>
                  )
                })
              )}
            </ul>
          </>
        ) : (
          /* 一个处理人都没有时，不显示空下拉 —— 直接说怎么办 */
          <p className="mt-panel rounded-sm border border-line bg-surface px-2.5 py-2.5 text-2xs text-ink-faint">
            还没有可指派的处理人。先在「设置 → 处理人」里添加一个，
            或者直接确认下面的「取消指派」。
          </p>
        )}

        <div className="mt-panel flex items-center justify-between gap-tight">
          {/*
            取消指派是**明确的操作**（不是"没选"）—— 契约把 assigneeId: null
            定成一个有意义的值。所以它有自己的按钮，而不是"不选就是取消"。
          */}
          <button
            type="button"
            onClick={() => submit(null)}
            disabled={busy}
            title="清空这一批的负责人，回到「未指派」"
            className={cn(
              'inline-flex h-8 items-center rounded-sm px-2 text-2xs text-ink-faint',
              'transition-ui duration-fast hover:text-ink',
              busy && 'cursor-not-allowed opacity-50',
            )}
          >
            取消指派
          </button>

          <div className="flex gap-tight">
            <button
              type="button"
              onClick={onCancel}
              className={cn(
                'inline-flex h-8 items-center rounded-sm border border-line px-3 text-xs text-ink-muted',
                'transition-ui duration-fast hover:border-line-strong hover:text-ink',
                'focus:outline-none focus:ring-2 focus:ring-accent/40',
              )}
            >
              取消
            </button>
            <button
              type="button"
              onClick={() => picked && submit(picked)}
              disabled={!picked || busy}
              title={!picked ? '先选一个处理人' : undefined}
              className={cn(
                'inline-flex h-8 items-center rounded-sm px-3 text-xs font-medium',
                'transition-ui duration-fast focus:outline-none focus:ring-2 focus:ring-accent/40',
                picked && !busy
                  ? 'bg-accent text-canvas hover:bg-accent-strong'
                  : 'cursor-not-allowed bg-surface-raised text-ink-faint',
              )}
            >
              {busy ? '指派中…' : '确认指派'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

export default AssignDialog
