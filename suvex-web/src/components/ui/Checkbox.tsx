import { cn } from '@/lib/cn'
import { useEffect, useRef } from 'react'

/**
 * 表格行用的 checkbox。
 *
 * ## 为什么不复用 `Field.tsx` 的 `Toggle`
 *
 * 那个是**设置页**的形态：整块可点、带标题与说明文字、尺寸偏大。
 * 列表里每行左边只需要一个 14px 的方框 —— 用 Toggle 会把行高撑起来，
 * 而"每行都变成一个卡片"正是要避免的。
 *
 * ## `indeterminate` 为什么要用 ref 设
 *
 * HTML 的 checkbox 有一个**只能用 JS 设**的第三态：
 * `input.indeterminate = true`。它不是一个属性、没有对应的 HTML 写法，
 * 也没有 React 的 prop。用 ref 直接设是唯一可行的方式。
 *
 * 它对应的是"选了**一部分**"—— 表头全选框在没有它时只有"全选/全不选"
 * 两种显示，而"选了 3/10 条"会被显示成"没选"，那是在撒谎。
 */
export function Checkbox({
  id,
  checked,
  indeterminate = false,
  onChange,
  label,
  disabled,
}: {
  id: string
  checked: boolean
  indeterminate?: boolean
  onChange: (next: boolean) => void
  /** 无障碍标签。列表里没有可见文字，必须给 —— 否则读屏器只念"复选框" */
  label: string
  disabled?: boolean
}) {
  const ref = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (ref.current) ref.current.indeterminate = indeterminate && !checked
  }, [indeterminate, checked])

  return (
    <input
      ref={ref}
      id={id}
      type="checkbox"
      checked={checked}
      disabled={disabled}
      aria-label={label}
      onChange={(e) => onChange(e.target.checked)}
      className={cn(
        'h-3.5 w-3.5 shrink-0 rounded-xs border-line bg-surface-sunken accent-[#8B7CF6]',
        disabled ? 'cursor-not-allowed opacity-40' : 'cursor-pointer',
      )}
    />
  )
}

export default Checkbox
