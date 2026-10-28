import { Button } from '@/components/ui/Button'
import { Modal } from '@/components/ui/Modal'
import { cn } from '@/lib/cn'
import { useEffect, useRef, useState } from 'react'

/**
 * 「给分组起个名字」弹窗。
 *
 * ## 为什么需要它（补的是「鸡生蛋」）
 *
 * 分组就是会话的 `project` 字段，平台里**没有 project 表**（见 `ChatPage`
 * 里 `projectSuggestions` 的注释：分组清单是从已有会话里推出来的）。
 * 于是「移动到分组」的子菜单只能列出**已经存在**的分组 —— 一个分组都没
 * 有的时候，那个子菜单里只有一项「无分组」，点了等于什么都没发生。
 * 用户看到的就是：这个菜单项存在，但没有用。
 *
 * 所以「新建分组」不是锦上添花，它是让「移动到分组」从"摆设"变成"能用"
 * 的那一步：先有名字，才有分组；有了分组，子菜单才有东西可列。
 *
 * ## 为什么新建 = 顺手把当前会话放进去
 *
 * 用户点的是「移动到分组 → 新建分组…」，他的意图是"把这条放到一个新组里"，
 * 不是"先建一个空组，然后我再回来移一次"。所以确认后直接执行移动，
 * 空分组不会出现在任何地方（分组清单是推导出来的，空组根本不存在）。
 *
 * ## 校验为什么放在组件里而不是只靠后端
 *
 * `project` 是自由文本，后端不会拒绝空串或超长（见 `ChatControls` 的
 * `suggestions` 也是自由输入）。但"名字为空"和"名字重复"在这里都**能
 * 当场判断**，让用户先跑一轮网络再看到错误是白费一次往返。
 */
export interface GroupNameDialogProps {
  open: boolean
  /** 已有的分组名，用来拦重复。 */
  existing: string[]
  busy?: boolean
  onConfirm: (name: string) => void
  onCancel: () => void
}

/** 与 `ChatControls` 的自由输入保持同一上限：太长的名字在侧栏会被截成省略号。 */
const MAX_LENGTH = 24

export function GroupNameDialog({
  open,
  existing,
  busy = false,
  onConfirm,
  onCancel,
}: GroupNameDialogProps) {
  const [name, setName] = useState('')
  const [touched, setTouched] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  /*
    每次打开都重置。`open` 放进依赖而不是卸载重挂：`Modal` 自己控制挂载，
    这里只需要保证"上一次输了一半的名字"不会带进下一次。
  */
  useEffect(() => {
    if (!open) return
    setName('')
    setTouched(false)
    /*
      焦点给输入框：用户点「新建分组…」的下一步必然是打字。
      用 `requestAnimationFrame` 是因为 `Modal` 在同一个提交里挂载内容，
      同步 `focus()` 会落在还没插进文档的节点上（焦点静默丢失）。
    */
    const id = window.requestAnimationFrame(() => inputRef.current?.focus())
    return () => window.cancelAnimationFrame(id)
  }, [open])

  const trimmed = name.trim()
  const duplicated = trimmed !== '' && existing.includes(trimmed)
  const tooLong = trimmed.length > MAX_LENGTH
  const invalid = trimmed === '' || duplicated || tooLong

  function submit() {
    setTouched(true)
    if (invalid) return
    onConfirm(trimmed)
  }

  return (
    <Modal
      open={open}
      onClose={onCancel}
      title="新建分组"
      widthClass="max-w-sm"
      footer={
        <div className="flex items-center justify-end gap-2">
          <Button variant="secondary" size="sm" onClick={onCancel} disabled={busy}>
            取消
          </Button>
          <Button size="sm" onClick={submit} disabled={busy || (touched && invalid)}>
            {busy ? '正在移动…' : '新建并移动'}
          </Button>
        </div>
      }
    >
      <div className="flex flex-col gap-2">
        <label htmlFor="group-name-input" className="text-xs text-ink-muted">
          分组名字
        </label>
        <input
          ref={inputRef}
          id="group-name-input"
          value={name}
          onChange={(event) => setName(event.target.value)}
          onBlur={() => setTouched(true)}
          onKeyDown={(event) => {
            // 回车提交；中文输入法组合态下的回车是"上屏候选词"，必须先排除。
            if (event.key === 'Enter' && !event.nativeEvent.isComposing) {
              event.preventDefault()
              submit()
            }
          }}
          placeholder="例如：客户 A、HW2026、内部演练"
          aria-label="分组名字"
          aria-invalid={touched && invalid}
          className={cn(
            'h-8 rounded-sm border bg-surface px-2 text-xs text-ink',
            touched && invalid ? 'border-signal-risk' : 'border-line',
          )}
        />
        {touched && duplicated && (
          <p role="alert" className="text-2xs text-signal-risk">
            「{trimmed}」已经是一个分组了，直接在那个分组上点一下就能移过去。
          </p>
        )}
        {touched && tooLong && (
          <p role="alert" className="text-2xs text-signal-risk">
            名字最多 {MAX_LENGTH} 个字符，现在 {trimmed.length} 个。
          </p>
        )}
        {touched && trimmed === '' && (
          <p role="alert" className="text-2xs text-signal-risk">
            名字不能为空。
          </p>
        )}
        <p className="text-2xs text-ink-faint">
          分组不是文件夹：一条对话只在**一个**分组里，移动会覆盖原来的分组。
        </p>
      </div>
    </Modal>
  )
}

export default GroupNameDialog
