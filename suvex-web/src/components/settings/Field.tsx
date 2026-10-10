import { cn } from '@/lib/cn'
import type { ReactNode } from 'react'

/**
 * 设置页的表单原子。
 *
 * 与向导（WizardSteps 内部的 Field）刻意**没有合并成一份**：
 *   向导的 Field 服务于"一步一步走"的流程，样式偏大（h-10、gap-bay）；
 *   设置页是密集的表单面板，需要更紧凑的节奏（h-9、gap-panel）。
 *   强行统一会让两边都别扭 —— 一个是流程骨架，一个是配置面板，
 *   视觉密度本来就该不同。
 *
 *   真正需要一致的是**语义约定**，那些在这里照搬向导的做法：
 *     · 标签在上、控件在下（design-system §4）
 *     · 必填标记紧跟标签
 *     · 错误只在提交后显示，提示一直显示
 *     · 错误用 aria-describedby 关联，读屏器能念到
 */

export interface FieldProps {
  label: string
  htmlFor?: string
  required?: boolean
  /** 常驻的说明文字 —— 它在帮忙，不是在指责，所以一直显示 */
  help?: string
  /** 校验错误。非空时显示在控件下方 */
  error?: string | null
  /**
   * 错误位置的自定义内容（v0.12）。
   *
   * 给「我已知晓风险」这类**要在报错处直接给出解决动作**的场景用。
   * 它替换掉默认的 `error` 段落 —— 两者同时出现会让同一件事说两遍。
   */
  footer?: ReactNode
  children: ReactNode
}

export function Field({ label, htmlFor, required, help, error, footer, children }: FieldProps) {
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-baseline gap-1.5">
        {/* 有 htmlFor 用 label（点标签能聚焦控件），没有就用 span 避免空关联 */}
        {htmlFor ? (
          <label htmlFor={htmlFor} className="text-xs font-medium text-ink">
            {label}
          </label>
        ) : (
          <span className="text-xs font-medium text-ink">{label}</span>
        )}
        {required && (
          <span className="text-2xs text-signal-risk" aria-label="必填">
            必填
          </span>
        )}
      </div>

      {help && <p className="text-2xs text-ink-faint">{help}</p>}
      {children}

      {footer ?? (error && (
        <p role="alert" className="text-2xs text-signal-risk">
          {error}
        </p>
      ))}
    </div>
  )
}

/** 文本输入。样式统一走这里，避免每个调用点各写一遍 className */
export function TextInput({
  id,
  value,
  onChange,
  placeholder,
  error,
  mono,
  type = 'text',
  autoComplete = 'off',
  describedBy,
}: {
  id: string
  value: string
  onChange: (value: string) => void
  placeholder?: string
  error?: string | null
  /** 等宽：用于地址、模型名、URL 这类要精确输入的标识符 */
  mono?: boolean
  type?: string
  autoComplete?: string
  describedBy?: string
}) {
  return (
    <input
      id={id}
      type={type}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      autoComplete={autoComplete}
      aria-invalid={error ? true : undefined}
      aria-describedby={describedBy}
      className={cn(
        'h-9 w-full rounded-sm border bg-surface-sunken px-2.5 text-sm text-ink',
        'transition-ui duration-fast placeholder:text-ink-faint',
        'focus:outline-none focus:ring-2 focus:ring-accent/40',
        mono && 'font-mono',
        error ? 'border-signal-risk' : 'border-line hover:border-line-strong',
      )}
    />
  )
}

/**
 * 开关（checkbox 形态）。
 *
 * 用真 checkbox 不用滑块 —— 理由与报告配置区一致：
 *   滑块看起来更"现代"，但读屏器念不出可靠状态，键盘用户也拿不到标准行为，
 *   而这里要同时表达"禁用 + 原因"，原生 checkbox 的语义是现成的。
 */
export function Toggle({
  id,
  label,
  hint,
  checked,
  disabled,
  disabledHint,
  onChange,
}: {
  id: string
  label: string
  hint?: string
  checked: boolean
  disabled?: boolean
  disabledHint?: string
  onChange: (value: boolean) => void
}) {
  return (
    <div
      className={cn(
        'flex items-start gap-tight rounded-sm border p-2.5 transition-ui duration-fast',
        disabled ? 'border-line/50' : 'border-line hover:border-line-strong',
      )}
    >
      <input
        id={id}
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
        className={cn(
          'mt-0.5 h-3.5 w-3.5 shrink-0 rounded-xs border-line bg-surface-sunken accent-[#8B7CF6]',
          disabled ? 'cursor-not-allowed opacity-40' : 'cursor-pointer',
        )}
      />
      <label
        htmlFor={id}
        className={cn('min-w-0 flex-1', disabled ? 'cursor-not-allowed' : 'cursor-pointer')}
      >
        <span className={cn('block text-xs', disabled ? 'text-ink-faint' : 'text-ink')}>
          {label}
        </span>
        {(hint || disabledHint) && (
          <span className="mt-0.5 block text-2xs text-ink-faint">
            {/* 禁用时换成禁用原因 —— 用户此刻的问题是"为什么点不了" */}
            {disabled ? disabledHint : hint}
          </span>
        )}
      </label>
    </div>
  )
}

/**
 * 危险操作按钮（删除、吊销）。
 *
 * 单独成一个组件是因为它有一个必须统一的语义：
 * **危险动作永远不用主色**。用 accent 会让人误以为它是推荐操作，
 * 而删除类操作需要的是"不那么显眼 + 明确后果"。
 */
export function DangerButton({
  onClick,
  children,
  disabled,
}: {
  onClick: () => void
  children: ReactNode
  disabled?: boolean
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={cn(
        'inline-flex h-7 items-center rounded-sm px-2 text-2xs transition-ui duration-fast',
        disabled
          ? 'cursor-not-allowed text-ink-ghost'
          : 'text-ink-faint hover:bg-signal-risk/10 hover:text-signal-risk',
      )}
    >
      {children}
    </button>
  )
}

export default Field
