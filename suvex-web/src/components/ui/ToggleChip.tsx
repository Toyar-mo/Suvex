import { cn } from '@/lib/cn'
import type { ButtonHTMLAttributes, Ref } from 'react'

/**
 * 开关型按钮（chip / 分段控件的选项）。它的语义是「按下/未按下」，不是「执行一次」——
 * 所以它必然带 `aria-pressed`，也必须有 `active` 的可见状态。
 *
 * ## 为什么它不能是 `Button` 的一个变体
 *
 * 形状就不一样：`Button` 的高度是固定的（`h-7`…`h-10`），而分段控件的选项得
 * **撑满外层容器**（`h-full`）—— 外层是 `flex h-8 items-center rounded-sm border p-0.5`，
 * 选项自己带高度就会把容器顶开，边框和内选项对不齐。
 * 把它塞进 `Button` 会让 `Button` 的尺寸表多出一个"高度由父级决定"的例外，
 * 而尺寸表存在的意义正是消灭例外。
 *
 * ## 两种形态的出处
 *
 * | 形态       | 现状                                                        |
 * | ---------- | ----------------------------------------------------------- |
 * | `pill`     | `Kernel.tsx:513`、`ScansPage.tsx:597`/`:634`（带边框的胶囊）    |
 * | `segment`  | `FindingsPage.tsx:984`/`:1044`（无边框，边框由外层 `role="group"` 给） |
 *
 * ⚠️ `pill` 用 `rounded-pill`，这在 design-system §4 里本来是「只留给状态点」的。
 *    但筛选 chip 是既有事实（两个页面的状态筛选都是胶囊），且它的形状表达的是
 *    「一组可并存的筛选器」而非「一个按钮」。本次收编**不改形状**，
 *    改形状属于视觉重做，不在这个任务里。
 *
 * ## ★ 禁用态为什么不用 `opacity`
 *
 * `FindingsPage.tsx:1509` 原注释写得很清楚：「不叠加 opacity，会把文字一起压暗」。
 * 这里沿用：禁用走 `text-ink-faint` + 半淡边框，每一层都还在 AA 之上。
 * 这与 `Button` 的禁用配色是**两条不同的规矩**，不是不一致：
 * `Button` 的禁用是"这一刻不能点"，按钮身上还带着一个本该醒目的实心底色（紫底 /
 * 红底），所以它要连底色一起换掉；而 chip 的禁用态常常是"这一项**永远**
 * 不能选"（比如没有对应数据的筛选项），边框和内距要长期可读，只减淡边框就够。
 * 两条规矩的**共同点**是都不动用 `opacity`。
 */

export type ToggleChipSize = 'xs' | 'sm'
export type ToggleChipVariant = 'pill' | 'segment'

/** 高度 + 内距 + 字号。`segment` 的高度由外层给，见下 */
const PILL_SIZE: Record<ToggleChipSize, string> = {
  xs: 'h-7 px-2 text-2xs',
  sm: 'h-8 px-2.5 text-xs',
}

/** 分段控件只接字号与横向内距 —— 高度必须留给外层容器 */
const SEGMENT_SIZE: Record<ToggleChipSize, string> = {
  xs: 'px-2 text-2xs',
  sm: 'px-2.5 text-xs',
}

const ACTIVE = 'border-accent/50 bg-accent-wash text-ink'
const IDLE = 'border-line text-ink-muted hover:border-line-strong hover:text-ink'

export interface ToggleChipProps
  extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'aria-pressed' | 'onClick'> {
  ref?: Ref<HTMLButtonElement>
  /** 是否处于按下状态。落到 `aria-pressed`，颜色只是冗余编码 */
  active: boolean
  /**
   * 切换回调。写成 `onToggle` 而不是 `onClick`，因为它不执行动作，只改状态。
   *
   * ⚠️ `onClick` 被显式 `Omit` 掉了（2026-10-17 补）。原来它还在类型里，
   * 但 `{...rest}` 在 `onClick={onToggle}` **之前**展开 —— 调用方写
   * `<ToggleChip onClick={fn}>` 类型检查过得去、运行时被静默吞掉，
   * 症状是"按钮点了没反应"。这种 bug 不会报错，只会被当成"没实现"。
   * 宁可在编译期拦住，也不要留一个专等人踩的坑。
   */
  onToggle: () => void
  variant?: ToggleChipVariant
  size?: ToggleChipSize
  className?: string
}

export function ToggleChip({
  ref,
  active,
  onToggle,
  variant = 'pill',
  size = 'sm',
  disabled,
  type = 'button',
  className,
  children,
  ...rest
}: ToggleChipProps) {
  const isPill = variant === 'pill'

  return (
    <button
      {...rest}
      ref={ref}
      type={type}
      disabled={disabled}
      aria-pressed={active}
      onClick={onToggle}
      className={cn(
        'inline-flex items-center gap-1.5 transition-ui duration-fast',
        isPill ? 'press rounded-pill border' : 'h-full rounded-xs',
        isPill ? PILL_SIZE[size] : SEGMENT_SIZE[size],
        disabled
          ? 'cursor-not-allowed border-line/50 text-ink-faint'
          : active
            ? ACTIVE
            : IDLE,
        className,
      )}
    >
      {children}
    </button>
  )
}

export default ToggleChip
