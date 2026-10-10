import { cn } from '@/lib/cn'
import { useEffect, useRef, useState } from 'react'

/**
 * 「填理由」弹窗。**目前只服务于"标记为误报"**。
 *
 * ## 为什么误报要拦一道，而其它状态变更不拦
 *
 * 后端已经强制要求（契约 §3.4 v0.14）：`status=false_positive` 时 `note`
 * 必填，否则 `400 VALIDATION_FAILED`。这条规则不是我们发明的 ——
 * 企业安全产品普遍要求误报有说明，因为它要回答一个具体问题：
 * **规则误报率是多少、是哪一类规则在误报**。没有理由的误报只能计数，
 * 没法复盘；而那个数字决定了下一步该去调规则、还是调扫描配置。
 *
 * 其它状态（确认 / 修复 / 复测 / 关闭）不拦：它们不需要解释自己。
 * 只有"我判断这条**不是**漏洞"这个断言需要留话 —— 它推翻了工具的结论。
 *
 * ## 为什么批量也只需要填**一次**
 *
 * `note` 会写到**每一条**选中项的时间线上（逐条留痕），但用户只填一次。
 * 要求 50 条各填一句的后果不是"更严谨"，而是**批量判误报这个功能没人会用** ——
 * 用户会改用「已确认」糊过去，反而把误报这个信号整个丢掉。
 *
 * ## 焦点为什么给输入框（与 `ConfirmDialog` 相反）
 *
 * `ConfirmDialog` 把焦点给"取消"，因为那是纯确认，误触回车应当什么都不发生。
 * 这里**必须**给输入框：用户点「标记误报」的下一步就是打字，
 * 让他再点一次输入框是白费一次操作。
 *
 * 但这也意味着**回车不能直接提交** —— 先打字、再回车的路径太顺，
 * 一句没写完就提交了。所以回车提交只在**非空**时生效，空时无动作。
 */
export interface ReasonDialogProps {
  open: boolean
  /** 会应用到的条数。让用户知道这一句写给了几条 */
  count: number
  busy?: boolean
  onConfirm: (reason: string) => void
  onCancel: () => void
}

export function ReasonDialog(props: ReasonDialogProps) {
  /*
    ★ 用「打开时才渲染内层」代替「在 effect 里把 state 清空」。

    两种写法都能做到"每次打开都是空的"，但后者会撞上
    `react-hooks/set-state-in-effect`：在 effect body 里同步 setState
    会触发一次级联渲染。而那不只是 lint 偏好 —— 它确实多余：
    组件都没挂载时，本来就不需要"清空"什么。

    拆成两层之后，内层的 `useState('')` 在每次打开时自然拿到初始值，
    **不需要任何重置逻辑**，调用方也不必额外传 key。
  */
  if (!props.open) return null
  return <ReasonDialogInner {...props} />
}

function ReasonDialogInner({ count, busy = false, onConfirm, onCancel }: ReasonDialogProps) {
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const [reason, setReason] = useState('')

  // 只做 DOM 操作（聚焦）与事件订阅，**不 setState** —— 见上面那段说明
  useEffect(() => {
    inputRef.current?.focus()
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onCancel()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [onCancel])

  const trimmed = reason.trim()
  const canSubmit = trimmed.length > 0 && !busy

  const submit = () => {
    if (!canSubmit) return
    onConfirm(trimmed)
  }

  return (
    <div
      onClick={onCancel}
      className="fixed inset-0 z-50 flex items-center justify-center bg-canvas/70 p-gutter backdrop-blur-sm"
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="reason-title"
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-md animate-rise-in rounded-md border border-line bg-surface-overlay p-panel shadow-overlay"
      >
        <h2 id="reason-title" className="text-sm font-medium text-ink">
          标记为误报
        </h2>
        <p className="mt-1.5 text-xs text-ink-muted">
          {count > 1
            ? `这一句会写进所选 ${count} 条的处置记录里 —— 填一次即可，不必逐条填写。`
            : '这一句会写进这条漏洞的处置记录里。'}
        </p>

        <label htmlFor="reason-input" className="mt-panel block text-2xs text-ink-faint">
          误报理由 <span className="text-signal-risk">（必填）</span>
        </label>
        <textarea
          id="reason-input"
          ref={inputRef}
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          onKeyDown={(e) => {
            /*
              回车提交只在非空时生效。
              焦点已经在输入框上，用户很可能直接回车 —— 而那时他还没写字。
              空着回车什么都不发生，比弹一句"请填写理由"更少打扰。
              Shift+Enter 换行（多行输入器的通用预期）。
            */
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              e.stopPropagation()
              submit()
            }
          }}
          maxLength={500}
          rows={3}
          placeholder="例如：该接口仅在内网可达，且返回的是占位符而非真实凭据"
          className={cn(
            'mt-1 w-full resize-y rounded-sm border bg-surface px-2.5 py-2 text-xs text-ink',
            'placeholder:text-ink-ghost',
            'focus:outline-none focus:ring-2 focus:ring-accent/40',
            trimmed.length === 0 ? 'border-line' : 'border-line-strong',
          )}
        />

        <div className="mt-1 flex items-center justify-between text-2xs text-ink-faint">
          <span>
            写清"为什么它不是漏洞"，便于日后复盘规则误报率。
          </span>
          <span className="tabular" data-numeric>
            {reason.length}/500
          </span>
        </div>

        <div className="mt-panel flex justify-end gap-tight">
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
            onClick={submit}
            disabled={!canSubmit}
            // 禁用原因放在 title 里 —— 只置灰不说原因，用户会反复点它
            title={trimmed.length === 0 ? '请先填写理由' : undefined}
            className={cn(
              'inline-flex h-8 items-center rounded-sm px-3 text-xs font-medium',
              'transition-ui duration-fast focus:outline-none focus:ring-2 focus:ring-accent/40',
              canSubmit
                ? 'bg-signal-warn/90 text-canvas hover:bg-signal-warn'
                : 'cursor-not-allowed bg-surface-raised text-ink-faint',
            )}
          >
            {busy ? '提交中…' : '确认标记误报'}
          </button>
        </div>
      </div>
    </div>
  )
}

export default ReasonDialog
