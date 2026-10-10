import { cn } from '@/lib/cn'

/**
 * 状态点。全站唯一用来表达「一件事处于什么状态」的最小单元。
 *
 * 用色规则（与 design-system.md §2 一致）：signal 色只表达结论。
 * pulse 只在「正在进行」时开 —— 满屏都在呼吸就等于没有东西在呼吸。
 */

export type StatusTone = 'accent' | 'ok' | 'warn' | 'risk' | 'info' | 'idle'

const TONE_CLASS: Record<StatusTone, string> = {
  accent: 'bg-accent',
  ok: 'bg-signal-ok',
  warn: 'bg-signal-warn',
  risk: 'bg-signal-risk',
  info: 'bg-signal-info',
  idle: 'bg-ink-ghost',
}

export interface StatusDotProps {
  tone?: StatusTone
  /** 是否呼吸。仅用于「此刻正在进行」，不要用于静态状态 */
  pulse?: boolean
  className?: string
}

export function StatusDot({ tone = 'idle', pulse = false, className }: StatusDotProps) {
  return (
    <span
      className={cn(
        'inline-block h-1.5 w-1.5 shrink-0 rounded-pill',
        TONE_CLASS[tone],
        pulse && 'animate-breath',
        className,
      )}
    />
  )
}
