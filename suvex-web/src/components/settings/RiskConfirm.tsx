import { AlertTriangle, Check } from 'lucide-react'

import { cn } from '@/lib/cn'

/**
 * 「我已知晓风险」确认行。
 *
 * ## 为什么是一个"按钮"而不是一个默认勾上的 checkbox
 *
 * 这道提示的全部价值在于**它是一个主动动作**。默认勾选、或者只是把红字
 * 降级成灰字，等于没有拦 —— 用户不会读到它。
 * 所以：风险存在时**默认拦下保存**，点一下才放行。
 *
 * ## 为什么放在错误位置旁边
 *
 * 报错说"这条地址有问题"，而解决它的动作就在同一处 ——
 * 用户不需要去别的地方找开关。中间隔一个区块就足以让人找不到。
 *
 * ## 为什么点过之后仍然显示提示
 *
 * 放行不等于风险消失。确认后把红字换成一句中性的陈述
 * （"已确认，将以明文传输"），用户每次看到这个表单都还能被提醒一次。
 * 直接让提示消失，等于把"确认"变成了"删除警告"。
 */
export function RiskConfirm({
  acknowledged,
  onAcknowledge,
  risk,
  acknowledgedText,
}: {
  acknowledged: boolean
  onAcknowledge: () => void
  /** 风险是什么（未确认时显示，红色） */
  risk: string
  /** 确认之后留下的中性陈述 */
  acknowledgedText: string
}) {
  if (acknowledged) {
    return (
      <p className="flex items-start gap-1.5 text-2xs text-ink-faint">
        <Check size={11} strokeWidth={2.5} className="mt-0.5 shrink-0" aria-hidden="true" />
        <span>{acknowledgedText}</span>
      </p>
    )
  }

  return (
    <div className="flex flex-wrap items-start justify-between gap-2">
      <p role="alert" className="flex min-w-0 items-start gap-1.5 text-2xs text-signal-risk">
        <AlertTriangle size={11} strokeWidth={2} className="mt-0.5 shrink-0" aria-hidden="true" />
        <span>{risk}</span>
      </p>
      <button
        type="button"
        onClick={onAcknowledge}
        className={cn(
          'inline-flex h-6 shrink-0 items-center gap-1 rounded-sm border border-line px-2',
          'text-2xs text-ink-muted transition-ui duration-fast',
          'hover:border-line-strong hover:text-ink',
          'focus:outline-none focus:ring-2 focus:ring-accent/40',
        )}
      >
        我已知晓风险
      </button>
    </div>
  )
}

export default RiskConfirm
