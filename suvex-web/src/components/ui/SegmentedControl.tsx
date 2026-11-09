import type { ReactNode } from 'react'

import { cn } from '@/lib/cn'

/**
 * 等宽分段控件：选中项由一块**常驻**的滑块表示，切换时滑块平移过去。
 *
 * ## 出处与取舍
 *
 * 交互形态来自 `zhoulinhua0-star/smooth-option-switcher`（MIT）：
 * 互斥选项 + 一个会移动的 active 指示器。它的规范（`references/production.md`）
 * 明确建议：
 *
 *   · 只在**少量、等宽**的互斥选项上用滑动指示器；
 *   · 指示器必须**常驻 DOM**，切换时移动而不是重挂载（否则中断/反向切换会跳）；
 *   · 用应用状态，不为这个效果引入动画库；
 *   · `prefers-reduced-motion` 下保留选中态、去掉位移。
 *
 * 本项目落地的差异：参考实现用 `left` 做动画，而 `design-system §5.1`
 * 明确「布局属性不进 transition-ui」——这里改为 `transform: translateX`，
 * 既保住了"连续可逆的滑动"，又不触发布局。
 *
 * ## 为什么要求等宽
 *
 * 滑块宽度是 `内容宽 / N`，位移是自身宽度的整数倍。选项宽度不等时
 * 这个公式必然错位；参考文档也明确说"不等宽请显式设计并测试，
 * 不要悄悄错位"。所以调用方只应传长度相近的短标签。
 *
 * ## 可访问性
 *
 * 沿用 `role="tab"` + `aria-selected`（调用方必须真的提供对应 tabpanel）；
 * 滑块是纯装饰（`aria-hidden`）。键盘焦点环仍在按钮上。
 */
export interface SegmentedOption<T extends string> {
  id: T
  label: ReactNode
  icon?: ReactNode
}

export interface SegmentedControlProps<T extends string> {
  options: readonly SegmentedOption<T>[]
  value: T
  onChange: (id: T) => void
  /** 给 tablist 的可读名称（如「报告视图」） */
  ariaLabel: string
  /** `md` 用于页面级视图切换；`sm` 用于工具栏里的紧凑开关 */
  size?: 'sm' | 'md'
  className?: string
}

export function SegmentedControl<T extends string>({
  options,
  value,
  onChange,
  ariaLabel,
  size = 'md',
  className,
}: SegmentedControlProps<T>) {
  const count = Math.max(1, options.length)
  const index = Math.max(
    0,
    options.findIndex((option) => option.id === value),
  )
  const compact = size === 'sm'

  return (
    <div
      role="tablist"
      aria-label={ariaLabel}
      className={cn(
        'relative inline-flex rounded-md border border-line bg-surface-sunken',
        compact ? 'p-0.5' : 'p-1',
        className,
      )}
    >
      {/*
        滑块常驻。宽度按内容区等分；`translateX(index * 100%)` 的百分比
        相对滑块自身宽度，因此每一项正好一格。
      */}
      <span
        aria-hidden="true"
        className={cn(
          'pointer-events-none absolute rounded-sm bg-surface-raised shadow-panel',
          compact ? 'inset-y-0.5 left-0.5' : 'inset-y-1 left-1',
          'transition-transform motion-reduce:transition-none',
        )}
        style={{
          width: `calc((100% - ${compact ? '0.25rem' : '0.5rem'}) / ${count})`,
          transform: `translateX(${index * 100}%)`,
          // 弹簧手感：`linear()` 能表达"过冲 3% 再回落"，cubic-bezier 不能。
          // 采样点来自 motion-web 的 spring-default 等价曲线（数值，不复制代码）。
          // reduced-motion 由全局规则压到 0.001ms。
          transitionDuration: '320ms',
          transitionTimingFunction:
            'linear(0, 0.32 8%, 0.79 20%, 1.03 30%, 1.01 46%, 1)',
        }}
      />
      <div
        className="relative grid"
        style={{ gridTemplateColumns: `repeat(${count}, minmax(0, 1fr))` }}
      >
        {options.map((option) => {
          const selected = option.id === value
          return (
            <button
              key={option.id}
              type="button"
              role="tab"
              aria-selected={selected}
              onClick={() => onChange(option.id)}
              className={cn(
                'relative z-10 inline-flex items-center justify-center gap-1.5 whitespace-nowrap rounded-sm transition-ui duration-fast',
                compact ? 'px-2 py-1 text-xs' : 'px-3 py-1.5 text-sm',
                selected ? 'text-ink' : 'text-ink-muted hover:text-ink',
              )}
            >
              {option.icon}
              {option.label}
            </button>
          )
        })}
      </div>
    </div>
  )
}
