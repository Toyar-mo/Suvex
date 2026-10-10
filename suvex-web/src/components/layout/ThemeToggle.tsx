import { Check, Monitor, Moon, Sun } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import type { LucideIcon } from 'lucide-react'

import { setThemePreference, useTheme } from '@/data/theme-store'
import { cn } from '@/lib/cn'
import type { ResolvedTheme, ThemePreference } from '@/lib/theme'

/**
 * 亮/暗主题开关（右上角）。
 *
 * ## 为什么是一个图标按钮 + 下拉，而不是"点一下在三个状态里轮转"
 *
 * 三态轮转看着更省一次点击，但它有两个说不清的地方：
 *   · 状态不可见 —— 用户点之前不知道下一个是哪个（"再点一下会到亮色还是跟随系统？"）；
 *   · 「跟随系统」是一个**选择**，不是一个结果。轮转把它和另外两个混成一串，
 *     而它恰恰是缺省值、也是最需要能被明确选回来的那个。
 * 下拉把三个选项一次摊开，当前项打勾，"我在哪 / 我要去哪"都看得见。
 *
 * ## 按钮图标为什么在 system 下是显示器
 *
 * 图标要说的是**当前偏好**，不是"现在看起来是亮的"：选了「跟随系统」时，
 * 用户最需要知道的是"我没锁死颜色"，所以画显示器；至于此刻解析成亮还是暗，
 * 由 title / 悬停提示补足（否则就分不清"跟随系统且当前是亮"和"我锁了亮色"）。
 */

const THEME_OPTIONS: { value: ThemePreference; label: string; icon: LucideIcon }[] = [
  { value: 'system', label: '跟随系统', icon: Monitor },
  { value: 'light', label: '亮色', icon: Sun },
  { value: 'dark', label: '暗色', icon: Moon },
]

/** 显式偏好下的按钮图标。解析结果只有两个值，所以这里一定能取到 */
const RESOLVED_ICON: Record<ResolvedTheme, LucideIcon> = { light: Sun, dark: Moon }

export function ThemeToggle() {
  const { preference, resolved } = useTheme()
  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement>(null)
  const buttonRef = useRef<HTMLButtonElement>(null)

  // Esc / 点外部关闭。刻意与 ScanViewsMenu 用**同一套**写法：
  // 同一个界面里两套菜单行为不一致（一个能 Esc、一个不能）比少一种关闭方式更烦人。
  useEffect(() => {
    if (!open) return
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node
      if (wrapRef.current?.contains(target)) return
      setOpen(false)
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      setOpen(false)
      // 焦点还给按钮，否则键盘用户会掉到一个没有焦点的位置
      buttonRef.current?.focus()
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  const current = THEME_OPTIONS.find((option) => option.value === preference)
  const currentLabel = current?.label ?? '跟随系统'
  const Icon = preference === 'system' ? Monitor : RESOLVED_ICON[resolved]
  const resolvedLabel = resolved === 'light' ? '亮色' : '暗色'

  return (
    <div ref={wrapRef} className="relative shrink-0">
      <button
        ref={buttonRef}
        type="button"
        onClick={() => setOpen((prev) => !prev)}
        aria-expanded={open}
        aria-haspopup="menu"
        /*
          aria-label 说清"现在是什么 + 点了会怎样"。
          只写"切换主题"的话，屏幕阅读器用户听不出当前处于哪一态 ——
          而这个按钮的图标恰好是纯视觉的，读屏念不到。
        */
        aria-label={`当前：${currentLabel}，点击切换主题`}
        title={
          preference === 'system'
            ? `跟随系统（当前为${resolvedLabel}）`
            : `当前：${currentLabel}`
        }
        className={cn(
          'inline-flex h-8 w-8 items-center justify-center rounded-sm border border-line',
          'text-ink-muted transition-ui duration-fast',
          'hover:border-line-strong hover:text-ink',
        )}
      >
        <Icon size={14} strokeWidth={1.75} aria-hidden="true" />
      </button>

      {open && (
        <div
          role="menu"
          aria-label="主题"
          className="absolute right-0 top-full z-30 mt-1 w-36 animate-stream-in rounded-md border border-line bg-surface-overlay p-1 shadow-overlay"
        >
          {THEME_OPTIONS.map(({ value, label, icon: OptionIcon }) => {
            const checked = value === preference
            return (
              <button
                key={value}
                /*
                  用 menuitemradio 而不是 menuitem：三个选项是**互斥的一组单选**，
                  aria-checked 让读屏念出"已选中的那个"，而不是只念三项文字。
                */
                role="menuitemradio"
                type="button"
                aria-checked={checked}
                onClick={() => {
                  setThemePreference(value)
                  setOpen(false)
                  buttonRef.current?.focus()
                }}
                className={cn(
                  'flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left text-xs',
                  'transition-ui duration-fast',
                  checked
                    ? 'bg-accent-wash text-ink'
                    : 'text-ink-muted hover:bg-surface-raised hover:text-ink',
                )}
              >
                <OptionIcon size={13} strokeWidth={1.75} aria-hidden="true" className="shrink-0" />
                <span className="flex-1">{label}</span>
                {/* 勾既是"选中"，也是这一行唯一需要被一眼扫到的信息 */}
                {checked && (
                  <Check
                    size={12}
                    strokeWidth={2}
                    aria-hidden="true"
                    className="shrink-0 text-accent-strong"
                  />
                )}
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}

export default ThemeToggle
