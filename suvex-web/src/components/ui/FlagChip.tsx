import { cn } from '@/lib/cn'
import { copyText, selectElementText } from '@/lib/clipboard'
import { Check, Copy, Flag } from 'lucide-react'
import { useRef, useState } from 'react'

export interface FlagChipProps {
  flag: string
  /**
   * `compact` —— 列表行里用：宽度受限、超长省略，悬停看全文。
   * `full` —— 活动页顶部用：尽量完整显示，那是"这次任务的结论"。
   */
  variant?: 'compact' | 'full'
  className?: string
}

/**
 * CTF flag 的展示条（任务 18）。
 *
 * ## 为什么它是个按钮
 *
 * **这个组件的全部用途就是让人把值拿走。** 用户拿到 flag 之后做的第一件事
 * 是切到比赛平台粘贴提交 —— 而 flag 是 44 个字符的随机串，
 * 让人用鼠标选中再复制是在给他制造一次抄错的机会。
 * 所以点一下即复制，没有"先选中"这一步。
 *
 * ## 为什么点击后原地变成「已复制」而不是弹 toast
 *
 * 与 `CodeBlock` 保持一致：这个反馈就在鼠标底下，2 秒后自己变回去。
 * 弹 toast 会把注意力从"我刚点了什么"引到屏幕角落，而这里的成功
 * 本来就近在眼前。
 *
 * ## ★ 失败时**不能静默**（v2，实测修正）
 *
 * 原文写的是：
 *
 * > ⚠️ 剪贴板不可用时**静默失败**（非安全上下文、权限被拒）。
 * > 不弹错误：用户仍然可以手动选中复制，功能没丢 ——
 * > 而为一个"其实还能自己来"的失败弹红字，只会让人以为坏了。
 *
 * **两个判断都错了，而且是实测打掉的**（2026-10-01）：
 *
 * ① 「非安全上下文」不是偶发情况，是本系统的**部署常态**：按局域网 IP
 *    走 http 时 `navigator.clipboard` 根本不存在（`isSecureContext`
 *    为 false，实测），于是**每一次**复制都抛 `TypeError` 被吞掉。
 * ② 「用户仍然可以手动选中复制」在这个组件上不成立：它是 `truncate`
 *    显示的，界面上只有 `CTF2{070b3903-1bf5-4d41-9566-e56bc…` ——
 *    用户手动选中之后**无法确认自己拿全了**。
 *
 * 而用户的真实反应正是原文想避免的那件事：「这个按了没复制」。
 * 一个看起来能按、按了没反应的按钮，比一句"复制不了"更让人以为坏了。
 *
 * 所以现在的失败处理分两步，都不弹红字：
 * * 先走 `copyText` 的降级链（`execCommand` 在非安全上下文下可用）；
 * * 只有降级也失败时才换文案，并**自动选中全文** —— 用户按一下
 *   Ctrl+C 就拿到了完整值，同时明确知道"自动复制没成"。
 */
export function FlagChip({ flag, variant = 'compact', className }: FlagChipProps) {
  const [state, setState] = useState<'idle' | 'copied' | 'manual'>('idle')
  const textRef = useRef<HTMLSpanElement>(null)

  const handleCopy = async () => {
    if (await copyText(flag)) {
      setState('copied')
      // 2 秒后恢复 —— 短暂到够确认，又不会一直占着视觉
      window.setTimeout(() => setState('idle'), 2000)
      return
    }
    // 自动复制没成：把全文选中，让用户按 Ctrl+C。
    // 用 `textRef`（DOM 里的**完整**文本），不是视觉上截断的那部分。
    selectElementText(textRef.current)
    setState('manual')
    window.setTimeout(() => setState('idle'), 4000)
  }

  const Icon = state === 'copied' ? Check : variant === 'full' ? Flag : Copy
  const label =
    state === 'copied' ? '已复制' : state === 'manual' ? '按 Ctrl+C 复制（已选中全文）' : null

  return (
    <button
      type="button"
      onClick={handleCopy}
      // title 给全文：compact 下会被省略号截断，而"截断的是中间还是结尾"
      // 不该由用户去猜 —— 他要的是完整的那串字符
      title={label ?? `点击复制：${flag}`}
      aria-label={label ? `${label}：${flag}` : `复制答案：${flag}`}
      className={cn(
        'inline-flex min-w-0 max-w-full items-center gap-1 rounded-xs border px-1.5 transition-ui duration-fast',
        state === 'manual'
          ? 'border-signal-warn/50 bg-signal-warn/[0.10] text-signal-warn'
          : 'border-signal-ok/40 bg-signal-ok/[0.08] text-signal-ok hover:border-signal-ok/70 hover:bg-signal-ok/[0.14]',
        variant === 'compact' ? 'h-5 max-w-[18rem] text-2xs' : 'h-6 text-xs',
        className,
      )}
    >
      <Icon size={variant === 'compact' ? 10 : 12} strokeWidth={2.25} aria-hidden="true" />
      <span ref={textRef} className="mono-ui min-w-0 truncate">
        {flag}
      </span>
      {state === 'manual' && <span className="shrink-0">Ctrl+C</span>}
    </button>
  )
}

