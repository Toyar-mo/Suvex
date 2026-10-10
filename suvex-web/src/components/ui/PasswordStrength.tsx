import type { PasswordCheck } from '@/data/settings'
import { cn } from '@/lib/cn'
import { Check } from 'lucide-react'

/**
 * 密码强度条 + 要求清单。
 *
 * ★ **抽出来是因为它原来有两份**：`AccountPanel`（改密码）与初始化页
 *   各要一份。复制第二份的下场是可预见的 —— 强度档位的颜色、
 *   要求清单的文案会各自演化，然后"设置页说弱、初始化页说中"。
 *   两条规则本身都在 `checkPassword` 里（那是唯一实现），
 *   这里的职责只是**把它的结果画出来**。
 *
 * 分档而不是只给"通过/不通过"：密码强度是连续量，
 * 只回一句"不达标"用户不知道该补什么。
 */

export interface PasswordStrengthProps {
  check: PasswordCheck
  className?: string
}

/** 三格强度条。格数与 `PasswordStrength` 的三档一一对应。 */
function StrengthBar({ strength }: { strength: PasswordCheck['strength'] }) {
  const tone =
    strength === 'strong'
      ? 'bg-signal-ok'
      : strength === 'fair'
        ? 'bg-signal-warn'
        : 'bg-signal-risk'

  return (
    <div className="flex gap-0.5" aria-hidden="true">
      {[0, 1, 2].map((index) => {
        const reached =
          strength === 'strong' ||
          (strength === 'fair' && index < 2) ||
          (strength === 'weak' && index < 1)
        return (
          <span
            key={index}
            className={cn('h-1 w-8 rounded-pill', reached ? tone : 'bg-line-strong')}
          />
        )
      })}
    </div>
  )
}

export function PasswordStrength({ check, className }: PasswordStrengthProps) {
  return (
    <div
      className={cn(
        'flex flex-col gap-tight rounded-sm border border-line bg-surface-sunken p-3',
        className,
      )}
    >
      <div className="flex items-center gap-tight">
        <span className="text-2xs text-ink-muted">强度</span>
        <StrengthBar strength={check.strength} />
        {/*
          文字与颜色**同时**表达强度（design-system §7「颜色是冗余编码，
          不是唯一载体」）。只有颜色的话，色觉障碍用户读不出强弱；
          读屏器也需要这句文本。
        */}
        <span className="text-2xs text-ink-muted">
          {check.strength === 'strong' ? '强' : check.strength === 'fair' ? '中' : '弱'}
        </span>
      </div>

      {/* 要求清单直接渲染 checkPassword 的结果，不在组件里重写一遍规则 */}
      <ul className="flex flex-col gap-0.5">
        {check.requirements.map((requirement) => (
          <li
            key={requirement.label}
            className={cn(
              'flex items-center gap-1.5 text-2xs',
              requirement.met ? 'text-signal-ok' : 'text-ink-faint',
            )}
          >
            {requirement.met ? (
              <Check size={10} strokeWidth={3} aria-hidden="true" />
            ) : (
              <span
                aria-hidden="true"
                className="h-2.5 w-2.5 rounded-pill border border-line-strong"
              />
            )}
            {requirement.label}
          </li>
        ))}
      </ul>
    </div>
  )
}

export default PasswordStrength
